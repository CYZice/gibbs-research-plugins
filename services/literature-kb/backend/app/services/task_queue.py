from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import redis
from rq import Queue, Repeat, Retry, get_current_job
from rq.exceptions import DuplicateJobError, NoSuchJobError
from rq.job import Job, JobStatus
from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import delete, func, select

from app.core.config import get_settings
from app.core.ids import new_id
from app.db.session import SessionLocal
from app.models.evidence_embedding import EvidenceEmbedding
from app.models.evidence_representation import EvidenceRepresentation
from app.models.evidence_unit import EvidenceUnit
from app.models.import_batch import ImportBatch, ImportBatchItem
from app.models.operational_control import ExecutionAttempt
from app.models.paper import Paper
from app.models.paper_task import PaperTask
from app.models.workflow_run import WorkflowRun
from app.schemas.v1 import CitationGroundRequest, EvidencePackRequest
from app.services.embedding_index_service import EmbeddingIndexService
from app.services.enrichment_service import EnrichmentService
from app.services.evidence_builder import EvidenceBuilderService
from app.services.external_errors import classify_external_error
from app.services.metadata_enrichment_service import MetadataEnrichmentService
from app.services.mineru_service import MinerUService
from app.services.operations_service import evaluate_alerts, flush_api_metrics
from app.services.paper_search_service import PaperSearchIndexService
from app.services.provider_control_service import aggregate_provider_metrics
from app.services.task_queue_constants import (
    INGEST_QUEUE,
    INTERACTIVE_QUEUE,
    MAINTENANCE_QUEUE,
    QUEUE_NAMES,
)
from app.services.workflow_service import CitationGroundingWorkflow, EvidencePackWorkflow
from app.services.workflow_scope_service import (
    validate_workflow_request_scope,
    validate_workflow_result_scope,
    validate_workflow_run_binding,
)

RQ_RETRY_INTERVALS = [120, 600, 1800]
QUEUE_ENQUEUE_RETRY_INTERVALS = [60, 300]
QUEUE_ENQUEUE_MAX_FAILURES = 3
PROCESS_OPERATION = "process"
REEMBED_OPERATION = "reembed_all"
REEMBED_PAPER_OPERATION = "reembed_paper"
EMBEDDING_REPAIR_OPERATION = "embedding_repair"
ACTIVE_PROCESS_STATUSES = ("queued", "running", "retrying", "paused")
ACTIVE_JOB_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.STARTED,
    JobStatus.SCHEDULED,
    JobStatus.DEFERRED,
}
TERMINAL_OWNER_STATUSES = {"succeeded", "failed", "interrupted", "cancelled", "stale"}
WATCHDOG_LOCK_KEY = "maintenance:watchdog:lock"
logger = logging.getLogger(__name__)


class QueueJobTerminatedError(RuntimeError):
    error_code = "QUEUE_JOB_TERMINATED"
    retryable = False


class StaleJobError(RuntimeError):
    error_code = "STALE_JOB"
    retryable = False


def submit_process_task(
    db,
    paper_id: str,
    *,
    provider: str = "local",
    options: dict | None = None,
    details: dict | None = None,
) -> tuple[PaperTask, bool]:
    """Create one complete processing task, or return the paper's active task."""
    paper = db.scalar(select(Paper).where(Paper.paper_id == paper_id).with_for_update())
    if not paper:
        raise ValueError("Paper not found.")
    active = db.scalar(
        select(PaperTask)
        .where(
            PaperTask.paper_id == paper_id,
            PaperTask.operation == PROCESS_OPERATION,
            PaperTask.status.in_(ACTIVE_PROCESS_STATUSES),
        )
        .order_by(PaperTask.created_at.desc())
        .limit(1)
    )
    if active:
        return active, False

    task_id = new_id("task")
    task = PaperTask(
        task_id=task_id,
        paper_id=paper_id,
        operation=PROCESS_OPERATION,
        provider=provider,
        status="queued",
        progress=0.0,
        stage="queued",
        attempt_count=0,
        max_attempts=4,
        root_task_id=(details or {}).get("root_task_id") or task_id,
        details={
            **(details or {}),
            "options": options or {},
            "stage": "queued",
            "queue": {
                "attempt": 0,
                "max_attempts": 4,
                "job_id": task_id,
                "delivery_status": "pending",
                "enqueue_attempt": 0,
            },
        },
    )
    paper.parse_status = "queued"
    paper.evidence_status = "not_started"
    paper.multimodal_status = "not_started"
    paper.embedding_status = "not_started"
    db.add(task)
    return task, True


def submit_embedding_rebuild(db, model: str, config_version: int) -> PaperTask:
    """Queue one full-library rebuild for a confirmed embedding model change."""
    for previous in db.scalars(
        select(PaperTask).where(
            PaperTask.operation.in_((REEMBED_OPERATION, REEMBED_PAPER_OPERATION)),
            PaperTask.status.in_(ACTIVE_PROCESS_STATUSES),
        )
    ).all():
        cancel_process_task(db, previous)
    task_id = new_id("task")
    task = PaperTask(
        task_id=task_id,
        paper_id=None,
        operation=REEMBED_OPERATION,
        provider="admin",
        status="queued",
        progress=0.0,
        stage="queued",
        attempt_count=0,
        max_attempts=4,
        root_task_id=task_id,
        details={
            "stage": "queued",
            "target_model": model,
            "config_version": config_version,
            "queue": {"attempt": 0, "max_attempts": 4, "job_id": task_id, "delivery_status": "pending", "enqueue_attempt": 0},
        },
    )
    db.add(task)
    return task


def submit_embedding_repair(
    db, paper_id: str, *, provider: str = "admin", wait_for_task_id: str | None = None,
    source_task_id: str | None = None,
) -> tuple[PaperTask, bool]:
    """Create a missing-or-stale-only repair task without reprocessing the PDF."""
    paper = db.scalar(select(Paper).where(Paper.paper_id == paper_id).with_for_update())
    if not paper:
        raise ValueError("Paper not found.")
    active = db.scalar(
        select(PaperTask).where(
            PaperTask.paper_id == paper_id,
            PaperTask.operation == EMBEDDING_REPAIR_OPERATION,
            PaperTask.status.in_(ACTIVE_PROCESS_STATUSES),
        ).order_by(PaperTask.created_at.desc()).limit(1)
    )
    if active:
        return active, False
    task_id = new_id("task")
    task = PaperTask(
        task_id=task_id,
        paper_id=paper_id,
        operation=EMBEDDING_REPAIR_OPERATION,
        provider=provider,
        status="paused" if wait_for_task_id else "queued",
        progress=0.0,
        stage="waiting_for_cancel" if wait_for_task_id else "queued",
        attempt_count=0,
        max_attempts=2,
        root_task_id=task_id,
        details={
            "stage": "waiting_for_cancel" if wait_for_task_id else "queued",
            "scope": "missing_or_stale_only",
            "source_task_id": source_task_id,
            "wait_for_task_id": wait_for_task_id,
            "queue": {
                "attempt": 0, "max_attempts": 2, "job_id": task_id,
                "delivery_status": "pending", "enqueue_attempt": 0,
            },
        },
    )
    paper.embedding_status = "queued"
    db.add(task)
    return task, True


def enqueue_submitted_process(db, task: PaperTask) -> PaperTask:
    """Persist a process task and retain queue outages for watchdog recovery."""
    db.commit()
    db.refresh(task, with_for_update=True)
    if task.status not in {"queued", "retrying"}:
        return task
    next_task_ids = []
    unexpected_error = None
    queue = dict((task.details or {}).get("queue") or {})
    job_id = str(queue.get("job_id") or task.task_id)
    try:
        enqueue_task(task.task_id, job_id=job_id)
        _record_enqueue_success(task, job_id)
    except QueueJobTerminatedError as exc:
        next_task_ids = _record_enqueue_terminal(db, task, exc)
    except redis.exceptions.RedisError as exc:
        next_task_ids = _record_enqueue_failure(db, task, exc)
    except Exception as exc:
        next_task_ids = _record_enqueue_terminal(db, task, exc, "QUEUE_ENQUEUE_FAILED")
        unexpected_error = exc
        logger.exception("Unexpected process task enqueue failure for %s", task.task_id)
    db.commit()
    for next_task_id in next_task_ids:
        next_task = db.get(PaperTask, next_task_id)
        if next_task:
            enqueue_submitted_process(db, next_task)
    if unexpected_error:
        raise unexpected_error
    return task


def enqueue_task(task_id: str, job_id: str | None = None) -> str:
    db = SessionLocal()
    try:
        task = db.get(PaperTask, task_id)
        queue_name = (
            MAINTENANCE_QUEUE
            if task and task.operation in {"index", "repair_representations", REEMBED_OPERATION, REEMBED_PAPER_OPERATION, EMBEDDING_REPAIR_OPERATION}
            else INGEST_QUEUE
        )
    finally:
        db.close()
    retry = (
        Retry(max=1, interval=[120])
        if task and task.operation == EMBEDDING_REPAIR_OPERATION
        else Retry(max=3, interval=RQ_RETRY_INTERVALS)
    )
    return _enqueue_unique(
        queue_name,
        execute_task,
        task_id,
        job_id=job_id or task_id,
        job_timeout=get_settings().ingest_job_timeout_seconds,
        retry=retry,
    )


def enqueue_workflow(run_id: str, job_id: str | None = None) -> str:
    return _enqueue_unique(
        INTERACTIVE_QUEUE,
        execute_workflow,
        run_id,
        job_id=job_id or run_id,
        job_timeout=get_settings().workflow_job_timeout_seconds,
    )


def enqueue_submitted_workflow(db, run: WorkflowRun) -> WorkflowRun:
    """Persist a workflow and retain Redis outages for watchdog recovery."""
    db.commit()
    db.refresh(run, with_for_update=True)
    if run.status not in {"queued", "retrying"}:
        return run
    queue = dict((run.details or {}).get("queue") or {})
    job_id = str(queue.get("job_id") or run.run_id)
    unexpected_error = None
    try:
        _enqueue_unique(
            INTERACTIVE_QUEUE,
            execute_workflow,
            run.run_id,
            job_id=job_id,
            job_timeout=get_settings().workflow_job_timeout_seconds,
        )
        _record_enqueue_success(run, job_id)
    except QueueJobTerminatedError as exc:
        _record_owner_enqueue_failure(run, exc, retryable=False)
    except redis.exceptions.RedisError as exc:
        _record_owner_enqueue_failure(run, exc, retryable=True)
    except Exception as exc:
        _record_owner_enqueue_failure(run, exc, retryable=False, error_code="QUEUE_ENQUEUE_FAILED")
        unexpected_error = exc
        logger.exception("Unexpected workflow enqueue failure for %s", run.run_id)
    db.commit()
    if unexpected_error:
        raise unexpected_error
    return run


def _enqueue_unique(queue_name, function, owner_id: str, *, job_id: str, job_timeout=None, retry=None) -> str:
    connection = redis.Redis.from_url(get_settings().redis_url)
    if connection.get(f"admin:queue:paused:{queue_name}"):
        queue_name = f"{queue_name}-paused"
    kwargs = {
        "job_id": job_id,
        "retry": retry or Retry(max=3, interval=RQ_RETRY_INTERVALS),
        "unique": True,
    }
    if job_timeout is not None:
        kwargs["job_timeout"] = job_timeout
    try:
        job = Queue(queue_name, connection=connection).enqueue(function, owner_id, **kwargs)
        return job.id
    except DuplicateJobError as exc:
        try:
            job = Job.fetch(job_id, connection=connection)
        except NoSuchJobError as missing:
            raise redis.exceptions.ConnectionError(
                f"Duplicate RQ job {job_id} disappeared before it could be inspected."
            ) from missing
        status = job.get_status()
        if status in ACTIVE_JOB_STATUSES:
            return job.id
        raise QueueJobTerminatedError(
            f"RQ job {job_id} already exists in terminal state {status}."
        ) from exc


def execute_workflow(run_id: str) -> None:
    db = SessionLocal()
    try:
        run = db.scalar(
            select(WorkflowRun).where(WorkflowRun.run_id == run_id).with_for_update()
        )
        job = get_current_job()
        if not _can_start_owner(run, job):
            return
        validate_workflow_run_binding(run)
        payload = run.request_payload
        request_scope = payload.get("scope") if isinstance(payload, dict) else None
        validate_workflow_request_scope(db, run.access_scope, request_scope)
        details = dict(run.details or {})
        queue_details = dict(details.get("queue") or {})
        queue_details["attempt"] = int(queue_details.get("attempt", 0)) + 1
        queue_details["max_attempts"] = queue_details["attempt"] + int(
            getattr(job, "retries_left", 0) or 0
        )
        queue_details.pop("next_retry_at", None)
        queue_details.pop("next_enqueue_at", None)
        queue_details.pop("last_error", None)
        queue_details.pop("error_code", None)
        queue_details["delivery_status"] = "started"
        details["queue"] = queue_details
        details["stage"] = (
            "claim_extraction" if run.workflow == "citation_grounding" else "retrieval_and_judging"
        )
        run.details, run.status, run.progress, run.started_at, run.error_message = (
            details,
            "running",
            0.1,
            datetime.now(UTC),
            None,
        )
        db.commit()
        attempt_row = _start_attempt(
            db, "workflow", run_id, queue_details["attempt"], details["stage"], run.workflow
        )

        def update_stage(stage: str, progress: float) -> None:
            current = db.get(WorkflowRun, run_id)
            if current:
                _refresh_and_assert_current_job(db, current)
                current.progress = progress
                current.details = {**dict(current.details or {}), "stage": stage}
                db.commit()

        if run.workflow == "evidence_pack":
            body = EvidencePackRequest.model_validate(payload)
            result = EvidencePackWorkflow(
                db,
                get_settings(),
                access_scope=run.access_scope,
            ).build(
                body.claims, body.scope, body.filters, body.top_k_per_claim, update_stage
            )
        elif run.workflow == "citation_grounding":
            body = CitationGroundRequest.model_validate(payload)
            result = CitationGroundingWorkflow(
                db,
                get_settings(),
                access_scope=run.access_scope,
            ).ground(
                body.draft, body.scope, body.filters, body.top_k_per_claim, update_stage
            )
        else:
            raise RuntimeError(f"Unsupported workflow: {run.workflow}")
        result = validate_workflow_result_scope(db, run, result)
        _refresh_and_assert_current_job(db, run)
        run.result_payload, run.status, run.progress, run.finished_at = (
            result,
            "succeeded",
            1.0,
            datetime.now(UTC),
        )
        run.details = {**dict(run.details or {}), "stage": "succeeded"}
        _finish_attempt(attempt_row, "succeeded", "succeeded")
        db.commit()
    except StaleJobError:
        db.rollback()
        return
    except Exception as exc:
        db.rollback()
        run = db.get(WorkflowRun, run_id)
        job = get_current_job()
        will_retry = bool(job and job.retries_left and _is_retryable(exc))
        if run:
            details = dict(run.details or {})
            queue_details = dict(details.get("queue") or {})
            queue_details.update(_retry_details(queue_details, exc, job, run.workflow))
            details["queue"] = queue_details
            details["stage"] = "retrying" if will_retry else "failed"
            run.details, run.error_message = details, str(exc)
            if will_retry:
                run.status, run.progress, run.finished_at = "queued", 0.0, None
            else:
                run.status, run.finished_at = "failed", datetime.now(UTC)
            _fail_attempt(
                db,
                "workflow",
                run_id,
                queue_details.get("attempt", 1),
                exc,
                will_retry,
                queue_details.get("next_retry_at"),
            )
            db.commit()
        if will_retry:
            raise
    finally:
        db.close()


def execute_task(task_id: str) -> None:
    """RQ entry point. Each job owns a fresh session and one exact task."""
    db = SessionLocal()
    try:
        task = db.scalar(select(PaperTask).where(PaperTask.task_id == task_id).with_for_update())
        job = get_current_job()
        if not _can_start_owner(task, job):
            return
        if task.operation == REEMBED_OPERATION:
            _execute_embedding_rebuild(db, task, job)
            return
        if task.operation == REEMBED_PAPER_OPERATION:
            _execute_embedding_rebuild_paper(db, task, job)
            return
        if task.operation == EMBEDDING_REPAIR_OPERATION:
            _execute_embedding_repair(db, task, job)
            return
        if task.operation != PROCESS_OPERATION:
            task.status = "stale"
            task.finished_at = datetime.now(UTC)
            task.details = {**dict(task.details or {}), "stage": "superseded"}
            db.commit()
            return
        paper = db.get(Paper, task.paper_id)
        if not paper:
            raise RuntimeError("Task paper no longer exists.")
        if (task.details or {}).get("resume_from") == "embedding":
            _resume_embedding_task(db, task, paper)
            return
        task.status, task.started_at, task.error_message = "running", datetime.now(UTC), None
        details = dict(task.details or {})
        queue_details = dict(details.get("queue") or {})
        queue_details["attempt"] = int(queue_details.get("attempt", 0)) + 1
        queue_details["max_attempts"] = queue_details["attempt"] + int(
            getattr(job, "retries_left", 0) or 0
        )
        queue_details.pop("next_retry_at", None)
        queue_details.pop("next_enqueue_at", None)
        queue_details.pop("last_error", None)
        queue_details.pop("error_code", None)
        queue_details["delivery_status"] = "started"
        details["queue"] = queue_details
        details["stage"] = "parsing"
        task.details = details
        task.stage = "parsing"
        task.attempt_count = queue_details["attempt"]
        task.max_attempts = queue_details["max_attempts"]
        task.next_retry_at = None
        task.error_code = None
        task.error_domain = None
        paper.parse_status = "running"
        _mark_batch_item(db, task, "running")
        db.commit()
        attempt_row = _start_attempt(
            db, "task", task_id, queue_details["attempt"], details["stage"], task.provider
        )

        options = details.get("options") or {}
        force_rebuild = bool(options.get("force"))
        reuse_parse = bool(options.get("reuse_parse"))
        _task_stage(db, task, attempt_row, "parsing", 0.05)
        MinerUService(db, get_settings()).run_task(
            paper, task, force=force_rebuild and not reuse_parse
        )
        task.last_completed_stage = "parsing"
        try:
            metadata_result = MetadataEnrichmentService().enrich(
                paper,
                (Path(get_settings().storage_root).resolve() / "parsed" / paper.paper_id).resolve(),
            )
            task.details = {**dict(task.details or {}), "metadata": metadata_result}
            db.commit()
        except Exception as metadata_error:
            task.details = {
                **dict(task.details or {}),
                "metadata": {"status": "unavailable", "error_type": type(metadata_error).__name__},
            }
            db.commit()
        _check_cancel(task)
        _task_stage(db, task, attempt_row, "evidence", 0.35)
        EvidenceBuilderService(db, get_settings()).build_for_paper(
            paper, task, force=force_rebuild, full_rebuild=bool(options.get("full_rebuild"))
        )
        task.last_completed_stage = "evidence"
        _check_cancel(task)
        _task_stage(db, task, attempt_row, "multimodal", 0.46)
        EnrichmentService(db, get_settings()).enrich_paper(paper, task=task, force=force_rebuild)
        task.last_completed_stage = "multimodal"
        _check_cancel(task)
        _task_stage(db, task, attempt_row, "embedding", 0.55)
        if _embedding_rebuild_active(db):
            task.status = "paused"
            task.details = {**dict(task.details or {}), "stage": "embedding_paused"}
            task.error_message = None
            paper.embedding_status = "not_started"
            _finish_attempt(attempt_row, "paused", "embedding_paused")
            db.commit()
            return
        def check_embedding_cancel() -> None:
            db.refresh(task)
            _check_cancel(task)

        result = EmbeddingIndexService(db, get_settings()).build_for_paper(
            paper, force_rebuild=force_rebuild, should_cancel=check_embedding_cancel
        )
        task.last_completed_stage = "embedding"
        _task_stage(db, task, attempt_row, "indexing", 0.9)
        PaperSearchIndexService(db, get_settings()).build_for_paper(paper, include_embedding=True)
        task.last_completed_stage = "indexing"
        _refresh_and_assert_current_job(db, task)
        task.details = {
            **dict(task.details or {}),
            "result": {"embedded": result.embedded_count, "skipped": result.skipped_count},
        }

        task.status, task.progress, task.finished_at = "succeeded", 1.0, datetime.now(UTC)
        task.stage = "succeeded"
        task.error_message = None
        queue_details = {
            key: value
            for key, value in dict((task.details or {}).get("queue") or {}).items()
            if key not in {"last_error", "error_code", "next_retry_at"}
        }
        task.details = {**dict(task.details or {}), "stage": "succeeded", "queue": queue_details}
        _finish_attempt(attempt_row, "succeeded", "succeeded")
        next_tasks = _finish_batch_item(db, task, "succeeded")
        db.commit()
        for next_task_id in next_tasks:
            next_task = db.get(PaperTask, next_task_id)
            if next_task:
                enqueue_submitted_process(db, next_task)
    except StaleJobError:
        db.rollback()
        return
    except Exception as exc:
        db.rollback()
        task = db.get(PaperTask, task_id)
        will_retry = False
        next_tasks = []
        automatic_repair = None
        if task:
            classified = classify_external_error(exc)
            job = get_current_job()
            details = dict(task.details or {})
            queue_details = dict(details.get("queue") or {})
            failed_stage = str(details.get("stage") or "processing")
            process_embedding_error = (
                task.operation == PROCESS_OPERATION
                and failed_stage == "embedding"
                and _is_retryable(exc)
            )
            will_retry = bool(job and job.retries_left and _is_retryable(exc) and not process_embedding_error)
            details["queue"] = _retry_details(queue_details, exc, job, task.provider)
            details["failed_stage"] = failed_stage
            details["stage"] = "retrying" if will_retry else "failed"
            task.details, task.error_message = details, str(exc)
            task.error_code, task.error_domain = classified.code, classified.domain
            if will_retry:
                task.status, task.progress, task.finished_at = "retrying", 0.0, None
                task.stage = "retrying"
                task.next_retry_at = datetime.fromisoformat(details["queue"]["next_retry_at"]) if details["queue"].get("next_retry_at") else None
                _mark_batch_item(db, task, "retrying", str(exc))
            else:
                cancelled = details["queue"].get("error_code") == "TASK_CANCELLED"
                task.status, task.finished_at = (
                    "cancelled" if cancelled else "failed",
                    datetime.now(UTC),
                )
                task.stage = "cancelled" if cancelled else "failed"
                details["stage"] = (
                    "cancelled" if cancelled else "waiting_manual"
                    if task.operation == EMBEDDING_REPAIR_OPERATION else "failed"
                )
                task.details = details
                paper = db.get(Paper, task.paper_id)
                if paper and task.operation == EMBEDDING_REPAIR_OPERATION and not cancelled:
                    paper.embedding_status = "failed"
                if paper and task.operation == PROCESS_OPERATION:
                    stage = str((task.details or {}).get("failed_stage") or "")
                    if cancelled:
                        paper.parse_status = "cancelled"
                    elif stage in {"parsing", "retrying"}:
                        paper.parse_status = "failed"
                    elif stage == "evidence":
                        paper.evidence_status = "failed"
                    elif stage == "multimodal":
                        paper.multimodal_status = "failed"
                    elif stage in {"embedding", "indexing"}:
                        paper.embedding_status = "failed"
                if process_embedding_error:
                    automatic_repair, _created = submit_embedding_repair(
                        db, task.paper_id, provider="automatic", source_task_id=task.task_id
                    )
                next_tasks = _finish_batch_item(
                    db, task, "cancelled" if cancelled else "failed", str(exc)
                )
            _fail_attempt(
                db,
                "task",
                task_id,
                queue_details.get("attempt", 1),
                exc,
                will_retry,
                details["queue"].get("next_retry_at"),
            )
            db.commit()
            if automatic_repair:
                enqueue_submitted_process(db, automatic_repair)
            if task.operation == REEMBED_PAPER_OPERATION and not will_retry:
                _update_embedding_rebuild_parent(db, task)
            for next_task_id in next_tasks:
                next_task = db.get(PaperTask, next_task_id)
                if next_task:
                    enqueue_submitted_process(db, next_task)
        if will_retry:
            raise
    finally:
        db.close()


def _execute_embedding_rebuild(db, task: PaperTask, job) -> None:
    details = dict(task.details or {})
    queue = dict(details.get("queue") or {})
    queue["attempt"] = int(queue.get("attempt", 0)) + 1
    queue["max_attempts"] = queue["attempt"] + int(getattr(job, "retries_left", 0) or 0)
    queue["delivery_status"] = "started"
    details.update(stage="dispatching", queue=queue)
    task.details, task.status, task.progress, task.started_at, task.error_message = (
        details, "running", 0.01, datetime.now(UTC), None
    )
    db.commit()

    rows = db.execute(
        select(EvidenceUnit.paper_id, EvidenceRepresentation.representation_id, EvidenceRepresentation.text_hash)
        .join(EvidenceRepresentation, EvidenceRepresentation.evidence_id == EvidenceUnit.evidence_id)
        .where(
            EvidenceRepresentation.status == "success",
            EvidenceRepresentation.is_active.is_(True),
            EvidenceRepresentation.indexable.is_(True),
        )
    ).all()
    representation_ids = [representation_id for _, representation_id, _ in rows]
    current = {
        item.representation_id: item for item in db.scalars(select(EvidenceEmbedding).where(
            EvidenceEmbedding.representation_id.in_(representation_ids),
            EvidenceEmbedding.embedding_model == (task.details or {}).get("target_model"),
        )).all()
    }
    by_paper: dict[str, list[str]] = {}
    for paper_id, representation_id, text_hash in rows:
        item = current.get(representation_id)
        if item and item.status == "success" and item.embedding_dim == 1024 and item.text_hash == text_hash:
            continue
        by_paper.setdefault(paper_id, []).append(representation_id)
    children = []
    for paper_id, representation_ids in by_paper.items():
        child_id = new_id("task")
        child = PaperTask(
            task_id=child_id,
            paper_id=paper_id,
            operation=REEMBED_PAPER_OPERATION,
            provider="admin",
            status="queued",
            details={
                "stage": "queued", "parent_task_id": task.task_id,
                "target_model": (task.details or {}).get("target_model"),
                "representation_ids": representation_ids,
                "queue": {"attempt": 0, "max_attempts": 4, "job_id": child_id, "delivery_status": "pending", "enqueue_attempt": 0},
            },
        )
        db.add(child)
        children.append(child)
    task.details = {**dict(task.details or {}), "stage": "embedding", "total_papers": len(children), "completed_papers": 0, "failed_papers": 0}
    db.commit()
    for child in children:
        enqueue_submitted_process(db, child)
        if child.status in {"failed", "cancelled"}:
            _update_embedding_rebuild_parent(db, child)
    if not by_paper:
        _finish_embedding_rebuild(db, task)


def _execute_embedding_rebuild_paper(db, task: PaperTask, job) -> None:
    details = dict(task.details or {})
    queue = dict(details.get("queue") or {})
    queue["attempt"] = int(queue.get("attempt", 0)) + 1
    queue["delivery_status"] = "started"
    task.status, task.started_at, task.error_message = "running", datetime.now(UTC), None
    task.details = {**details, "stage": "embedding", "queue": queue}
    db.commit()

    def check_cancel() -> None:
        db.refresh(task)
        _check_cancel(task)

    result = EmbeddingIndexService(db, get_settings()).build_for_representation_ids(
        list(details.get("representation_ids") or []), model=details.get("target_model"), should_cancel=check_cancel
    )
    check_cancel()
    task.status, task.progress, task.finished_at, task.error_message = "succeeded", 1.0, datetime.now(UTC), None
    task.details = {**dict(task.details or {}), "stage": "succeeded", "result": {"embedded": result.embedded_count, "skipped": result.skipped_count}}
    db.commit()
    _update_embedding_rebuild_parent(db, task)


def _execute_embedding_repair(db, task: PaperTask, job) -> None:
    paper = db.get(Paper, task.paper_id)
    if not paper:
        raise RuntimeError("Task paper no longer exists.")
    details = dict(task.details or {})
    queue = dict(details.get("queue") or {})
    queue["attempt"] = int(queue.get("attempt", 0)) + 1
    queue["max_attempts"] = queue["attempt"] + int(getattr(job, "retries_left", 0) or 0)
    queue["delivery_status"] = "started"
    for key in ("next_retry_at", "last_error", "error_code"):
        queue.pop(key, None)
    task.status, task.started_at, task.error_message = "running", datetime.now(UTC), None
    task.details = {**details, "stage": "embedding", "queue": queue}
    paper.embedding_status = "building"
    db.commit()
    attempt = _start_attempt(db, "task", task.task_id, queue["attempt"], "embedding", task.provider)

    def check_cancel() -> None:
        db.refresh(task)
        _check_cancel(task)

    def report_progress(batch: int, total: int, embedded: int, target: int) -> None:
        db.refresh(task)
        _check_cancel(task)
        task.progress = embedded / target if target else 1.0
        task.details = {
            **dict(task.details or {}),
            "stage": "embedding",
            "progress": {"batch": batch, "total_batches": total, "embedded": embedded, "target": target},
        }
        db.commit()

    result = EmbeddingIndexService(db, get_settings()).build_for_paper(
        paper, should_cancel=check_cancel, on_progress=report_progress
    )
    check_cancel()
    PaperSearchIndexService(db, get_settings()).build_for_paper(paper, include_embedding=True)
    task.status, task.progress, task.finished_at, task.error_message = "succeeded", 1.0, datetime.now(UTC), None
    task.details = {
        **dict(task.details or {}), "stage": "succeeded",
        "result": {"embedded": result.embedded_count, "skipped": result.skipped_count},
    }
    source_task_id = details.get("source_task_id")
    source_task = db.get(PaperTask, source_task_id) if source_task_id else None
    if source_task and source_task.operation == PROCESS_OPERATION:
        source_task.details = {
            **dict(source_task.details or {}),
            "recovery": {
                "status": "succeeded",
                "task_id": task.task_id,
                "stage": "embedding",
                "completed_at": datetime.now(UTC).isoformat(),
            },
        }
    _finish_attempt(attempt, "succeeded", "succeeded")
    db.commit()


def _update_embedding_rebuild_parent(db, child: PaperTask) -> None:
    parent_id = (child.details or {}).get("parent_task_id")
    if not parent_id:
        return
    parent = db.get(PaperTask, parent_id)
    if not parent or parent.status in {"cancelled", "failed", "succeeded"}:
        return
    children = db.scalars(select(PaperTask).where(
        PaperTask.operation == REEMBED_PAPER_OPERATION,
        PaperTask.details["parent_task_id"].astext == parent_id,
    )).all()
    completed = sum(item.status == "succeeded" for item in children)
    failed = sum(item.status in {"failed", "cancelled"} for item in children)
    parent.progress = completed / len(children) if children else 1.0
    parent.details = {**dict(parent.details or {}), "completed_papers": completed, "failed_papers": failed}
    if failed:
        parent.status, parent.finished_at = "failed", datetime.now(UTC)
        parent.details = {**dict(parent.details or {}), "stage": "failed"}
        db.commit()
        return
    if completed == len(children):
        _finish_embedding_rebuild(db, parent)
    else:
        db.commit()


def _finish_embedding_rebuild(db, parent: PaperTask) -> None:
    target_model = (parent.details or {}).get("target_model")
    db.execute(delete(EvidenceEmbedding).where(EvidenceEmbedding.embedding_model != target_model))
    parent.status, parent.progress, parent.finished_at, parent.error_message = "succeeded", 1.0, datetime.now(UTC), None
    parent.details = {**dict(parent.details or {}), "stage": "succeeded"}
    db.commit()
    for paused in db.scalars(select(PaperTask).where(PaperTask.operation == PROCESS_OPERATION, PaperTask.status == "paused")).all():
        details = dict(paused.details or {})
        queue = dict(details.get("queue") or {})
        queue.update(job_id=new_id("job"), delivery_status="pending")
        paused.status, paused.details = "queued", {**details, "stage": "embedding_resume", "resume_from": "embedding", "queue": queue}
        enqueue_submitted_process(db, paused)


def _embedding_rebuild_active(db) -> bool:
    task = db.scalar(
        select(PaperTask).where(PaperTask.operation == REEMBED_OPERATION)
        .order_by(PaperTask.created_at.desc()).limit(1)
    )
    return bool(task and task.status != "succeeded")


def _resume_embedding_task(db, task: PaperTask, paper: Paper) -> None:
    task.status, task.started_at, task.error_message = "running", datetime.now(UTC), None
    task.details = {**dict(task.details or {}), "stage": "embedding"}
    db.commit()
    result = EmbeddingIndexService(db, get_settings()).build_for_paper(paper)
    PaperSearchIndexService(db, get_settings()).build_for_paper(paper, include_embedding=True)
    task.details = {**dict(task.details or {}), "stage": "succeeded", "result": {"embedded": result.embedded_count, "skipped": result.skipped_count}}
    task.status, task.progress, task.finished_at = "succeeded", 1.0, datetime.now(UTC)
    task.error_message = None
    _finish_batch_item(db, task, "succeeded")
    db.commit()


def _finish_batch_item(
    db, task: PaperTask, status: str, error_message: str | None = None
) -> list[str]:
    next_tasks = []
    batch_ids = set()
    for item_id in _batch_item_ids(task):
        item = db.get(ImportBatchItem, item_id)
        if not item or item.status == status:
            continue
        item.status, item.error_message = status, error_message
        item.details = {
            **dict(getattr(item, "details", None) or {}),
            "stage": "ready" if status == "succeeded" else status,
            "task_id": getattr(task, "task_id", None),
        }
        batch = db.get(ImportBatch, item.batch_id)
        if not batch:
            continue
        batch_ids.add(batch.batch_id)
        if status == "succeeded":
            batch.completed_items += 1
        elif status in {"failed", "cancelled"}:
            batch.failed_items += 1
    for batch_id in batch_ids:
        batch = db.get(ImportBatch, batch_id)
        pending = db.scalar(
            select(ImportBatchItem.item_id)
            .where(
                ImportBatchItem.batch_id == batch_id,
                ImportBatchItem.status.in_(
                    ("pending", "queued", "running", "retrying")
                ),
            )
            .limit(1)
        )
        if not pending:
            batch.status = (
                "partial_failed"
                if batch.failed_items and batch.completed_items
                else "failed"
                if batch.failed_items
                else "succeeded"
            )
    return next_tasks


def cancel_process_task(db, task: PaperTask) -> list[str]:
    task.cancel_requested_at = datetime.now(UTC)
    if task.status not in {"queued", "retrying"}:
        return []
    task.status, task.finished_at = "cancelled", datetime.now(UTC)
    task.error_message = "Task was cancelled by an administrator."
    details = dict(task.details or {})
    queue = dict(details.get("queue") or {})
    queue.update(delivery_status="cancelled", error_code="TASK_CANCELLED")
    queue.pop("next_enqueue_at", None)
    task.details = {**details, "stage": "cancelled", "queue": queue}
    return _finish_batch_item(db, task, "cancelled", task.error_message)


def cancel_embedding_rebuild(db, task: PaperTask) -> None:
    cancel_process_task(db, task)
    for child in db.scalars(select(PaperTask).where(
        PaperTask.operation == REEMBED_PAPER_OPERATION,
        PaperTask.details["parent_task_id"].astext == task.task_id,
        PaperTask.status.in_(ACTIVE_PROCESS_STATUSES),
    )).all():
        cancel_process_task(db, child)


def release_waiting_embedding_repairs(db) -> list[str]:
    """Release repair successors only after the cancelled owner is terminal."""
    released = []
    for repair in db.scalars(
        select(PaperTask).where(
            PaperTask.operation == EMBEDDING_REPAIR_OPERATION,
            PaperTask.status == "paused",
        )
    ).all():
        predecessor_id = (repair.details or {}).get("wait_for_task_id")
        predecessor = db.get(PaperTask, predecessor_id) if predecessor_id else None
        if not predecessor or predecessor.status not in TERMINAL_OWNER_STATUSES:
            continue
        details = dict(repair.details or {})
        queue = dict(details.get("queue") or {})
        queue.update(delivery_status="pending", job_id=repair.task_id)
        repair.status = "queued"
        repair.details = {**details, "stage": "queued", "queue": queue}
        released.append(repair.task_id)
    return released


def _retry_details(details: dict, exc: Exception, job, provider: str | None) -> dict:
    classified = classify_external_error(exc)
    retries_left = int(getattr(job, "retries_left", 0) or 0)
    retry_index = max(0, len(RQ_RETRY_INTERVALS) - retries_left)
    result = {
        **details,
        "last_error": str(exc),
        "error_code": classified.code,
        "provider": getattr(exc, "provider", None) or provider,
        "credential_id": getattr(exc, "credential_id", None),
        "max_attempts": max(
            int(details.get("max_attempts", 0) or 0),
            int(details.get("attempt", 0) or 0) + retries_left,
        ),
    }
    if retries_left:
        delay = RQ_RETRY_INTERVALS[min(retry_index, len(RQ_RETRY_INTERVALS) - 1)]
        result["next_retry_at"] = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
    else:
        result.pop("next_retry_at", None)
    return result


def _error_code(exc: Exception) -> str:
    return classify_external_error(exc).code


def _is_retryable(exc: Exception) -> bool:
    return classify_external_error(exc).retryable


def _start_attempt(
    db, owner_type: str, owner_id: str, attempt: int, stage: str, provider: str | None
):
    row = ExecutionAttempt(
        attempt_id=new_id("attempt"),
        owner_type=owner_type,
        owner_id=owner_id,
        attempt=attempt,
        stage=stage,
        status="running",
        provider=provider,
    )
    db.add(row)
    db.commit()
    return row


def _finish_attempt(row: ExecutionAttempt, status: str, stage: str) -> None:
    row.status, row.stage, row.finished_at = status, stage, datetime.now(UTC)


def _fail_attempt(
    db,
    owner_type: str,
    owner_id: str,
    attempt: int,
    exc: Exception,
    retrying: bool,
    next_retry_at: str | None,
) -> None:
    row = db.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.owner_type == owner_type,
            ExecutionAttempt.owner_id == owner_id,
            ExecutionAttempt.attempt == attempt,
        )
    )
    if row:
        row.status = "retrying" if retrying else "failed"
        row.stage = "retrying" if retrying else "failed"
        row.error_code = _error_code(exc)
        row.error_message = str(exc)
        row.next_retry_at = datetime.fromisoformat(next_retry_at) if next_retry_at else None
        row.finished_at = datetime.now(UTC)


def _task_stage(
    db, task: PaperTask, attempt: ExecutionAttempt, stage: str, progress: float
) -> None:
    _refresh_and_assert_current_job(db, task)
    _check_cancel(task)
    task.progress = progress
    task.stage = stage
    task.details = {**dict(task.details or {}), "stage": stage}
    attempt.stage = stage
    db.commit()


def _check_cancel(task: PaperTask) -> None:
    if task.cancel_requested_at:
        error = RuntimeError("Task cancellation was requested.")
        error.retryable = False
        error.error_code = "TASK_CANCELLED"
        raise error


def _batch_item_ids(task: PaperTask) -> list[str]:
    details = task.details or {}
    values = list(details.get("batch_item_ids") or [])
    if details.get("batch_item_id") and details["batch_item_id"] not in values:
        values.append(details["batch_item_id"])
    return values


def _mark_batch_item(db, task: PaperTask, status: str, error_message: str | None = None) -> None:
    for item_id in _batch_item_ids(task):
        item = db.get(ImportBatchItem, item_id)
        if item and item.status not in {"succeeded", "failed", "cancelled"}:
            item.status = status
            item.error_message = error_message
            item.details = {
                **dict(item.details or {}),
                "stage": (task.details or {}).get("stage"),
                "queue": (task.details or {}).get("queue"),
                "task_id": task.task_id,
            }


def _record_enqueue_failure(db, task: PaperTask, exc: Exception) -> list[str]:
    terminal = _record_owner_enqueue_failure(task, exc, retryable=True)
    if not terminal:
        _mark_batch_item(db, task, "retrying", task.error_message)
        return []
    paper = db.get(Paper, task.paper_id)
    if paper:
        paper.parse_status = "failed"
    return _finish_batch_item(db, task, "failed", task.error_message)


def _record_enqueue_terminal(
    db, task: PaperTask, exc: Exception, error_code: str | None = None
) -> list[str]:
    _record_owner_enqueue_failure(task, exc, retryable=False, error_code=error_code)
    paper = db.get(Paper, task.paper_id)
    if paper:
        paper.parse_status = "failed"
    return _finish_batch_item(db, task, "failed", task.error_message)


def _record_enqueue_success(owner, job_id: str) -> None:
    details = dict(owner.details or {})
    queue = dict(details.get("queue") or {})
    for key in ("next_enqueue_at", "error_code", "last_error"):
        queue.pop(key, None)
    queue.update(job_id=job_id, delivery_status="enqueued")
    owner.details = {**details, "stage": "queued", "queue": queue}
    if owner.status == "retrying":
        owner.status = "queued"
    owner.error_message = None
    owner.finished_at = None


def _record_owner_enqueue_failure(
    owner,
    exc: Exception,
    *,
    retryable: bool,
    error_code: str | None = None,
) -> bool:
    details = dict(owner.details or {})
    queue = dict(details.get("queue") or {})
    attempt = int(queue.get("enqueue_attempt", 0)) + 1
    code = error_code or getattr(exc, "error_code", None) or "QUEUE_UNAVAILABLE"
    queue.update(
        enqueue_attempt=attempt,
        delivery_status=(
            "retrying" if retryable and attempt < QUEUE_ENQUEUE_MAX_FAILURES else "failed"
        ),
        error_code=code,
        last_error=str(exc),
    )
    owner.error_message = f"Queue delivery failed: {exc}"
    if retryable and attempt < QUEUE_ENQUEUE_MAX_FAILURES:
        queue["next_enqueue_at"] = (
            datetime.now(UTC) + timedelta(seconds=QUEUE_ENQUEUE_RETRY_INTERVALS[attempt - 1])
        ).isoformat()
        owner.status = "retrying"
        owner.details = {**details, "stage": "retrying", "queue": queue}
        return False
    queue.pop("next_enqueue_at", None)
    owner.status, owner.finished_at = "failed", datetime.now(UTC)
    owner.details = {
        **details,
        "stage": "failed",
        "failed_stage": details.get("stage", "queued"),
        "queue": queue,
    }
    return True


def _can_start_owner(owner, job) -> bool:
    if not owner or owner.status not in {"queued", "retrying", "running", "paused"}:
        return False
    expected = str(((owner.details or {}).get("queue") or {}).get("job_id") or "")
    return not job or not expected or job.id == expected


def _refresh_and_assert_current_job(db, owner) -> None:
    db.refresh(owner, with_for_update=True)
    if owner.status in TERMINAL_OWNER_STATUSES:
        raise StaleJobError("The database owner is already terminal.")
    job = get_current_job()
    expected = str(((owner.details or {}).get("queue") or {}).get("job_id") or "")
    if job and expected and job.id != expected:
        raise StaleJobError(f"RQ job {job.id} no longer owns this database task.")


def watchdog() -> dict:
    settings = get_settings()
    connection = redis.Redis.from_url(settings.redis_url)
    lock = connection.lock(
        WATCHDOG_LOCK_KEY, timeout=max(60, settings.task_orphan_grace_seconds)
    )
    if not lock.acquire(blocking=False):
        return {"skipped": "already_running"}
    try:
        return _watchdog_once(connection, settings)
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:
            pass


def _watchdog_once(connection, settings) -> dict:
    started = {
        job_id
        for name in QUEUE_NAMES
        for job_id in StartedJobRegistry(name, connection=connection).get_job_ids()
    }
    scheduled = {
        job_id: ScheduledJobRegistry(name, connection=connection)
        for name in QUEUE_NAMES
        for job_id in ScheduledJobRegistry(name, connection=connection).get_job_ids()
    }
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.task_orphan_grace_seconds)
    db = SessionLocal()
    recovered_ids, workflow_recovered_ids, enqueue_retry_ids, workflow_retry_ids = [], [], [], []
    promoted, reconciled = [], []
    try:
        enqueue_retry_ids.extend(release_waiting_embedding_repairs(db))
        for task in db.scalars(
            select(PaperTask)
            .where(
                PaperTask.pipeline_version == 1,
                PaperTask.status == "running",
                PaperTask.updated_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        ).all():
            details = dict(task.details or {})
            queue = dict(details.get("queue") or {})
            if str(queue.get("job_id") or task.task_id) in started:
                continue
            _recover_orphan(task, task.task_id, "task")
            recovered_ids.append(task.task_id)
        now = datetime.now(UTC)
        for task in db.scalars(
            select(PaperTask).where(
                PaperTask.pipeline_version == 1,
                PaperTask.status.in_(("queued", "retrying")),
            )
        ).all():
            queue = (task.details or {}).get("queue") or {}
            job_id = str(queue.get("job_id") or task.task_id)
            next_retry = queue.get("next_retry_at")
            if (
                job_id in scheduled
                and next_retry
                and _delivery_due(next_retry, now - timedelta(seconds=60))
            ):
                scheduled[job_id].requeue(job_id)
                promoted.append(task.task_id)
            if _delivery_retry_due(queue, task.updated_at, now, cutoff):
                enqueue_retry_ids.append(task.task_id)
        for run in db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.status == "running", WorkflowRun.updated_at < cutoff)
            .with_for_update(skip_locked=True)
        ).all():
            queue = dict((run.details or {}).get("queue") or {})
            if str(queue.get("job_id") or run.run_id) in started:
                continue
            _recover_orphan(run, run.run_id, "workflow")
            workflow_recovered_ids.append(run.run_id)
        for run in db.scalars(
            select(WorkflowRun).where(WorkflowRun.status.in_(("queued", "retrying")))
        ).all():
            queue = (run.details or {}).get("queue") or {}
            job_id = str(queue.get("job_id") or run.run_id)
            next_retry = queue.get("next_retry_at")
            if (
                job_id in scheduled
                and next_retry
                and _delivery_due(next_retry, now - timedelta(seconds=60))
            ):
                scheduled[job_id].requeue(job_id)
                promoted.append(run.run_id)
            if _delivery_retry_due(queue, run.updated_at, now, cutoff):
                workflow_retry_ids.append(run.run_id)
        for batch in db.scalars(
            select(ImportBatch).where(ImportBatch.status.in_(("processing", "failed")))
        ).all():
            active_tasks = db.scalars(
                select(PaperTask).where(
                    PaperTask.details["batch_id"].astext == batch.batch_id,
                    PaperTask.status.in_(("queued", "running", "retrying")),
                )
            ).all()
            for task in active_tasks:
                item_id = (task.details or {}).get("batch_item_id")
                item = db.get(ImportBatchItem, item_id) if item_id else None
                if item:
                    item.status = "running" if task.status == "running" else "retrying"
                    item.error_message = task.error_message
            if active_tasks:
                batch.status = "processing"
            db.flush()
            rows = db.execute(
                select(ImportBatchItem.status, func.count())
                .where(ImportBatchItem.batch_id == batch.batch_id)
                .group_by(ImportBatchItem.status)
            ).all()
            counts = dict(rows)
            batch.completed_items = int(counts.get("succeeded", 0))
            batch.failed_items = int(counts.get("failed", 0)) + int(counts.get("cancelled", 0))
            terminal = batch.completed_items + batch.failed_items
            if terminal == batch.total_items:
                batch.status = "failed" if batch.failed_items else "succeeded"
                reconciled.append(batch.batch_id)
        db.commit()
    finally:
        db.close()
    recovered_success, enqueue_retried = [], []
    workflow_recovered, workflow_enqueue_retried, enqueue_failed = [], [], []
    for task_id in [*recovered_ids, *enqueue_retry_ids]:
        db = SessionLocal()
        try:
            task = db.get(PaperTask, task_id)
            if task and task.status in {"queued", "retrying"}:
                enqueue_submitted_process(db, task)
                delivery = ((task.details or {}).get("queue") or {}).get("delivery_status")
                if delivery in {"enqueued", "started"}:
                    target = recovered_success if task_id in recovered_ids else enqueue_retried
                    target.append(task_id)
                elif task.status == "failed":
                    enqueue_failed.append(task_id)
        finally:
            db.close()
    for run_id in [*workflow_recovered_ids, *workflow_retry_ids]:
        db = SessionLocal()
        try:
            run = db.get(WorkflowRun, run_id)
            if run and run.status == "retrying":
                enqueue_submitted_workflow(db, run)
                delivery = ((run.details or {}).get("queue") or {}).get("delivery_status")
                if delivery in {"enqueued", "started"}:
                    target = (
                        workflow_recovered
                        if run_id in workflow_recovered_ids
                        else workflow_enqueue_retried
                    )
                    target.append(run_id)
                elif run.status == "failed":
                    enqueue_failed.append(run_id)
        finally:
            db.close()
    return {
        "recovered": recovered_success,
        "workflow_recovered": workflow_recovered,
        "interrupted": [],
        "promoted": promoted,
        "enqueue_retried": enqueue_retried,
        "workflow_enqueue_retried": workflow_enqueue_retried,
        "enqueue_failed": enqueue_failed,
        "reconciled_batches": reconciled,
    }


def _delivery_due(value: str, cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(value) <= cutoff
    except (TypeError, ValueError):
        return True


def _delivery_retry_due(
    queue: dict, updated_at: datetime | None, now: datetime, cutoff: datetime
) -> bool:
    next_enqueue = queue.get("next_enqueue_at")
    if (
        (
            queue.get("delivery_status") == "retrying"
            or queue.get("error_code") == "QUEUE_UNAVAILABLE"
        )
        and next_enqueue
        and _delivery_due(next_enqueue, now)
    ):
        return True
    return queue.get("delivery_status") == "pending" and bool(updated_at and updated_at <= cutoff)


def _recover_orphan(owner, owner_id: str, kind: str) -> None:
    details = dict(owner.details or {})
    queue = dict(details.get("queue") or {})
    generation = int(queue.get("recovery_generation", 0)) + 1
    queue.update(
        job_id=f"{owner_id}-recovery-{generation}",
        recovery_generation=generation,
        delivery_status="pending",
        enqueue_attempt=0,
        error_code=f"ORPHAN_{kind.upper()}_RECOVERED",
        last_error=f"Worker heartbeat disappeared; {kind} was recovered.",
    )
    queue.pop("next_enqueue_at", None)
    owner.status = "retrying"
    if isinstance(owner, PaperTask):
        owner.stage = "retrying"
        owner.error_code = f"ORPHAN_{kind.upper()}_RECOVERED"
        owner.error_domain = "platform"
    owner.details = {**details, "stage": "retrying", "queue": queue}


def watchdog_tick() -> dict:
    try:
        result = watchdog()
        flush_api_metrics()
        evaluate_alerts()
        return result
    except Exception as exc:
        logger.exception("Maintenance watchdog tick failed")
        return {"error_code": "WATCHDOG_TICK_FAILED", "error_type": type(exc).__name__}


def ensure_maintenance_jobs() -> None:
    connection = redis.Redis.from_url(get_settings().redis_url)
    _ensure_repeating_job(
        connection, "maintenance_watchdog", watchdog_tick, times=525600, interval=60
    )
    _ensure_repeating_job(
        connection,
        "maintenance_provider_metrics",
        aggregate_provider_metrics,
        times=8760,
        interval=3600,
    )
    settings = get_settings()
    if settings.harvest_enabled:
        from app.services.harvest_tasks import resume_harvest_jobs

        _ensure_repeating_job(
            connection,
            "maintenance_harvest_dispatcher",
            resume_harvest_jobs,
            times=525600,
            interval=60,
        )
    if settings.pipeline_enabled:
        from app.services.pipeline_dispatcher import dispatch_pipeline_once

        _ensure_repeating_job(
            connection,
            "maintenance_pipeline_dispatcher",
            dispatch_pipeline_once,
            times=15768000,
            interval=settings.pipeline_dispatch_interval_seconds,
        )


def _ensure_repeating_job(connection, job_id: str, function, *, times: int, interval: int) -> None:
    try:
        job = Job.fetch(job_id, connection=connection)
    except NoSuchJobError:
        job = None
    if job and job.get_status() in ACTIVE_JOB_STATUSES:
        return
    if job:
        job.delete(remove_from_queue=True)
    try:
        Queue(MAINTENANCE_QUEUE, connection=connection).enqueue(
            function,
            job_id=job_id,
            repeat=Repeat(times=times, interval=interval),
            unique=True,
        )
    except DuplicateJobError:
        pass
