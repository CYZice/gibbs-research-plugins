from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import ceil

import redis
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from rq import Queue, Worker
from rq.job import Job
from rq.registry import FailedJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.ids import new_id
from app.db.session import get_db
from app.models.evidence_embedding import EvidenceEmbedding
from app.models.evidence_representation import EvidenceRepresentation
from app.models.harvest import HarvestCredential
from app.models.import_batch import ImportBatch, ImportBatchItem
from app.models.operational_control import (
    ConfigAuditLog,
    ExecutionAttempt,
    OperationalAlert,
    ProviderCallEvent,
    ProviderConfig,
    ProviderCredential,
)
from app.models.paper import Paper
from app.models.paper_task import PaperTask
from app.schemas.harvest import HarvestCredentialCreate
from app.services.harvest_service import (
    HarvestCredentialService,
    HarvestError,
    credential_payload,
)
from app.services.operations_service import slo_summary
from app.services.pipeline_dispatcher import PIPELINE_QUEUE_NAMES
from app.services.pipeline_service import (
    PIPELINE_ROOT_OPERATION,
    PIPELINE_STAGE_ORDER,
    PIPELINE_VERSION,
    PipelineInvariantError,
    reconcile_pipeline_root,
    request_pipeline_cancel,
    retry_pipeline_from_stage,
)
from app.services.provider_control_service import ProviderControlService
from app.services.task_queue import (
    EMBEDDING_REPAIR_OPERATION,
    PROCESS_OPERATION,
    QUEUE_NAMES,
    REEMBED_OPERATION,
    REEMBED_PAPER_OPERATION,
    cancel_embedding_rebuild,
    cancel_process_task,
    enqueue_submitted_process,
    release_waiting_embedding_repairs,
    submit_embedding_rebuild,
    submit_embedding_repair,
    submit_process_task,
    watchdog,
)
from app.services.task_queue_constants import PIPELINE_INTAKE_PAUSE_KEY

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])
ROLES = {"embedding", "structured", "llm", "vision", "rerank"}
ROLE_DEFAULT_MODELS = {
    "embedding": "text-embedding-v4",
    "structured": "qwen-plus",
    "llm": "qwen-plus",
    "vision": "qwen-vl-plus",
    "rerank": "qwen3-rerank",
}
RETRYABLE_TERMINAL_STATUSES = {"failed", "interrupted", "cancelled"}


class ProviderPatch(BaseModel):
    provider: str = "dashscope"
    base_url: str
    model: str
    enabled: bool = True
    timeout_seconds: int = Field(ge=1, le=1800)
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    breaker_threshold: int = Field(default=5, ge=1, le=100)
    breaker_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    confirm_embedding_model_change: bool = False


class CredentialCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=8)
    weight: float = Field(default=1.0, gt=0, le=100)
    max_concurrency: int = Field(default=5, ge=1, le=100)


class CredentialPatch(BaseModel):
    alias: str | None = Field(default=None, min_length=1, max_length=100)
    api_key: str | None = Field(default=None, min_length=8)
    weight: float | None = Field(default=None, gt=0, le=100)
    max_concurrency: int | None = Field(default=None, ge=1, le=100)


class ProviderSetup(ProviderPatch):
    """Save a role configuration and its initial/additional keys as one transaction."""

    keys: list[CredentialCreate] = Field(default_factory=list, max_length=100)


class BulkTaskRetry(BaseModel):
    dry_run: bool = True
    include_non_retryable: bool = False
    confirm: bool = False


class EmbeddingRepairRequest(BaseModel):
    paper_ids: list[str] = Field(min_length=1, max_length=50)


class AlertAck(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


def _connection():
    return redis.Redis.from_url(get_settings().redis_url)


def _credential(
    row: ProviderCredential,
    calls: dict | None = None,
    active_leases: int = 0,
) -> dict:
    now = datetime.now(UTC)
    latest_activity = max(
        (value for value in (row.last_success_at, row.last_failure_at) if value is not None),
        default=None,
    )
    health = (
        "disabled"
        if row.status == "disabled"
        else "cooldown"
        if row.cooldown_until and row.cooldown_until > now
        else "unhealthy"
        if row.last_failure_at and row.last_failure_at > (row.last_success_at or datetime.min.replace(tzinfo=UTC))
        else "healthy"
        if row.last_success_at
        else "unverified"
    )
    return {
        "credential_id": row.credential_id,
        "alias": row.alias,
        "key_suffix": row.key_suffix,
        "status": row.status,
        "weight": row.weight,
        "max_concurrency": row.max_concurrency,
        "active_leases": active_leases,
        "remaining_capacity": max(0, row.max_concurrency - active_leases),
        "cooldown_until": row.cooldown_until,
        "consecutive_failures": row.consecutive_failures,
        "last_error_code": row.last_error_code,
        "last_success_at": row.last_success_at,
        "last_failure_at": row.last_failure_at,
        "health": health,
        "last_activity_at": latest_activity,
        "calls": calls or {"total": 0, "success": 0, "failure": 0},
        "created_at": row.created_at,
    }


def _new_provider_credential(
    config: ProviderConfig, body: CredentialCreate
) -> ProviderCredential:
    control = ProviderControlService(get_settings())
    try:
        encrypted = control.cipher.encrypt(body.api_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ADMIN_MASTER_KEY_NOT_CONFIGURED", "message": str(exc)},
        ) from exc
    return ProviderCredential(
        credential_id=new_id("credential"),
        provider_config_id=config.provider_config_id,
        alias=body.alias,
        encrypted_api_key=encrypted,
        key_suffix=body.api_key[-4:],
        key_fingerprint=control.fingerprint(body.api_key),
        weight=body.weight,
        max_concurrency=body.max_concurrency,
    )


def _latest_process_by_paper(rows: list[PaperTask]) -> dict[str, PaperTask]:
    latest: dict[str, PaperTask] = {}
    for task in rows:
        if task.paper_id:
            latest.setdefault(task.paper_id, task)
    return latest


def _latest_process_task(db: Session, paper_id: str) -> PaperTask | None:
    return db.scalar(
        select(PaperTask)
        .where(PaperTask.paper_id == paper_id, PaperTask.operation == PROCESS_OPERATION)
        .order_by(PaperTask.created_at.desc())
        .limit(1)
    )


def _audit(
    db: Session,
    request: Request,
    action: str,
    target_type: str,
    target_id: str,
    changes: dict | None = None,
) -> None:
    db.add(
        ConfigAuditLog(
            audit_id=new_id("audit"),
            action=action,
            target_type=target_type,
            target_id=target_id,
            changes=changes,
            request_id=request.headers.get("x-request-id"),
        )
    )


def _external_retryable(task: PaperTask) -> bool:
    if task.status == "interrupted":
        return True
    error_code = ((task.details or {}).get("queue") or {}).get("error_code")
    return error_code in {
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "QUEUE_UNAVAILABLE",
    }


def _provider_probe(config: ProviderConfig, key: str) -> tuple[object, str]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if config.role == "embedding":
        return requests.post(
            f"{config.base_url.rstrip('/')}/embeddings",
            headers=headers,
            json={"model": config.model, "input": ["healthcheck"]},
            timeout=(3, min(10, config.timeout_seconds)),
        ), "embedding"
    if config.role == "rerank":
        return requests.post(
            f"{config.base_url.rstrip('/')}/reranks",
            headers=headers,
            json={"model": config.model, "query": "healthcheck", "documents": ["ok"], "top_n": 1},
            timeout=(3, min(10, config.timeout_seconds)),
        ), "rerank"
    return requests.post(
        f"{config.base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json={"model": config.model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 2},
        timeout=(3, min(10, config.timeout_seconds)),
    ), "chat"


def _record_manual_probe(
    row: ProviderCredential, status_code: int | None, error_code: str | None
) -> None:
    now = datetime.now(UTC)
    if status_code is not None and status_code < 400:
        row.consecutive_failures = 0
        row.last_success_at = now
        row.last_error_code = None
        row.cooldown_until = None
        return
    row.consecutive_failures += 1
    row.last_failure_at = now
    row.last_error_code = error_code or "PROVIDER_UNAVAILABLE"
    if status_code in {401, 403}:
        row.status = "disabled"
    elif status_code == 429:
        row.cooldown_until = now + timedelta(seconds=60)


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    since = datetime.now(UTC) - timedelta(hours=24)
    task_rows = db.execute(select(PaperTask.status, func.count()).group_by(PaperTask.status)).all()
    batch_rows = db.execute(
        select(ImportBatch.status, func.count()).group_by(ImportBatch.status)
    ).all()
    events = db.scalars(
        select(ProviderCallEvent).where(ProviderCallEvent.created_at >= since)
    ).all()
    latencies = sorted(event.latency_ms for event in events)
    percentile = lambda value: (
        latencies[min(len(latencies) - 1, ceil(len(latencies) * value) - 1)] if latencies else 0
    )
    error_counts = {}
    for event in events:
        if event.error_code:
            error_counts[event.error_code] = error_counts.get(event.error_code, 0) + 1
    workers = Worker.all(connection=_connection())
    now = datetime.now(UTC)
    recent_batches = db.scalars(
        select(ImportBatch).order_by(ImportBatch.updated_at.desc()).limit(20)
    ).all()
    return {
        "tasks": dict(task_rows),
        "batches": dict(batch_rows),
        "provider_24h": {
            "calls": len(events),
            "success": sum(event.status == "success" for event in events),
            "errors": error_counts,
            "p50_ms": percentile(0.5),
            "p95_ms": percentile(0.95),
        },
        "workers": [
            {
                "name": worker.name,
                "state": str(worker.get_state()),
                "queues": [queue.name for queue in worker.queues],
                "last_heartbeat": worker.last_heartbeat,
                "healthy": bool(
                    worker.last_heartbeat and worker.last_heartbeat >= now - timedelta(seconds=90)
                ),
            }
            for worker in workers
        ],
        "recent_batches": [
            {
                "batch_id": batch.batch_id,
                "source": batch.source,
                "status": batch.status,
                "total_items": batch.total_items,
                "completed_items": batch.completed_items,
                "failed_items": batch.failed_items,
                "updated_at": batch.updated_at,
            }
            for batch in recent_batches
        ],
    }


def _pipeline_row(task: PaperTask, paper: Paper | None = None) -> dict:
    return {
        "task_id": task.task_id,
        "root_task_id": task.root_task_id,
        "paper_id": task.paper_id,
        "title": paper.title if paper else None,
        "batch_id": task.batch_id,
        "batch_item_id": task.batch_item_id,
        "operation": task.operation,
        "status": task.status,
        "stage": task.stage,
        "progress": task.progress,
        "queue_name": task.queue_name,
        "delivery_status": task.delivery_status,
        "delivery_generation": task.delivery_generation,
        "next_action": task.next_action,
        "available_at": task.available_at,
        "delivery_lease_until": task.delivery_lease_until,
        "run_lease_until": task.run_lease_until,
        "worker_id": task.worker_id,
        "heartbeat_at": task.heartbeat_at,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "pipeline_version": task.pipeline_version,
        "pipeline_generation": task.pipeline_generation,
        "priority": task.priority,
        "error_code": task.error_code,
        "error_domain": task.error_domain,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }


@router.get("/pipeline/overview")
def pipeline_overview(db: Session = Depends(get_db)):
    settings = get_settings()
    now = datetime.now(UTC)
    stage_counts = {
        operation: {}
        for operation in PIPELINE_STAGE_ORDER
    }
    for operation, status_value, count in db.execute(
        select(PaperTask.operation, PaperTask.status, func.count())
        .where(
            PaperTask.pipeline_version == PIPELINE_VERSION,
            PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
        )
        .group_by(PaperTask.operation, PaperTask.status)
    ).all():
        stage_counts[operation][status_value] = int(count)
    queue_backlog = {
        queue_name: int(count)
        for queue_name, count in db.execute(
            select(PaperTask.queue_name, func.count())
            .where(
                PaperTask.pipeline_version == PIPELINE_VERSION,
                PaperTask.delivery_status == "pending",
            )
            .group_by(PaperTask.queue_name)
        ).all()
        if queue_name
    }
    return {
        "enabled": settings.pipeline_enabled,
        "intake_paused": bool(
            _connection().get(PIPELINE_INTAKE_PAUSE_KEY)
        ),
        "stage_counts": stage_counts,
        "queue_backlog": queue_backlog,
        "overdue": int(
            db.scalar(
                select(func.count())
                .select_from(PaperTask)
                .where(
                    PaperTask.pipeline_version == PIPELINE_VERSION,
                    PaperTask.delivery_status == "pending",
                    PaperTask.available_at
                    < now - timedelta(seconds=settings.pipeline_overdue_seconds),
                )
            )
            or 0
        ),
        "expired_delivery_leases": int(
            db.scalar(
                select(func.count())
                .select_from(PaperTask)
                .where(
                    PaperTask.delivery_status == "dispatching",
                    PaperTask.delivery_lease_until < now,
                )
            )
            or 0
        ),
        "expired_run_leases": int(
            db.scalar(
                select(func.count())
                .select_from(PaperTask)
                .where(
                    PaperTask.run_token.is_not(None),
                    PaperTask.run_lease_until < now,
                )
            )
            or 0
        ),
        "expanding_batches": int(
            db.scalar(
                select(func.count())
                .select_from(ImportBatch)
                .where(ImportBatch.expansion_status.in_(("pending", "running")))
            )
            or 0
        ),
    }


@router.post("/pipeline/intake/pause")
def pause_pipeline_intake(
    request: Request, db: Session = Depends(get_db)
):
    _connection().set(PIPELINE_INTAKE_PAUSE_KEY, "1")
    _audit(db, request, "pipeline.intake.pause", "pipeline", str(PIPELINE_VERSION))
    db.commit()
    return {"paused": True}


@router.post("/pipeline/intake/resume")
def resume_pipeline_intake(
    request: Request, db: Session = Depends(get_db)
):
    _connection().delete(PIPELINE_INTAKE_PAUSE_KEY)
    _audit(db, request, "pipeline.intake.resume", "pipeline", str(PIPELINE_VERSION))
    db.commit()
    return {"paused": False}


@router.get("/pipelines")
def pipelines(
    status: str | None = None,
    stage: str | None = None,
    batch_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    size = min(max(page_size, 1), 100)
    statement = (
        select(PaperTask)
        .where(
            PaperTask.operation == PIPELINE_ROOT_OPERATION,
            PaperTask.pipeline_version == PIPELINE_VERSION,
        )
        .order_by(PaperTask.updated_at.desc())
    )
    if status:
        statement = statement.where(PaperTask.status == status)
    if stage:
        statement = statement.where(PaperTask.stage == stage)
    if batch_id:
        statement = statement.where(PaperTask.batch_id == batch_id)
    rows = db.scalars(
        statement.offset((max(page, 1) - 1) * size).limit(size)
    ).all()
    papers = {
        paper.paper_id: paper
        for paper in db.scalars(
            select(Paper).where(
                Paper.paper_id.in_(
                    [row.paper_id for row in rows if row.paper_id]
                )
            )
        ).all()
    } if any(row.paper_id for row in rows) else {}
    return {
        "items": [
            _pipeline_row(row, papers.get(row.paper_id))
            for row in rows
        ],
        "pagination": {"page": max(page, 1), "page_size": size},
    }


@router.get("/pipelines/{root_task_id}/stages")
def pipeline_stages(
    root_task_id: str, db: Session = Depends(get_db)
):
    root = db.get(PaperTask, root_task_id)
    if not root or root.operation != PIPELINE_ROOT_OPERATION:
        raise HTTPException(status_code=404, detail="Pipeline not found.")
    rows = db.scalars(
        select(PaperTask).where(
            PaperTask.root_task_id == root_task_id,
            PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
        )
    ).all()
    order = {stage: index for index, stage in enumerate(PIPELINE_STAGE_ORDER)}
    rows.sort(key=lambda row: order[row.operation])
    attempts = db.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.owner_id.in_([row.task_id for row in rows]))
        .order_by(ExecutionAttempt.started_at)
    ).all() if rows else []
    by_task: dict[str, list[dict]] = {}
    for attempt in attempts:
        by_task.setdefault(attempt.owner_id, []).append(
            {
                "attempt": attempt.attempt,
                "status": attempt.status,
                "stage": attempt.stage,
                "worker_id": attempt.worker_id,
                "error_code": attempt.error_code,
                "error_message": attempt.error_message,
                "next_retry_at": attempt.next_retry_at,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
            }
        )
    return {
        "root": _pipeline_row(root, db.get(Paper, root.paper_id) if root.paper_id else None),
        "stages": [
            {**_pipeline_row(row), "attempts": by_task.get(row.task_id, [])}
            for row in rows
        ],
    }


@router.post("/pipeline-stages/{task_id}/retry")
def retry_pipeline_stage(
    task_id: str, request: Request, db: Session = Depends(get_db)
):
    try:
        root = retry_pipeline_from_stage(db, task_id=task_id, priority=80)
    except PipelineInvariantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        db,
        request,
        "pipeline.stage.retry",
        "task",
        task_id,
        {"root_task_id": root.task_id, "stage": root.stage},
    )
    db.commit()
    return _pipeline_row(root)


@router.post("/pipelines/{root_task_id}/cancel")
def cancel_pipeline(
    root_task_id: str, request: Request, db: Session = Depends(get_db)
):
    try:
        root = request_pipeline_cancel(db, root_task_id=root_task_id)
    except PipelineInvariantError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(
        db,
        request,
        "pipeline.cancel",
        "task",
        root_task_id,
    )
    db.commit()
    return _pipeline_row(root)


@router.post("/pipelines/{root_task_id}/reconcile")
def reconcile_pipeline(
    root_task_id: str, request: Request, db: Session = Depends(get_db)
):
    try:
        root = reconcile_pipeline_root(db, root_task_id)
    except PipelineInvariantError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(
        db,
        request,
        "pipeline.reconcile",
        "task",
        root_task_id,
    )
    db.commit()
    return _pipeline_row(root)


def _alert_row(row: OperationalAlert) -> dict:
    return {
        "alert_id": row.alert_id, "fingerprint": row.fingerprint, "rule": row.rule,
        "severity": row.severity, "status": row.status, "summary": row.summary,
        "details": row.details or {}, "occurrence_count": row.occurrence_count,
        "first_seen_at": row.first_seen_at, "last_seen_at": row.last_seen_at,
        "acknowledged_at": row.acknowledged_at, "acknowledged_note": row.acknowledged_note,
        "resolved_at": row.resolved_at,
    }


@router.get("/slo")
def slo(window: str = "24h"):
    if window not in {"24h", "30d"}:
        raise HTTPException(status_code=400, detail="window must be 24h or 30d.")
    return slo_summary(24 if window == "24h" else 24 * 30)


@router.get("/alerts")
def alerts(status: str | None = None, severity: str | None = None, page: int = 1, page_size: int = 50, db: Session = Depends(get_db)):
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(status_code=400, detail="Invalid pagination.")
    statement = select(OperationalAlert).order_by(OperationalAlert.last_seen_at.desc())
    if status:
        statement = statement.where(OperationalAlert.status == status)
    if severity:
        statement = statement.where(OperationalAlert.severity == severity)
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = db.scalars(statement.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [_alert_row(row) for row in rows], "pagination": {"page": page, "page_size": page_size, "total": total}}


@router.get("/alerts/{alert_id}")
def alert_detail(alert_id: str, db: Session = Depends(get_db)):
    row = db.get(OperationalAlert, alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found.")
    return _alert_row(row)


@router.post("/alerts/{alert_id}/ack")
def acknowledge_alert(alert_id: str, body: AlertAck, request: Request, db: Session = Depends(get_db)):
    row = db.get(OperationalAlert, alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if row.status == "resolved":
        raise HTTPException(status_code=409, detail="Resolved alerts cannot be acknowledged.")
    row.status, row.acknowledged_at, row.acknowledged_note = "acknowledged", datetime.now(UTC), body.note
    _audit(db, request, "alert.acknowledge", "alert", alert_id, {"note": body.note})
    db.commit()
    return _alert_row(row)


@router.get("/processing")
def processing_tasks(
    status: str | None = None, page: int = 1, page_size: int = 100, db: Session = Depends(get_db)
):
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(status_code=400, detail="Invalid pagination.")
    rows = db.execute(
        select(PaperTask, Paper.title)
        .join(Paper, Paper.paper_id == PaperTask.paper_id)
        .where(PaperTask.operation == PROCESS_OPERATION)
        .order_by(PaperTask.paper_id, PaperTask.created_at.desc())
    ).all()
    latest = {}
    for task, title in rows:
        latest.setdefault(task.paper_id, (task, title))
    items = list(latest.values())
    recovered_ids = _recovered_process_ids(db)
    if status:
        items = [item for item in items if _effective_process_status(item[0], recovered_ids) == status]
    counts = {}
    for task, _title in latest.values():
        effective_status = _effective_process_status(task, recovered_ids)
        counts[effective_status] = counts.get(effective_status, 0) + 1
    total = len(items)
    items = items[(page - 1) * page_size : page * page_size]
    return {
        "counts": counts,
        "items": [
            {
                "task_id": task.task_id,
                "paper_id": task.paper_id,
                "title": title,
                "status": task.status,
                "effective_status": _effective_process_status(task, recovered_ids),
                "stage": "embedding_recovered"
                if _effective_process_status(task, recovered_ids) == "recovered"
                else (task.details or {}).get("failed_stage")
                if task.status in RETRYABLE_TERMINAL_STATUSES
                else task.stage or (task.details or {}).get("stage"),
                "progress": task.progress,
                "error_code": task.error_code or ((task.details or {}).get("queue") or {}).get("error_code"),
                "error_domain": task.error_domain,
                "last_completed_stage": task.last_completed_stage,
                "root_task_id": task.root_task_id,
                "error_message": task.error_message,
                "attempt": task.attempt_count or ((task.details or {}).get("queue") or {}).get("attempt", 0),
                "max_attempts": task.max_attempts or ((task.details or {}).get("queue") or {}).get("max_attempts", 0),
                "next_retry_at": task.next_retry_at or ((task.details or {}).get("queue") or {}).get("next_retry_at"),
                "recovery_action": "retry_later" if task.error_domain == "external" else "inspect" if task.error_code else None,
                "manual_retryable": task.status in RETRYABLE_TERMINAL_STATUSES and _effective_process_status(task, recovered_ids) != "recovered",
                "external_retryable": _external_retryable(task),
                "updated_at": task.updated_at,
            }
            for task, title in items
        ],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    }


@router.get("/queues")
def queues(db: Session = Depends(get_db)):
    connection = _connection()
    database = {
        name: {
            "pending": int(pending or 0),
            "dispatching": int(dispatching or 0),
            "enqueued": int(enqueued or 0),
            "oldest_available_at": oldest,
        }
        for name, pending, dispatching, enqueued, oldest in db.execute(
            select(
                PaperTask.queue_name,
                func.count().filter(PaperTask.delivery_status == "pending"),
                func.count().filter(PaperTask.delivery_status == "dispatching"),
                func.count().filter(PaperTask.delivery_status == "enqueued"),
                func.min(PaperTask.available_at).filter(
                    PaperTask.delivery_status == "pending"
                ),
            )
            .where(PaperTask.queue_name.in_(QUEUE_NAMES))
            .group_by(PaperTask.queue_name)
        ).all()
    }
    return {
        "items": [
            {
                "name": name,
                "queued": Queue(name, connection=connection).count,
                "started": len(StartedJobRegistry(name, connection=connection).get_job_ids()),
                "scheduled": len(ScheduledJobRegistry(name, connection=connection).get_job_ids()),
                "failed": len(FailedJobRegistry(name, connection=connection).get_job_ids()),
                "paused": bool(connection.get(f"admin:queue:paused:{name}")),
                "database": database.get(
                    name,
                    {
                        "pending": 0,
                        "dispatching": 0,
                        "enqueued": 0,
                        "oldest_available_at": None,
                    },
                ),
            }
            for name in QUEUE_NAMES
        ]
    }


@router.post("/queues/{name}/pause")
def pause_queue(name: str):
    if name not in QUEUE_NAMES:
        raise HTTPException(status_code=404, detail="Unknown queue.")
    connection = _connection()
    connection.set(f"admin:queue:paused:{name}", "1")
    if name in PIPELINE_QUEUE_NAMES:
        return {"name": name, "paused": True, "moved": 0}
    source, target = (
        Queue(name, connection=connection),
        Queue(f"{name}-paused", connection=connection),
    )
    for job_id in list(source.job_ids):
        job = Job.fetch(job_id, connection=connection)
        source.remove(job, delete_job=False)
        target.enqueue_job(job)
    return {"name": name, "paused": True}


@router.post("/queues/{name}/resume")
def resume_queue(name: str):
    if name not in QUEUE_NAMES:
        raise HTTPException(status_code=404, detail="Unknown queue.")
    connection = _connection()
    connection.delete(f"admin:queue:paused:{name}")
    if name in PIPELINE_QUEUE_NAMES:
        return {"name": name, "paused": False, "moved": 0}
    source, target = (
        Queue(f"{name}-paused", connection=connection),
        Queue(name, connection=connection),
    )
    for job_id in list(source.job_ids):
        job = Job.fetch(job_id, connection=connection)
        source.remove(job, delete_job=False)
        target.enqueue_job(job)
    return {"name": name, "paused": False}


@router.get("/import-batches/{batch_id}/items")
def batch_items(batch_id: str, page: int = 1, page_size: int = 50, db: Session = Depends(get_db)):
    if not db.get(ImportBatch, batch_id):
        raise HTTPException(status_code=404, detail="Import batch not found.")
    statement = (
        select(ImportBatchItem)
        .where(ImportBatchItem.batch_id == batch_id)
        .order_by(ImportBatchItem.created_at)
    )
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = db.scalars(statement.offset((page - 1) * page_size).limit(page_size)).all()
    item_ids = [row.item_id for row in rows]
    task_rows = (
        db.scalars(
            select(PaperTask)
            .where(PaperTask.batch_item_id.in_(item_ids))
            .order_by(PaperTask.created_at.desc())
        ).all()
        if item_ids
        else []
    )
    tasks = {}
    for task in task_rows:
        tasks.setdefault(task.batch_item_id, task)
    return {
        "items": [
            {
                "item_id": row.item_id,
                "source_key": row.source_key,
                "paper_id": row.paper_id,
                "status": row.status,
                "attempts": row.attempts,
                "error_message": row.error_message,
                "details": row.details,
                "task_id": row.pipeline_task_id
                or (tasks[row.item_id].task_id if row.item_id in tasks else None),
                "stage": row.status
                if row.status in {"succeeded", "failed"}
                else (
                    row.current_stage
                    or (
                        tasks[row.item_id].stage
                        if row.item_id in tasks
                        else None
                    )
                ),
                "queue": (tasks[row.item_id].details or {}).get("queue")
                if row.item_id in tasks
                else None,
            }
            for row in rows
        ],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    }


@router.post("/import-batches/{batch_id}/pause")
def pause_import_batch(
    batch_id: str, request: Request, db: Session = Depends(get_db)
):
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import batch not found.")
    if batch.status in {"completed", "completed_with_errors", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Import batch is already terminal.")
    batch.status = "paused"
    _audit(db, request, "batch.pause", "import_batch", batch_id)
    db.commit()
    return {"batch_id": batch_id, "status": batch.status}


@router.post("/import-batches/{batch_id}/resume")
def resume_import_batch(
    batch_id: str, request: Request, db: Session = Depends(get_db)
):
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import batch not found.")
    if batch.status != "paused":
        raise HTTPException(status_code=409, detail="Import batch is not paused.")
    batch.status = (
        "expanding"
        if batch.manifest_storage_key and not batch.intake_closed
        else "running"
    )
    _audit(db, request, "batch.resume", "import_batch", batch_id)
    db.commit()
    return {"batch_id": batch_id, "status": batch.status}


@router.post("/import-batches/{batch_id}/cancel")
def cancel_import_batch(
    batch_id: str, request: Request, db: Session = Depends(get_db)
):
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import batch not found.")
    batch.status = "cancelling"
    batch.intake_closed = True
    roots = db.scalars(
        select(PaperTask).where(
            PaperTask.batch_id == batch_id,
            PaperTask.operation == PIPELINE_ROOT_OPERATION,
            PaperTask.status.in_(
                ("pending", "running", "waiting_external", "retrying", "cancelling")
            ),
        )
    ).all()
    for root in roots:
        request_pipeline_cancel(db, root_task_id=root.task_id)
    batch.status = (
        "cancelling"
        if any(root.status == "cancelling" for root in roots)
        else "cancelled"
    )
    _audit(
        db,
        request,
        "batch.cancel",
        "import_batch",
        batch_id,
        {"pipelines": len(roots)},
    )
    db.commit()
    return {"batch_id": batch_id, "status": batch.status, "pipelines": len(roots)}


@router.post("/import-batches/{batch_id}/retry")
def retry_import_batch_pipeline(
    batch_id: str, request: Request, db: Session = Depends(get_db)
):
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import batch not found.")
    failed_stages = db.scalars(
        select(PaperTask)
        .where(
            PaperTask.batch_id == batch_id,
            PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
            PaperTask.status == "failed",
        )
        .order_by(PaperTask.created_at)
    ).all()
    retried = 0
    seen_roots = set()
    for stage in failed_stages:
        if stage.root_task_id in seen_roots:
            continue
        retry_pipeline_from_stage(db, task_id=stage.task_id)
        seen_roots.add(stage.root_task_id)
        retried += 1
    _audit(
        db,
        request,
        "batch.retry",
        "import_batch",
        batch_id,
        {"pipelines": retried},
    )
    db.commit()
    return {"batch_id": batch_id, "retried": retried, "status": batch.status}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    task = db.get(PaperTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.operation != PROCESS_OPERATION:
        raise HTTPException(status_code=409, detail="Only complete processing tasks can be retried.")
    latest = _latest_process_task(db, task.paper_id) if task.paper_id else None
    if latest is None or latest.task_id != task.task_id:
        raise HTTPException(status_code=409, detail="Historical or superseded tasks cannot be retried.")
    if _effective_process_status(task, _recovered_process_ids(db)) == "recovered":
        raise HTTPException(status_code=409, detail="Embedding repair already completed this processing task; full reprocessing is unnecessary.")
    if task.status not in RETRYABLE_TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Task is not in a retryable terminal state.")
    retry, created = submit_process_task(
        db, task.paper_id, provider="admin", details={"retry_of": task.task_id, "root_task_id": task.root_task_id or task.task_id}
    )
    if created:
        retry.root_task_id = task.root_task_id or task.task_id
        retry = enqueue_submitted_process(db, retry)
    _audit(
        db,
        request,
        "task.retry_process",
        "task",
        retry.task_id,
        {"retry_of": task.task_id},
    )
    db.commit()
    return {
        "task_id": retry.task_id,
        "retry_of": task.task_id,
        "already_active": not created,
    }


def _recovered_process_ids(db: Session) -> set[str]:
    return {
        str((repair.details or {}).get("source_task_id"))
        for repair in db.scalars(
            select(PaperTask).where(
                PaperTask.operation == EMBEDDING_REPAIR_OPERATION,
                PaperTask.status == "succeeded",
            )
        ).all()
        if (repair.details or {}).get("source_task_id")
    }


def _effective_process_status(task: PaperTask, recovered_ids: set[str]) -> str:
    recovery = (task.details or {}).get("recovery") or {}
    return "recovered" if task.task_id in recovered_ids or recovery.get("status") == "succeeded" else task.status


@router.post("/papers/{paper_id}/retry-process")
def retry_paper_process(paper_id: str, request: Request, db: Session = Depends(get_db)):
    if not db.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found.")
    latest = _latest_process_task(db, paper_id)
    if not latest:
        raise HTTPException(status_code=409, detail="Paper has no complete processing task to retry.")
    if _effective_process_status(latest, _recovered_process_ids(db)) == "recovered":
        raise HTTPException(status_code=409, detail="Embedding repair already completed this processing task; full reprocessing is unnecessary.")
    if latest.status not in RETRYABLE_TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Paper processing is not in a retryable terminal state.")
    retry, created = submit_process_task(
        db, paper_id, provider="admin", details={"retry_of": latest.task_id, "root_task_id": latest.root_task_id or latest.task_id}
    )
    if created:
        retry.root_task_id = latest.root_task_id or latest.task_id
    if created:
        retry = enqueue_submitted_process(db, retry)
    _audit(db, request, "paper.retry_process", "paper", paper_id, {"retry_of": latest.task_id})
    db.commit()
    return {"paper_id": paper_id, "task_id": retry.task_id, "already_active": not created}


@router.post("/tasks/retry-failed")
@router.post("/tasks/retry-interrupted")
def retry_interrupted_tasks(
    body: BulkTaskRetry, request: Request, db: Session = Depends(get_db)
):
    if body.include_non_retryable and not body.dry_run and not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Retrying all failures requires an explicit administrator confirmation.",
        )
    recovered = []
    if not body.dry_run:
        recovered = watchdog().get("recovered", [])
    rows = db.scalars(
        select(PaperTask)
        .where(PaperTask.operation == PROCESS_OPERATION)
        .order_by(PaperTask.created_at.desc())
    ).all()
    latest_by_paper = _latest_process_by_paper(rows)
    candidates = [
        task for task in latest_by_paper.values() if task.status in RETRYABLE_TERMINAL_STATUSES
    ]
    selected = [
        task
        for task in candidates
        if body.include_non_retryable or _external_retryable(task)
    ]
    titles = (
        dict(
            db.execute(
                select(Paper.paper_id, Paper.title).where(Paper.paper_id.in_(list(latest_by_paper)))
            ).all()
        )
        if latest_by_paper
        else {}
    )
    selected_ids = {task.task_id for task in selected}
    preview_items = []
    for task in candidates:
        queue = (task.details or {}).get("queue") or {}
        automatic = _external_retryable(task)
        preview_items.append(
            {
                "task_id": task.task_id,
                "paper_id": task.paper_id,
                "title": titles.get(task.paper_id or ""),
                "status": task.status,
                "stage": (task.details or {}).get("failed_stage")
                or (task.details or {}).get("stage"),
                "error_code": queue.get("error_code"),
                "selected": task.task_id in selected_ids,
                "external_retryable": automatic,
                "reason": "外部暂态错误" if automatic else "需在修复原因后确认重试",
            }
        )
    result = {
        "dry_run": body.dry_run,
        "include_non_retryable": body.include_non_retryable,
        "recovered_task_ids": recovered,
        "candidate_count": len(candidates),
        "retry_count": len(selected),
        "skipped_active_count": 0,
        "skipped_non_retryable_count": len(candidates) - len(selected),
        "historical_task_count": max(0, len(rows) - len(latest_by_paper)),
        "by_operation": {PROCESS_OPERATION: len(selected)},
        "task_ids": [task.task_id for task in selected],
        "items": preview_items,
    }
    if body.dry_run or not selected:
        return result
    task_ids = []
    for task in selected:
        retry, created = submit_process_task(
            db, task.paper_id, provider="admin", details={"retry_of": task.task_id}
        )
        if created:
            retry = enqueue_submitted_process(db, retry)
        task_ids.append(retry.task_id)
        _audit(db, request, "task.bulk_retry_process", "task", retry.task_id, {"retry_of": task.task_id})
    db.commit()
    result["task_ids"] = task_ids
    return result


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    task = db.get(PaperTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.operation not in {PROCESS_OPERATION, REEMBED_OPERATION, EMBEDDING_REPAIR_OPERATION}:
        raise HTTPException(status_code=409, detail="This task cannot be cancelled.")
    next_task_ids = []
    if task.operation == REEMBED_OPERATION:
        cancel_embedding_rebuild(db, task)
    else:
        next_task_ids = cancel_process_task(db, task)
    _audit(db, request, "task.cancel", "task", task_id)
    db.commit()
    for next_task_id in next_task_ids:
        next_task = db.get(PaperTask, next_task_id)
        if next_task:
            enqueue_submitted_process(db, next_task)
    return {"task_id": task_id, "status": task.status, "cancel_requested": True}


@router.post("/tasks/{task_id}/reconcile")
def reconcile_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    if not db.get(PaperTask, task_id):
        raise HTTPException(status_code=404, detail="Task not found.")
    _audit(db, request, "task.reconcile", "task", task_id)
    db.commit()
    result = watchdog()
    return {"task_id": task_id, **result}


@router.get("/providers")
def providers(db: Session = Depends(get_db)):
    rows = {row.role: row for row in db.scalars(select(ProviderConfig)).all()}
    items = []
    for role in sorted(ROLES):
        row = rows.get(role)
        credentials = (
            db.scalars(
                select(ProviderCredential).where(
                    ProviderCredential.provider_config_id
                    == row.provider_config_id
                )
            ).all()
            if row
            else []
        )
        now = datetime.now(UTC)
        healthy = [
            credential
            for credential in credentials
            if credential.status == "active"
            and not (
                credential.cooldown_until
                and credential.cooldown_until > now
            )
        ]
        connection = _connection()
        active_leases = 0
        for credential in healthy:
            lease_key = f"provider:key-leases:{credential.credential_id}"
            try:
                connection.zremrangebyscore(
                    lease_key, "-inf", int(now.timestamp() * 1000)
                )
                active_leases += int(connection.zcard(lease_key))
            except (AttributeError, redis.RedisError):
                pass
        healthy_capacity = (
            sum(credential.max_concurrency for credential in healthy)
            if row
            and row.enabled
            and not connection.get(f"provider:breaker:{role}")
            else 0
        )
        items.append(
            {
                "role": role,
                "source": "database" if row else "unconfigured",
                "key_mode": "console_pool" if row else "none",
                "provider": row.provider if row else "dashscope",
                "base_url": row.base_url if row else "",
                "model": row.model if row else ROLE_DEFAULT_MODELS[role],
                "enabled": row.enabled if row else False,
                "timeout_seconds": row.timeout_seconds if row else 60,
                "rpm_limit": row.rpm_limit if row else None,
                "tpm_limit": row.tpm_limit if row else None,
                "breaker_threshold": row.breaker_threshold if row else 5,
                "breaker_cooldown_seconds": row.breaker_cooldown_seconds if row else 60,
                "breaker_open": bool(_connection().get(f"provider:breaker:{role}")),
                "key_count": len(credentials),
                "healthy_key_count": len(healthy),
                "healthy_max_concurrency": healthy_capacity,
                "current_leases": active_leases,
                "remaining_capacity": max(
                    0, healthy_capacity - active_leases
                ),
            }
        )
    return {"items": items}


@router.patch("/providers/{role}")
def update_provider(
    role: str, body: ProviderPatch, request: Request, db: Session = Depends(get_db)
):
    if role not in ROLES:
        raise HTTPException(status_code=404, detail="Unknown provider role.")
    row = db.scalar(select(ProviderConfig).where(ProviderConfig.role == role))
    values = body.model_dump(exclude={"confirm_embedding_model_change"})
    model_changed = role == "embedding" and row is not None and row.model != values["model"]
    if model_changed and not body.confirm_embedding_model_change:
        raise HTTPException(status_code=409, detail="Changing the embedding model requires confirmation and a full rebuild.")
    if row is None:
        row = ProviderConfig(provider_config_id=new_id("provider"), role=role, **values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.config_version += 1
    _audit(
        db,
        request,
        "provider.update",
        "provider",
        role,
        {key: value for key, value in values.items() if key != "base_url"},
    )
    db.commit()
    _connection().incr("provider:config:version")
    rebuild = None
    if model_changed:
        rebuild = submit_embedding_rebuild(db, row.model, row.config_version)
        rebuild = enqueue_submitted_process(db, rebuild)
    return {"role": role, "config_version": row.config_version, "rebuild_task_id": rebuild.task_id if rebuild else None}


@router.put("/providers/{role}/setup")
def setup_provider(
    role: str, body: ProviderSetup, request: Request, db: Session = Depends(get_db)
):
    """Atomically configure a model type and append every submitted API key."""
    if role not in ROLES:
        raise HTTPException(status_code=404, detail="Unknown provider role.")
    aliases = [item.alias for item in body.keys]
    if len(aliases) != len(set(aliases)):
        raise HTTPException(status_code=400, detail="Each submitted API key needs a unique alias.")
    row = db.scalar(select(ProviderConfig).where(ProviderConfig.role == role))
    if row is None and not body.keys:
        raise HTTPException(status_code=400, detail="At least one API key is required when taking over a role.")
    values = body.model_dump(exclude={"keys", "confirm_embedding_model_change"})
    model_changed = role == "embedding" and row is not None and row.model != values["model"]
    if model_changed and not body.confirm_embedding_model_change:
        raise HTTPException(status_code=409, detail="Changing the embedding model requires confirmation and a full rebuild.")
    try:
        if row is None:
            row = ProviderConfig(provider_config_id=new_id("provider"), role=role, **values)
            db.add(row)
            db.flush()
        else:
            for key, value in values.items():
                setattr(row, key, value)
            row.config_version += 1
        for item in body.keys:
            db.add(_new_provider_credential(row, item))
        _audit(
            db,
            request,
            "provider.setup",
            "provider",
            role,
            {"key_count_added": len(body.keys), "model": row.model},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="API key alias already exists for this model type.") from exc
    _connection().incr("provider:config:version")
    rebuild = None
    if model_changed:
        rebuild = submit_embedding_rebuild(db, row.model, row.config_version)
        rebuild = enqueue_submitted_process(db, rebuild)
    return {
        "role": role,
        "config_version": row.config_version,
        "key_count_added": len(body.keys),
        "rebuild_task_id": rebuild.task_id if rebuild else None,
    }


@router.get("/embedding-repairs")
def embedding_repairs(
    status: str | None = None, error_code: str | None = None, db: Session = Depends(get_db)
):
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.role == "embedding"))
    model = config.model if config else get_settings().embedding_model
    repair_tasks = db.scalars(
        select(PaperTask).where(PaperTask.operation == EMBEDDING_REPAIR_OPERATION)
        .order_by(PaperTask.created_at.desc())
    ).all()
    latest_repairs = {}
    for task in repair_tasks:
        latest_repairs.setdefault(task.paper_id, task)
    process_tasks = db.scalars(
        select(PaperTask).where(PaperTask.operation == PROCESS_OPERATION)
        .order_by(PaperTask.created_at.desc())
    ).all()
    latest_process = {}
    for task in process_tasks:
        latest_process.setdefault(task.paper_id, task)
    candidates = set(latest_repairs) | {
        paper_id for paper_id, task in latest_process.items()
        if str((task.details or {}).get("stage")) in {"embedding", "retrying"}
        or str((task.details or {}).get("failed_stage")) == "embedding"
    }
    candidates.update(
        db.scalars(select(Paper.paper_id).where(Paper.embedding_status != "ready")).all()
    )
    papers = db.scalars(select(Paper).where(Paper.paper_id.in_(candidates))).all() if candidates else []
    items = []
    for paper in papers:
        reps = db.scalars(select(EvidenceRepresentation).where(
            EvidenceRepresentation.evidence.has(paper_id=paper.paper_id),
            EvidenceRepresentation.status == "success",
            EvidenceRepresentation.is_active.is_(True),
            EvidenceRepresentation.indexable.is_(True),
        )).all()
        embeddings = {
            item.representation_id: item
            for item in db.scalars(select(EvidenceEmbedding).where(
                EvidenceEmbedding.representation_id.in_([rep.representation_id for rep in reps]),
                EvidenceEmbedding.embedding_model == model,
            )).all()
        } if reps else {}
        healthy = sum(
            bool(item and item.status == "success" and item.embedding_dim == 1024 and item.text_hash == rep.text_hash)
            for rep in reps for item in [embeddings.get(rep.representation_id)]
        )
        task = latest_repairs.get(paper.paper_id)
        process = latest_process.get(paper.paper_id)
        current = task or process
        queue = ((current.details if current else {}) or {}).get("queue") or {}
        stage = ((current.details if current else {}) or {}).get("stage")
        item = {
            "paper_id": paper.paper_id,
            "title": paper.title,
            "status": task.status if task else paper.embedding_status,
            "stage": stage or paper.embedding_status,
            "task_id": task.task_id if task else None,
            "process_task_id": process.task_id if process else None,
            "healthy_count": healthy,
            "pending_count": len(reps) - healthy,
            "total_count": len(reps),
            "progress": (task.details or {}).get("progress") if task else None,
            "error_code": queue.get("error_code"),
            "error_message": current.error_message if current else None,
            "next_retry_at": queue.get("next_retry_at"),
            "updated_at": current.updated_at if current else paper.updated_at,
            "can_stop_and_retry": bool(
                process and process.status in {"queued", "running", "retrying"}
                and (stage == "embedding" or (process.details or {}).get("failed_stage") == "embedding")
            ),
        }
        if status and item["status"] != status:
            continue
        if error_code and item["error_code"] != error_code:
            continue
        items.append(item)
    return {"model": model, "items": sorted(items, key=lambda item: str(item["updated_at"]), reverse=True)}


@router.post("/embedding-repairs")
def create_embedding_repairs(
    body: EmbeddingRepairRequest, request: Request, db: Session = Depends(get_db)
):
    created = []
    for paper_id in dict.fromkeys(body.paper_ids):
        task, was_created = submit_embedding_repair(db, paper_id)
        if was_created:
            created.append(task)
            _audit(db, request, "embedding.repair.create", "paper", paper_id, {"scope": "missing_or_stale_only"})
    db.commit()
    for task in created:
        enqueue_submitted_process(db, task)
    return {"task_ids": [task.task_id for task in created], "created": len(created)}


@router.post("/embedding-repairs/stop-and-retry")
def stop_and_retry_embedding(
    body: EmbeddingRepairRequest, request: Request, db: Session = Depends(get_db)
):
    created = []
    for paper_id in dict.fromkeys(body.paper_ids):
        process = db.scalar(select(PaperTask).where(
            PaperTask.paper_id == paper_id,
            PaperTask.operation == PROCESS_OPERATION,
            PaperTask.status.in_(("queued", "running", "retrying")),
        ).order_by(PaperTask.created_at.desc()).limit(1))
        if not process:
            raise HTTPException(status_code=409, detail=f"Paper {paper_id} has no active processing task.")
        details = process.details or {}
        if details.get("stage") != "embedding" and details.get("failed_stage") != "embedding":
            raise HTTPException(status_code=409, detail=f"Paper {paper_id} is not in embedding stage.")
        cancel_process_task(db, process)
        wait_for = process.task_id if process.status not in {"cancelled", "failed", "succeeded"} else None
        repair, was_created = submit_embedding_repair(
            db, paper_id, wait_for_task_id=wait_for, source_task_id=process.task_id
        )
        if was_created:
            created.append(repair)
        _audit(db, request, "embedding.repair.stop_and_retry", "paper", paper_id, {"process_task_id": process.task_id})
    released = release_waiting_embedding_repairs(db)
    db.commit()
    for task in [*created, *(db.get(PaperTask, task_id) for task_id in released)]:
        if task and task.status == "queued":
            enqueue_submitted_process(db, task)
    return {"task_ids": [task.task_id for task in created], "waiting_for_cancel": len(created) - len(released)}


@router.get("/embedding-rebuild")
def embedding_rebuild(db: Session = Depends(get_db)):
    task = db.scalar(
        select(PaperTask).where(PaperTask.operation == REEMBED_OPERATION)
        .order_by(PaperTask.created_at.desc()).limit(1)
    )
    if not task:
        return {"task": None}
    children = db.scalars(select(PaperTask).where(
        PaperTask.operation == REEMBED_PAPER_OPERATION,
        PaperTask.details["parent_task_id"].astext == task.task_id,
    )).all()
    return {"task": {"task_id": task.task_id, "status": task.status, "progress": task.progress, "details": task.details, "error_message": task.error_message, "children": {"total": len(children), "succeeded": sum(item.status == "succeeded" for item in children), "failed": sum(item.status in {"failed", "cancelled"} for item in children)}}}


@router.post("/embedding-rebuild/restart")
def restart_embedding_rebuild(request: Request, db: Session = Depends(get_db)):
    latest = db.scalar(
        select(PaperTask).where(PaperTask.operation == REEMBED_OPERATION)
        .order_by(PaperTask.created_at.desc()).limit(1)
    )
    if latest and latest.status in {"queued", "running", "retrying"}:
        raise HTTPException(status_code=409, detail="An embedding rebuild is already active.")
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.role == "embedding"))
    if not config:
        raise HTTPException(status_code=409, detail="Configure an embedding model before rebuilding.")
    task = submit_embedding_rebuild(db, config.model, config.config_version)
    task = enqueue_submitted_process(db, task)
    _audit(db, request, "embedding.rebuild.restart", "provider", "embedding", {"model": config.model})
    db.commit()
    return {"task_id": task.task_id, "status": task.status}


@router.get("/providers/{role}/keys")
def provider_keys(role: str, db: Session = Depends(get_db)):
    if role not in ROLES:
        raise HTTPException(status_code=404, detail="Unknown provider role.")
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.role == role))
    if not config:
        return {
            "items": [],
            "source": "unconfigured",
        }
    rows = db.scalars(
        select(ProviderCredential)
        .where(ProviderCredential.provider_config_id == config.provider_config_id)
        .order_by(ProviderCredential.created_at)
    ).all()
    credential_ids = [row.credential_id for row in rows]
    calls = {credential_id: {"total": 0, "success": 0, "failure": 0} for credential_id in credential_ids}
    if credential_ids:
        for event in db.scalars(
            select(ProviderCallEvent).where(ProviderCallEvent.credential_id.in_(credential_ids))
        ).all():
            summary = calls[event.credential_id]
            summary["total"] += 1
            summary["success" if event.status == "success" else "failure"] += 1
    connection = _connection()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    active_leases: dict[str, int] = {}
    for row in rows:
        lease_key = f"provider:key-leases:{row.credential_id}"
        try:
            connection.zremrangebyscore(lease_key, "-inf", now_ms)
            active_leases[row.credential_id] = int(
                connection.zcard(lease_key)
            )
        except (AttributeError, redis.RedisError):
            active_leases[row.credential_id] = 0
    return {
        "items": [
            _credential(
                row,
                calls[row.credential_id],
                active_leases[row.credential_id],
            )
            for row in rows
        ],
        "source": "database",
    }


@router.post("/providers/{role}/keys")
def add_provider_key(
    role: str, body: CredentialCreate, request: Request, db: Session = Depends(get_db)
):
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.role == role))
    if not config:
        raise HTTPException(status_code=400, detail="Create the provider configuration first.")
    row = _new_provider_credential(config, body)
    db.add(row)
    _audit(
        db,
        request,
        "credential.create",
        "credential",
        row.credential_id,
        {"alias": row.alias, "key_suffix": row.key_suffix},
    )
    db.commit()
    return _credential(row)


@router.patch("/provider-keys/{credential_id}")
def update_provider_key(
    credential_id: str, body: CredentialPatch, request: Request, db: Session = Depends(get_db)
):
    row = db.get(ProviderCredential, credential_id)
    if not row:
        raise HTTPException(status_code=404, detail="Credential not found.")
    if body.alias is not None:
        row.alias = body.alias
    if body.weight is not None:
        row.weight = body.weight
    if body.max_concurrency is not None:
        row.max_concurrency = body.max_concurrency
    if body.api_key is not None:
        control = ProviderControlService(get_settings())
        row.encrypted_api_key = control.cipher.encrypt(body.api_key)
        row.key_suffix = body.api_key[-4:]
        row.key_fingerprint = control.fingerprint(body.api_key)
    _audit(
        db,
        request,
        "credential.update",
        "credential",
        credential_id,
        {
            "alias": row.alias,
            "key_suffix": row.key_suffix,
            "max_concurrency": row.max_concurrency,
        },
    )
    db.commit()
    return _credential(row)


@router.post("/provider-keys/{credential_id}/enable")
def enable_provider_key(credential_id: str, request: Request, db: Session = Depends(get_db)):
    row = db.get(ProviderCredential, credential_id)
    if not row:
        raise HTTPException(status_code=404, detail="Credential not found.")
    row.status, row.cooldown_until, row.consecutive_failures = "active", None, 0
    _audit(db, request, "credential.enable", "credential", credential_id)
    db.commit()
    return _credential(row)


@router.post("/provider-keys/{credential_id}/disable")
def disable_provider_key(credential_id: str, request: Request, db: Session = Depends(get_db)):
    row = db.get(ProviderCredential, credential_id)
    if not row:
        raise HTTPException(status_code=404, detail="Credential not found.")
    row.status = "disabled"
    _audit(db, request, "credential.disable", "credential", credential_id)
    db.commit()
    return _credential(row)


@router.post("/provider-keys/{credential_id}/test")
def test_provider_key(credential_id: str, db: Session = Depends(get_db)):
    row = db.get(ProviderCredential, credential_id)
    if not row:
        raise HTTPException(status_code=404, detail="Credential not found.")
    config = db.get(ProviderConfig, row.provider_config_id)
    if not config:
        raise HTTPException(status_code=400, detail="Credential is not assigned to a provider config.")
    key = ProviderControlService(get_settings()).cipher.decrypt(row.encrypted_api_key)
    started = datetime.now(UTC)
    try:
        response, operation = _provider_probe(config, key)
        status_code = response.status_code
        _record_manual_probe(row, status_code, None if status_code < 400 else f"HTTP_{status_code}")
        db.commit()
        return {
            "credential_id": credential_id,
            "role": config.role,
            "operation": operation,
            "healthy": status_code < 400,
            "status_code": status_code,
            "latency_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
            "credential": _credential(row),
        }
    except requests.RequestException as exc:
        _record_manual_probe(row, None, type(exc).__name__)
        db.commit()
        return {
            "credential_id": credential_id,
            "role": config.role,
            "healthy": False,
            "error": type(exc).__name__,
            "credential": _credential(row),
        }


@router.get("/provider-events")
def provider_events(
    role: str | None = None,
    error_code: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    statement = select(ProviderCallEvent).order_by(ProviderCallEvent.created_at.desc())
    if role:
        statement = statement.where(ProviderCallEvent.role == role)
    if error_code:
        statement = statement.where(ProviderCallEvent.error_code == error_code)
    rows = db.scalars(statement.offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "items": [
            {
                "event_id": row.event_id,
                "role": row.role,
                "provider": row.provider,
                "model": row.model,
                "credential_id": row.credential_id,
                "operation": row.operation,
                "status": row.status,
                "error_code": row.error_code,
                "http_status": row.http_status,
                "latency_ms": row.latency_ms,
                "attempt": row.attempt,
                "request_id": row.request_id,
                "trace_id": row.trace_id,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@router.get("/attempts")
def attempts(
    owner_id: str | None = None, page: int = 1, page_size: int = 50, db: Session = Depends(get_db)
):
    statement = select(ExecutionAttempt).order_by(ExecutionAttempt.started_at.desc())
    if owner_id:
        statement = statement.where(ExecutionAttempt.owner_id == owner_id)
    rows = db.scalars(statement.offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "items": [
            {
                column.name: getattr(row, column.name)
                for column in ExecutionAttempt.__table__.columns
            }
            for row in rows
        ]
    }


@router.get("/harvest/openalex/credentials")
def harvest_credentials(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(HarvestCredential)
        .where(HarvestCredential.provider == "openalex")
        .order_by(HarvestCredential.created_at)
    ).all()
    return {"items": [credential_payload(row) for row in rows]}


@router.post("/harvest/openalex/credentials", status_code=201)
def create_harvest_credential(
    body: HarvestCredentialCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        row = HarvestCredentialService(get_settings()).create(
            db,
            alias=body.alias,
            api_key=body.api_key,
            weight=body.weight,
        )
        _audit(
            db,
            request,
            "harvest.credential.create",
            "harvest_credential",
            row.credential_id,
            {"provider": "openalex", "alias": row.alias},
        )
        db.commit()
        return credential_payload(row)
    except (HarvestError, ValueError) as exc:
        db.rollback()
        code = getattr(exc, "code", "ADMIN_MASTER_KEY_NOT_CONFIGURED")
        status_code = getattr(exc, "status_code", 503)
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": str(exc)},
        ) from exc


@router.post("/harvest/credentials/{credential_id}/test")
def test_harvest_credential(
    credential_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    row = db.get(HarvestCredential, credential_id)
    if not row:
        raise HTTPException(status_code=404, detail="Harvest credential not found.")
    try:
        HarvestCredentialService(get_settings()).refresh_quota(row)
        _audit(
            db,
            request,
            "harvest.credential.test",
            "harvest_credential",
            credential_id,
            {"healthy": True},
        )
        db.commit()
        return {"healthy": True, "credential": credential_payload(row)}
    except (HarvestError, ValueError) as exc:
        _audit(
            db,
            request,
            "harvest.credential.test",
            "harvest_credential",
            credential_id,
            {"healthy": False, "error_code": getattr(exc, "code", type(exc).__name__)},
        )
        db.commit()
        return {
            "healthy": False,
            "error_code": getattr(exc, "code", type(exc).__name__),
            "credential": credential_payload(row),
        }


@router.post("/harvest/credentials/{credential_id}/{action}")
def toggle_harvest_credential(
    credential_id: str,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if action not in {"enable", "disable"}:
        raise HTTPException(status_code=404, detail="Unknown credential action.")
    row = db.get(HarvestCredential, credential_id)
    if not row:
        raise HTTPException(status_code=404, detail="Harvest credential not found.")
    row.status = "active" if action == "enable" else "disabled"
    if action == "enable":
        row.last_error_code = None
        row.cooldown_until = None
    _audit(
        db,
        request,
        f"harvest.credential.{action}",
        "harvest_credential",
        credential_id,
        {"status": row.status},
    )
    db.commit()
    return credential_payload(row)


@router.get("/harvest/openalex/quota")
def harvest_quota(db: Session = Depends(get_db)):
    return HarvestCredentialService(get_settings()).quota_summary(db)
