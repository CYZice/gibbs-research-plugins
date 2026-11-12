from __future__ import annotations

import json
import random
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread

from rq import get_current_job
from sqlalchemy import select

from app.core.config import get_settings
from app.core.ids import new_id
from app.db.session import SessionLocal
from app.models.evidence_representation import EvidenceRepresentation
from app.models.evidence_unit import EvidenceUnit
from app.models.harvest import CorpusDocument, HarvestJob, HarvestJobItem
from app.models.import_batch import ImportBatchItem
from app.models.operational_control import ExecutionAttempt
from app.models.paper import Paper
from app.models.paper_content import PaperContent
from app.models.paper_search_document import PaperSearchDocument
from app.models.paper_task import PaperTask
from app.services.embedding_index_service import EmbeddingIndexService
from app.services.enrichment_service import EnrichmentService
from app.services.evidence_builder import EvidenceBuilderService
from app.services.external_errors import classify_external_error
from app.services.metadata_enrichment_service import MetadataEnrichmentService
from app.services.mineru_service import MinerUService
from app.services.paper_search_service import INDEX_VERSION, PaperSearchIndexService
from app.services.pipeline_service import (
    PIPELINE_STAGE_ORDER,
    PipelineInvariantError,
    cancel_owned_stage_and_root,
    claim_stage_task,
    complete_stage_and_schedule_successors,
    fail_stage_and_root,
    release_stage_run,
    renew_stage_run,
)
from app.services.remote_fetch_service import RemoteFetchService
from app.services.staged_artifact_service import ImportRegistrationService

RUN_LEASE_SECONDS = {
    "remote_fetch": 300,
    "register": 300,
    "parse": 300,
    "evidence": 1800,
    "resource_context": 1800,
    "vectorize": 900,
    "index": 300,
}
MINERU_POLL_SECONDS = 5


class PipelineCancelledError(RuntimeError):
    error_code = "TASK_CANCELLED"
    retryable = False


def execute_pipeline_stage(task_id: str, delivery_generation: int) -> None:
    job = get_current_job()
    worker_id = str(getattr(job, "worker_name", None) or "unknown")
    run_token = new_id("run")
    db = SessionLocal()
    try:
        task = claim_stage_task(
            db,
            task_id=task_id,
            delivery_generation=delivery_generation,
            run_token=run_token,
            worker_id=worker_id,
            run_lease_until=datetime.now(UTC)
            + timedelta(seconds=RUN_LEASE_SECONDS.get("parse", 300)),
        )
        if not task:
            db.rollback()
            return
        if task.operation not in PIPELINE_STAGE_ORDER:
            db.rollback()
            return
        task.run_lease_until = datetime.now(UTC) + timedelta(
            seconds=RUN_LEASE_SECONDS[task.operation]
        )
        task.error_code = None
        task.error_domain = None
        task.error_message = None
        root = db.get(PaperTask, task.root_task_id)
        if not root or (
            root.pipeline_version != task.pipeline_version
            or root.pipeline_generation != task.pipeline_generation
        ):
            db.rollback()
            return
        if root.cancel_requested_at:
            cancel_owned_stage_and_root(
                db, task_id=task_id, run_token=run_token
            )
            db.commit()
            return
        root.status = "running"
        root.stage = task.operation
        root.started_at = root.started_at or datetime.now(UTC)
        root.error_code = None
        root.error_domain = None
        root.error_message = None
        task.input_fingerprint = _stage_fingerprint(db, task, output=False)
        if task.batch_item_id:
            item = db.get(ImportBatchItem, task.batch_item_id)
            if item:
                item.status = "running"
                item.current_stage = task.operation
        attempt = ExecutionAttempt(
            attempt_id=new_id("attempt"),
            owner_type="task",
            owner_id=task.task_id,
            attempt=task.attempt_count,
            stage=task.operation,
            status="running",
            provider=task.provider,
            run_token=run_token,
            worker_id=worker_id,
            input_fingerprint=task.input_fingerprint,
            details={"action": task.next_action},
        )
        db.add(attempt)
        db.commit()

        with _stage_heartbeat(
            task_id,
            run_token,
            RUN_LEASE_SECONDS[task.operation],
        ):
            {
                "remote_fetch": _run_remote_fetch,
                "register": _run_register,
                "parse": _run_parse,
                "evidence": _run_evidence,
                "resource_context": _run_resource_context,
                "vectorize": _run_vectorize,
                "index": _run_index,
            }[task.operation](db, task_id, run_token, attempt.attempt_id)
    except Exception as exc:
        db.rollback()
        _handle_stage_failure(db, task_id, run_token, exc)
    finally:
        db.close()


def _run_remote_fetch(db, task_id: str, run_token: str, attempt_id: str) -> None:
    task = _owned_task(db, task_id, run_token)
    db.commit()
    result = RemoteFetchService(db, get_settings()).run(task)
    task = _owned_task(db, task_id, run_token)
    root = db.get(PaperTask, task.root_task_id)
    if not root:
        raise PipelineInvariantError("Pipeline root task was not found.")
    if result.paper:
        task.paper_id = result.paper.paper_id
        root.paper_id = result.paper.paper_id
    fetch_result = {
        "paper_id": result.paper.paper_id if result.paper else None,
        "staged_artifact_id": (
            result.artifact.artifact_id if result.artifact else None
        ),
        "duplicate": result.duplicate,
        "selected_source": result.selected_source,
    }
    task.details = {**dict(task.details or {}), "result": fetch_result}
    root.details = {
        **dict(root.details or {}),
        "fetch_result": fetch_result,
        "source_attempts": (task.details or {}).get("source_attempts", []),
        "selected_source": (task.details or {}).get("selected_source"),
    }
    _complete(db, task_id, run_token, attempt_id)


def _run_register(db, task_id: str, run_token: str, attempt_id: str) -> None:
    task = _owned_task(db, task_id, run_token)
    if not task.batch_item_id:
        raise PipelineInvariantError("Register task is missing its batch item.")
    registration = ImportRegistrationService(db, get_settings()).register(
        task.batch_item_id
    )
    paper = registration.paper
    duplicate = registration.duplicate
    completed_stages = registration.completed_stages
    task = _owned_task(db, task_id, run_token)
    root = db.get(PaperTask, task.root_task_id)
    if not root:
        raise PipelineInvariantError("Pipeline root task was not found.")
    task.paper_id = paper.paper_id
    root.paper_id = paper.paper_id
    result = {
        "paper_id": paper.paper_id,
        "duplicate": duplicate,
        "completed_stages": sorted(completed_stages),
    }
    task.details = {**dict(task.details or {}), "result": result}
    root.details = {
        **dict(root.details or {}),
        "result": result,
        "skipped_stages": sorted(
            set((root.details or {}).get("skipped_stages") or [])
            | completed_stages
        ),
    }
    harvest_item = db.scalar(
        select(HarvestJobItem).where(
            HarvestJobItem.import_batch_item_id == task.batch_item_id
        )
    )
    if harvest_item:
        harvest_item.status = "registered"
        harvest_item.registered_paper_id = paper.paper_id
        document = db.get(CorpusDocument, harvest_item.document_id)
        if document:
            document.registered_paper_id = paper.paper_id
        from app.services.harvest_service import HarvestService

        HarvestService(db, get_settings()).reconcile_job(
            db.get(HarvestJob, harvest_item.job_id)
        )
    _complete(db, task_id, run_token, attempt_id)


def _run_parse(db, task_id: str, run_token: str, attempt_id: str) -> None:
    task = _owned_task(db, task_id, run_token)
    paper = _paper_for_task(db, task)
    service = MinerUService(db, get_settings())
    action = task.next_action or "submit"
    force = bool((task.details or {}).get("force"))

    if action == "submit":
        cached = service.cached_result(paper, task, force=force)
        if cached is not None:
            _complete(db, task_id, run_token, attempt_id)
            return
        service.submit_parse(paper, task)
        _reschedule_parse(db, task_id, run_token, attempt_id, "poll")
        return

    if action == "poll":
        parsed = service.poll_parse_once(task)
        if parsed.get("state") in service.pending_states:
            _reschedule_parse(db, task_id, run_token, attempt_id, "poll")
        else:
            _reschedule_parse(
                db, task_id, run_token, attempt_id, "finalize", delay_seconds=0
            )
        return

    if action != "finalize":
        raise PipelineInvariantError(f"Unsupported parse action: {action}")
    service.finalize_parse(paper, task, service.poll_parse_once(task))
    try:
        metadata = MetadataEnrichmentService().enrich(
            paper,
            (
                Path(get_settings().storage_root).resolve()
                / "parsed"
                / paper.paper_id
            ).resolve(),
        )
        task.details = {**dict(task.details or {}), "metadata": metadata}
    except Exception as exc:
        task.details = {
            **dict(task.details or {}),
            "metadata": {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            },
        }
    _complete(db, task_id, run_token, attempt_id)


def _run_evidence(db, task_id: str, run_token: str, attempt_id: str) -> None:
    task = _owned_task(db, task_id, run_token)
    paper = _paper_for_task(db, task)
    options = dict((task.details or {}).get("options") or {})
    EvidenceBuilderService(db, get_settings()).build_for_paper(
        paper,
        task,
        force=bool(options.get("force")),
        full_rebuild=bool(options.get("full_rebuild")),
    )
    _complete(db, task_id, run_token, attempt_id)


def _run_resource_context(
    db, task_id: str, run_token: str, attempt_id: str
) -> None:
    task = _owned_task(db, task_id, run_token)
    paper = _paper_for_task(db, task)
    options = dict((task.details or {}).get("options") or {})
    EnrichmentService(db, get_settings()).enrich_paper(
        paper,
        task=task,
        force=bool(options.get("force")),
    )
    _complete(db, task_id, run_token, attempt_id)


def _run_vectorize(db, task_id: str, run_token: str, attempt_id: str) -> None:
    task = _owned_task(db, task_id, run_token)
    paper = _paper_for_task(db, task)
    options = dict((task.details or {}).get("options") or {})

    def record_progress(
        completed_batches: int,
        total_batches: int,
        embedded_count: int,
        target_count: int,
    ) -> None:
        owned = _owned_task(db, task_id, run_token)
        owned.details = {
            **dict(owned.details or {}),
            "vectorize_progress": {
                "completed_batches": completed_batches,
                "total_batches": total_batches,
                "embedded_count": embedded_count,
                "target_count": target_count,
            },
        }
        _check_cancel_and_renew(db, task_id, run_token)

    result = EmbeddingIndexService(db, get_settings()).build_for_paper(
        paper,
        force_rebuild=bool(options.get("force")),
        should_cancel=lambda: _check_cancel_and_renew(
            db, task_id, run_token
        ),
        on_progress=record_progress,
    )
    paper_index = PaperSearchIndexService(db, get_settings())
    paper_index.build_for_paper(paper, include_embedding=False)
    paper_index.embed_for_paper(paper)
    task = _owned_task(db, task_id, run_token)
    task.details = {
        **dict(task.details or {}),
        "result": {
            "embedded": result.embedded_count,
            "skipped": result.skipped_count,
        },
    }
    _complete(db, task_id, run_token, attempt_id)


def _run_index(db, task_id: str, run_token: str, attempt_id: str) -> None:
    task = _owned_task(db, task_id, run_token)
    paper = _paper_for_task(db, task)
    paper.index_status = "building"
    db.commit()
    PaperSearchIndexService(db, get_settings()).build_for_paper(
        paper, include_embedding=False
    )
    paper.index_status = "ready"
    _complete(db, task_id, run_token, attempt_id)


def _reschedule_parse(
    db,
    task_id: str,
    run_token: str,
    attempt_id: str,
    next_action: str,
    *,
    delay_seconds: int = MINERU_POLL_SECONDS,
) -> None:
    if _cancel_if_requested(db, task_id, run_token):
        attempt = db.get(ExecutionAttempt, attempt_id)
        if attempt:
            attempt.status = "cancelled"
            attempt.finished_at = datetime.now(UTC)
        db.commit()
        return
    attempt = db.get(ExecutionAttempt, attempt_id)
    if attempt:
        attempt.status = "succeeded"
        attempt.stage = f"parse_{next_action}"
        attempt.finished_at = datetime.now(UTC)
    if not release_stage_run(
        db,
        task_id=task_id,
        run_token=run_token,
        status="waiting_external",
        available_at=datetime.now(UTC) + timedelta(seconds=delay_seconds),
        next_action=next_action,
    ):
        raise PipelineInvariantError("Pipeline parse action lost execution ownership.")
    task = db.get(PaperTask, task_id)
    root = db.get(PaperTask, task.root_task_id) if task else None
    if root:
        root.status = "waiting_external"
        root.stage = "parse"
    db.commit()


def _complete(
    db, task_id: str, run_token: str, attempt_id: str
) -> None:
    if _cancel_if_requested(db, task_id, run_token):
        attempt = db.get(ExecutionAttempt, attempt_id)
        if attempt:
            attempt.status = "cancelled"
            attempt.finished_at = datetime.now(UTC)
        db.commit()
        return
    attempt = db.get(ExecutionAttempt, attempt_id)
    if attempt:
        attempt.status = "succeeded"
        attempt.stage = "succeeded"
        attempt.output_fingerprint = _stage_fingerprint(
            db, _owned_task(db, task_id, run_token), output=True
        )
        attempt.finished_at = datetime.now(UTC)
    complete_stage_and_schedule_successors(
        db,
        task_id=task_id,
        run_token=run_token,
        output_fingerprint=(
            attempt.output_fingerprint if attempt else None
        ),
    )
    db.commit()


def _handle_stage_failure(
    db, task_id: str, run_token: str, exc: Exception
) -> None:
    task = db.get(PaperTask, task_id)
    if not task or task.run_token != run_token:
        return
    classified = classify_external_error(exc)
    details = dict(task.details or {})
    details["last_error_code"] = classified.code
    backpressure = classified.code in {
        "PROVIDER_CAPACITY_WAIT",
        "PROVIDER_RATE_LIMITED",
        "EXTERNAL_RATE_LIMITED",
        "PROVIDER_CIRCUIT_OPEN",
    }
    failures = int(details.get("failure_count", 0))
    if backpressure:
        details["backpressure_count"] = int(
            details.get("backpressure_count", 0)
        ) + 1
    else:
        progress = int(
            dict(details.get("vectorize_progress") or {}).get(
                "embedded_count", 0
            )
        )
        previous_failure_progress = int(
            details.get("last_failure_progress", 0)
        )
        if task.operation == "vectorize" and progress > previous_failure_progress:
            failures = 1
            details["last_failure_progress"] = progress
        else:
            failures += 1
        details["failure_count"] = failures
    task.details = details
    task.error_code = classified.code
    task.error_domain = classified.domain
    task.error_message = str(exc)
    attempt = db.scalar(
        select(ExecutionAttempt)
        .where(
            ExecutionAttempt.owner_id == task_id,
            ExecutionAttempt.run_token == run_token,
        )
        .order_by(ExecutionAttempt.started_at.desc())
        .limit(1)
    )
    if classified.code == "TASK_CANCELLED":
        cancel_owned_stage_and_root(
            db, task_id=task_id, run_token=run_token
        )
        if attempt:
            attempt.status = "cancelled"
            attempt.error_code = classified.code
            attempt.error_message = str(exc)
            attempt.finished_at = datetime.now(UTC)
        db.commit()
        return
    if backpressure:
        base_delay = max(1.0, float(classified.retry_after or 5))
        delay = max(
            1,
            int(base_delay + random.uniform(0, min(5.0, base_delay * 0.2))),
        )
        release_stage_run(
            db,
            task_id=task_id,
            run_token=run_token,
            status="retry_wait",
            available_at=datetime.now(UTC) + timedelta(seconds=delay),
            next_action=task.next_action,
        )
        root = db.get(PaperTask, task.root_task_id)
        if root:
            root.status = "retrying"
            root.stage = task.operation
        if attempt:
            attempt.status = "retrying"
            attempt.stage = task.operation
            attempt.error_code = classified.code
            attempt.error_message = str(exc)
            attempt.next_retry_at = datetime.now(UTC) + timedelta(
                seconds=delay
            )
            attempt.finished_at = datetime.now(UTC)
        db.commit()
        return
    retryable = classified.retryable and failures < task.max_attempts
    if retryable:
        delay = int(
            classified.retry_after
            or min(1800, 30 * (2 ** max(0, failures - 1)))
        )
        next_action = task.next_action
        if task.operation == "parse":
            provider = dict((task.details or {}).get("provider") or {})
            if not provider.get("batch_id"):
                next_action = "submit"
        release_stage_run(
            db,
            task_id=task_id,
            run_token=run_token,
            status="retry_wait",
            available_at=datetime.now(UTC) + timedelta(seconds=delay),
            next_action=next_action,
        )
        root = db.get(PaperTask, task.root_task_id)
        if root:
            root.status = "retrying"
            root.stage = task.operation
    else:
        fail_stage_and_root(
            db,
            task_id=task_id,
            run_token=run_token,
            error_code=classified.code,
            error_domain=classified.domain,
            error_message=str(exc),
        )
        paper = db.get(Paper, task.paper_id) if task.paper_id else None
        if paper:
            {
                "parse": lambda: setattr(paper, "parse_status", "failed"),
                "evidence": lambda: setattr(paper, "evidence_status", "failed"),
                "resource_context": lambda: setattr(
                    paper, "multimodal_status", "failed"
                ),
                "vectorize": lambda: setattr(paper, "embedding_status", "failed"),
                "index": lambda: setattr(paper, "index_status", "failed"),
            }.get(task.operation, lambda: None)()
    if attempt:
        db.execute(
            ExecutionAttempt.__table__.update()
            .where(ExecutionAttempt.attempt_id == attempt.attempt_id)
            .values(
                status="retrying" if retryable else "failed",
                stage=task.operation,
                error_code=classified.code,
                error_message=str(exc),
                next_retry_at=(
                    datetime.now(UTC) + timedelta(seconds=delay)
                    if retryable
                    else None
                ),
                finished_at=datetime.now(UTC),
            )
        )
    db.commit()


def _owned_task(db, task_id: str, run_token: str) -> PaperTask:
    task = db.get(PaperTask, task_id)
    if not task or task.run_token != run_token:
        raise PipelineInvariantError("Pipeline stage is no longer owned by this run.")
    return task


def _paper_for_task(db, task: PaperTask) -> Paper:
    if not task.paper_id:
        raise PipelineInvariantError(
            f"Pipeline stage {task.operation} has no paper."
        )
    paper = db.get(Paper, task.paper_id)
    if not paper:
        raise PipelineInvariantError("Pipeline paper was not found.")
    return paper


def _cancel_if_requested(db, task_id: str, run_token: str) -> bool:
    task = _owned_task(db, task_id, run_token)
    root = db.get(PaperTask, task.root_task_id)
    if not root or not root.cancel_requested_at:
        return False
    cancel_owned_stage_and_root(db, task_id=task_id, run_token=run_token)
    return True


def _check_cancel_and_renew(db, task_id: str, run_token: str) -> None:
    task = _owned_task(db, task_id, run_token)
    root = db.get(PaperTask, task.root_task_id)
    if root and root.cancel_requested_at:
        raise PipelineCancelledError("Pipeline cancellation was requested.")
    if not renew_stage_run(
        db,
        task_id=task_id,
        run_token=run_token,
        lease_seconds=RUN_LEASE_SECONDS["vectorize"],
    ):
        raise PipelineInvariantError("Pipeline stage lost execution ownership.")
    db.commit()


@contextmanager
def _stage_heartbeat(
    task_id: str, run_token: str, lease_seconds: int
):
    stopped = Event()

    def heartbeat() -> None:
        interval = max(5, min(30, lease_seconds // 3))
        while not stopped.wait(interval):
            heartbeat_db = SessionLocal()
            try:
                alive = renew_stage_run(
                    heartbeat_db,
                    task_id=task_id,
                    run_token=run_token,
                    lease_seconds=lease_seconds,
                )
                heartbeat_db.commit()
                if not alive:
                    return
            except Exception:
                heartbeat_db.rollback()
            finally:
                heartbeat_db.close()

    thread = Thread(
        target=heartbeat,
        name=f"pipeline-heartbeat-{task_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


def _stage_fingerprint(db, task: PaperTask, *, output: bool) -> str:
    payload: dict = {
        "pipeline_version": task.pipeline_version,
        "operation": task.operation,
        "paper_id": task.paper_id,
    }
    if task.operation in {"remote_fetch", "register"}:
        payload["remote"] = (task.details or {}).get("remote")
        payload["batch_item_id"] = task.batch_item_id
        if output:
            payload["result"] = (task.details or {}).get("result")
    if task.paper_id:
        paper = db.get(Paper, task.paper_id)
        content = db.get(PaperContent, task.paper_id)
        representations = db.execute(
            select(
                EvidenceRepresentation.representation_id,
                EvidenceRepresentation.repr_type,
                EvidenceRepresentation.text_hash,
                EvidenceRepresentation.model_name,
                EvidenceRepresentation.prompt_version,
            )
            .join(
                EvidenceUnit,
                EvidenceUnit.evidence_id
                == EvidenceRepresentation.evidence_id,
            )
            .where(
                EvidenceUnit.paper_id == task.paper_id,
                EvidenceRepresentation.status == "success",
                EvidenceRepresentation.is_active.is_(True),
            )
            .order_by(EvidenceRepresentation.representation_id)
        ).all()
        document = db.get(PaperSearchDocument, task.paper_id)
        payload.update(
            {
                "pdf_sha256": paper.pdf_sha256 if paper else None,
                "content_sha256": (
                    content.content_sha256 if content else None
                ),
                "representations": [tuple(row) for row in representations],
            }
        )
        if task.operation == "parse":
            payload["parse_model"] = get_settings().mineru_model_version
            if output:
                payload["parse_version"] = (
                    paper.parse_version if paper else None
                )
        elif task.operation == "vectorize":
            payload["embedding_model"] = get_settings().embedding_model
            if output:
                payload["document_embedding_model"] = (
                    document.embedding_model if document else None
                )
        elif task.operation == "index":
            payload["index_version"] = INDEX_VERSION
            payload["document_embedding_model"] = (
                document.embedding_model if document else None
            )
            if output:
                payload["search_index_version"] = (
                    document.search_index_version if document else None
                )
                payload["search_tier"] = (
                    document.search_tier if document else None
                )
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
