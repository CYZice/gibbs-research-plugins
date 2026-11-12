from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select, update

from app.core.ids import new_id
from app.models.import_batch import ImportBatch, ImportBatchItem
from app.models.paper import Paper
from app.models.paper_task import PaperTask
from app.services.task_queue_constants import (
    CONTENT_QUEUE,
    EMBEDDING_QUEUE,
    INDEX_QUEUE,
    INGEST_QUEUE,
    PARSE_QUEUE,
    REMOTE_FETCH_QUEUE,
)

PIPELINE_VERSION = 3
PIPELINE_ROOT_OPERATION = "pipeline"
PIPELINE_STAGE_ORDER = (
    "remote_fetch",
    "register",
    "parse",
    "evidence",
    "resource_context",
    "vectorize",
    "index",
)
PIPELINE_DEPENDENCIES = {
    "remote_fetch": frozenset(),
    "register": frozenset({"remote_fetch"}),
    "parse": frozenset({"register"}),
    "evidence": frozenset({"parse"}),
    "resource_context": frozenset({"evidence"}),
    "vectorize": frozenset({"evidence", "resource_context"}),
    "index": frozenset({"vectorize"}),
}
PIPELINE_STAGE_WEIGHTS = {
    "remote_fetch": 0.10,
    "register": 0.05,
    "parse": 0.35,
    "evidence": 0.15,
    "resource_context": 0.10,
    "vectorize": 0.20,
    "index": 0.05,
}
PIPELINE_QUEUE_BY_STAGE = {
    "remote_fetch": REMOTE_FETCH_QUEUE,
    "register": INGEST_QUEUE,
    "parse": PARSE_QUEUE,
    "evidence": CONTENT_QUEUE,
    "resource_context": CONTENT_QUEUE,
    "vectorize": EMBEDDING_QUEUE,
    "index": INDEX_QUEUE,
}
CLAIMABLE_STAGE_STATUSES = ("pending", "waiting_external", "retry_wait")
COMPLETED_STAGE_STATUSES = frozenset({"succeeded", "skipped"})
ACTIVE_ROOT_STATUSES = (
    "pending",
    "running",
    "waiting_external",
    "retrying",
)


class PipelineInvariantError(RuntimeError):
    pass


def validate_stage(stage: str) -> None:
    if stage not in PIPELINE_DEPENDENCIES:
        raise PipelineInvariantError(f"Unknown pipeline stage: {stage}")


def dependencies_satisfied(stage: str, completed: Collection[str]) -> bool:
    validate_stage(stage)
    return PIPELINE_DEPENDENCIES[stage].issubset(completed)


def ready_successors(completed_stage: str, completed: Collection[str]) -> tuple[str, ...]:
    validate_stage(completed_stage)
    completed_set = set(completed)
    completed_set.add(completed_stage)
    return tuple(
        stage
        for stage in PIPELINE_STAGE_ORDER[
            PIPELINE_STAGE_ORDER.index(completed_stage) + 1 :
        ]
        if stage not in completed_set
        and dependencies_satisfied(stage, completed_set)
    )[:1]


def pipeline_progress(completed: Collection[str]) -> float:
    completed_set = set(completed)
    return min(
        1.0,
        sum(
            PIPELINE_STAGE_WEIGHTS[stage]
            for stage in PIPELINE_STAGE_ORDER
            if stage in completed_set
        ),
    )


def reconcile_import_batch(db, batch_id: str) -> ImportBatch | None:
    """Serialize and rebuild batch counters from their item source of truth."""
    batch = db.scalar(
        select(ImportBatch)
        .where(ImportBatch.batch_id == batch_id)
        .with_for_update()
    )
    if not batch:
        return None
    db.flush()
    total, completed, failed, cancelled = db.execute(
        select(
            func.count(),
            func.count().filter(ImportBatchItem.status == "succeeded"),
            func.count().filter(ImportBatchItem.status == "failed"),
            func.count().filter(ImportBatchItem.status == "cancelled"),
        ).where(ImportBatchItem.batch_id == batch_id)
    ).one()
    batch.total_items = int(total or 0)
    batch.completed_items = int(completed or 0)
    batch.failed_items = int(failed or 0)
    details = dict(batch.details or {})
    details["cancelled_items"] = int(cancelled or 0)
    batch.details = details

    terminal = batch.completed_items + batch.failed_items + int(cancelled or 0)
    if batch.intake_closed and terminal >= batch.total_items:
        if batch.total_items == 0:
            batch.status = "completed"
        elif batch.status == "cancelling" or cancelled == batch.total_items:
            batch.status = "cancelled"
        elif batch.failed_items and not batch.completed_items and not cancelled:
            batch.status = "failed"
        elif batch.failed_items or cancelled:
            batch.status = "completed_with_errors"
        else:
            batch.status = "completed"
    elif batch.status not in {"paused", "cancelling"}:
        batch.status = (
            "expanding"
            if batch.manifest_storage_key and not batch.intake_closed
            else "running"
            if batch.total_items
            else "draft"
        )
    return batch


def new_pipeline_tasks(
    *,
    paper_id: str | None,
    initial_stage: str,
    provider: str,
    batch_id: str | None = None,
    batch_item_id: str | None = None,
    priority: int = 50,
    details: dict | None = None,
    now: datetime | None = None,
) -> tuple[PaperTask, PaperTask]:
    validate_stage(initial_stage)
    created_at = now or datetime.now(UTC)
    root_id = new_id("task")
    skipped_stages = list(
        PIPELINE_STAGE_ORDER[: PIPELINE_STAGE_ORDER.index(initial_stage)]
    )
    pipeline_details = {
        **(details or {}),
        "pipeline_version": PIPELINE_VERSION,
        "skipped_stages": skipped_stages,
    }
    root = PaperTask(
        task_id=root_id,
        paper_id=paper_id,
        operation=PIPELINE_ROOT_OPERATION,
        provider=provider,
        status="pending",
        progress=0.0,
        stage=initial_stage,
        root_task_id=root_id,
        batch_id=batch_id,
        batch_item_id=batch_item_id,
        pipeline_version=PIPELINE_VERSION,
        pipeline_generation=1,
        priority=priority,
        details=pipeline_details,
    )
    stage_id = new_id("task")
    stage_task = PaperTask(
        task_id=stage_id,
        paper_id=paper_id,
        operation=initial_stage,
        provider=provider,
        status="pending",
        progress=0.0,
        stage=initial_stage,
        root_task_id=root_id,
        batch_id=batch_id,
        batch_item_id=batch_item_id,
        queue_name=PIPELINE_QUEUE_BY_STAGE[initial_stage],
        delivery_status="pending",
        delivery_generation=0,
        available_at=created_at,
        next_action="submit" if initial_stage == "parse" else "execute",
        pipeline_version=PIPELINE_VERSION,
        pipeline_generation=1,
        idempotency_key=f"{root_id}:1:{initial_stage}",
        priority=priority,
        details={**pipeline_details},
    )
    return root, stage_task


def new_stage_task(
    root: PaperTask, stage: str, *, now: datetime | None = None
) -> PaperTask:
    validate_stage(stage)
    available_at = now or datetime.now(UTC)
    return PaperTask(
        task_id=new_id("task"),
        paper_id=root.paper_id,
        operation=stage,
        provider=root.provider,
        status="pending",
        progress=0.0,
        stage=stage,
        root_task_id=root.task_id,
        batch_id=root.batch_id,
        batch_item_id=root.batch_item_id,
        queue_name=PIPELINE_QUEUE_BY_STAGE[stage],
        delivery_status="pending",
        delivery_generation=0,
        available_at=available_at,
        next_action="submit" if stage == "parse" else "execute",
        pipeline_version=root.pipeline_version,
        pipeline_generation=root.pipeline_generation,
        idempotency_key=(
            f"{root.task_id}:{root.pipeline_generation}:{stage}"
        ),
        priority=root.priority,
        details={
            **dict(root.details or {}),
            "pipeline_version": root.pipeline_version,
            "pipeline_generation": root.pipeline_generation,
        },
    )


def submit_pipeline_for_paper(
    db,
    paper_id: str,
    *,
    provider: str,
    options: dict | None = None,
    priority: int = 50,
) -> tuple[PaperTask, bool]:
    paper = db.scalar(
        select(Paper).where(Paper.paper_id == paper_id).with_for_update()
    )
    if not paper:
        raise ValueError("Paper not found.")
    active = db.scalar(
        select(PaperTask)
        .where(
            PaperTask.paper_id == paper_id,
            PaperTask.operation == PIPELINE_ROOT_OPERATION,
            PaperTask.pipeline_version == PIPELINE_VERSION,
            PaperTask.status.in_(ACTIVE_ROOT_STATUSES),
        )
        .order_by(PaperTask.created_at.desc())
        .limit(1)
    )
    if active:
        return active, False
    root, stage = new_pipeline_tasks(
        paper_id=paper_id,
        initial_stage="parse",
        provider=provider,
        priority=priority,
        details={"options": options or {}},
    )
    paper.parse_status = "queued"
    paper.evidence_status = "not_started"
    paper.multimodal_status = "not_started"
    paper.embedding_status = "not_started"
    paper.index_status = "not_started"
    db.add_all((root, stage))
    return root, True


def build_claim_statement(
    *,
    task_id: str,
    delivery_generation: int,
    run_token: str,
    worker_id: str,
    run_lease_until: datetime,
):
    now = datetime.now(UTC)
    return (
        update(PaperTask)
        .where(
            PaperTask.task_id == task_id,
            PaperTask.delivery_generation == delivery_generation,
            PaperTask.delivery_status == "enqueued",
            PaperTask.run_token.is_(None),
            PaperTask.status.in_(CLAIMABLE_STAGE_STATUSES),
        )
        .values(
            status=case(
                (
                    PaperTask.status == "waiting_external",
                    "waiting_external",
                ),
                else_="running",
            ),
            attempt_count=PaperTask.attempt_count + 1,
            run_token=run_token,
            run_lease_until=run_lease_until,
            worker_id=worker_id,
            heartbeat_at=now,
            delivery_status="idle",
            started_at=now,
        )
        .returning(PaperTask)
    )


def claim_stage_task(
    db,
    *,
    task_id: str,
    delivery_generation: int,
    run_token: str,
    worker_id: str,
    run_lease_until: datetime,
) -> PaperTask | None:
    return db.scalar(
        build_claim_statement(
            task_id=task_id,
            delivery_generation=delivery_generation,
            run_token=run_token,
            worker_id=worker_id,
            run_lease_until=run_lease_until,
        )
    )


def release_stage_run(
    db,
    *,
    task_id: str,
    run_token: str,
    status: str,
    available_at: datetime | None = None,
    next_action: str | None = None,
) -> bool:
    values = {
        "status": status,
        "run_token": None,
        "run_lease_until": None,
        "worker_id": None,
        "heartbeat_at": datetime.now(UTC),
        "next_action": next_action,
    }
    if status in {"waiting_external", "retry_wait"}:
        if available_at is None:
            raise PipelineInvariantError(f"{status} requires available_at")
        values.update(delivery_status="pending", available_at=available_at)
    elif status in {"succeeded", "failed", "cancelled", "superseded", "skipped"}:
        values.update(
            delivery_status="idle",
            available_at=None,
            finished_at=datetime.now(UTC),
        )
    else:
        raise PipelineInvariantError(f"Unsupported released stage status: {status}")
    result = db.execute(
        update(PaperTask)
        .where(PaperTask.task_id == task_id, PaperTask.run_token == run_token)
        .values(**values)
    )
    return bool(result.rowcount)


def renew_stage_run(
    db,
    *,
    task_id: str,
    run_token: str,
    lease_seconds: int,
) -> bool:
    now = datetime.now(UTC)
    result = db.execute(
        update(PaperTask)
        .where(
            PaperTask.task_id == task_id,
            PaperTask.run_token == run_token,
        )
        .values(
            heartbeat_at=now,
            run_lease_until=now + timedelta(seconds=lease_seconds),
        )
    )
    return bool(result.rowcount)


def complete_stage_and_schedule_successors(
    db,
    *,
    task_id: str,
    run_token: str,
    output_fingerprint: str | None = None,
    now: datetime | None = None,
) -> list[PaperTask]:
    completed_at = now or datetime.now(UTC)
    task = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task_id)
        .with_for_update()
    )
    if not task or task.operation not in PIPELINE_DEPENDENCIES:
        raise PipelineInvariantError("Pipeline stage task was not found.")
    if task.run_token != run_token:
        raise PipelineInvariantError("Pipeline stage is no longer owned by this run.")
    root = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task.root_task_id)
        .with_for_update()
    )
    if not root or root.operation != PIPELINE_ROOT_OPERATION:
        raise PipelineInvariantError("Pipeline root task was not found.")
    if (
        task.pipeline_version != root.pipeline_version
        or task.pipeline_generation != root.pipeline_generation
    ):
        raise PipelineInvariantError("Pipeline stage generation is stale.")

    stage_rows = db.scalars(
        select(PaperTask)
        .where(
            PaperTask.root_task_id == root.task_id,
            PaperTask.pipeline_version == root.pipeline_version,
            PaperTask.pipeline_generation == root.pipeline_generation,
            PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
        )
        .with_for_update()
    ).all()
    completed = {
        row.operation for row in stage_rows if row.status in COMPLETED_STAGE_STATUSES
    }
    completed.update((root.details or {}).get("skipped_stages") or [])
    if not dependencies_satisfied(task.operation, completed):
        raise PipelineInvariantError(
            f"Dependencies for {task.operation} are not complete."
        )

    task.status = "succeeded"
    task.progress = 1.0
    task.output_fingerprint = output_fingerprint
    task.run_token = None
    task.run_lease_until = None
    task.worker_id = None
    task.delivery_status = "idle"
    task.delivery_lease_until = None
    task.finished_at = completed_at
    completed.add(task.operation)

    existing = {row.operation: row for row in stage_rows}
    successors = []
    for stage in ready_successors(task.operation, completed):
        successor = existing.get(stage)
        if successor and successor.status not in {
            "superseded",
            "failed",
            "cancelled",
        }:
            continue
        if successor:
            successor.status = "pending"
            successor.progress = 0.0
            successor.delivery_status = "pending"
            successor.available_at = completed_at
            successor.next_action = "submit" if stage == "parse" else "execute"
            successor.run_token = None
            successor.run_lease_until = None
            successor.worker_id = None
            successor.finished_at = None
            successor.error_code = None
            successor.error_domain = None
            successor.error_message = None
        else:
            successor = new_stage_task(root, stage, now=completed_at)
            db.add(successor)
        successors.append(successor)

    root.progress = pipeline_progress(completed)
    if len(completed) == len(PIPELINE_STAGE_ORDER):
        root.status = "succeeded"
        root.stage = "succeeded"
        root.progress = 1.0
        root.finished_at = completed_at
    elif successors:
        root.status = "running"
        root.stage = successors[0].operation

    if root.batch_item_id:
        item = db.get(ImportBatchItem, root.batch_item_id)
        if item:
            was_succeeded = item.status == "succeeded"
            item.pipeline_task_id = root.task_id
            item.current_stage = root.stage
            item.details = {**(item.details or {}), "stage": root.stage}
            item.pipeline_version = root.pipeline_version
            item.status = "succeeded" if root.status == "succeeded" else "running"
            item.finished_at = completed_at if root.status == "succeeded" else None
            if root.status == "succeeded" and not was_succeeded and root.batch_id:
                reconcile_import_batch(db, root.batch_id)
    return successors


def fail_stage_and_root(
    db,
    *,
    task_id: str,
    run_token: str,
    error_code: str,
    error_domain: str,
    error_message: str,
    now: datetime | None = None,
) -> None:
    failed_at = now or datetime.now(UTC)
    task = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task_id)
        .with_for_update()
    )
    if not task or task.run_token != run_token:
        raise PipelineInvariantError("Pipeline stage is no longer owned by this run.")
    root = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task.root_task_id)
        .with_for_update()
    )
    if not root or root.operation != PIPELINE_ROOT_OPERATION:
        raise PipelineInvariantError("Pipeline root task was not found.")
    if (
        task.pipeline_version != root.pipeline_version
        or task.pipeline_generation != root.pipeline_generation
    ):
        raise PipelineInvariantError("Pipeline stage generation is stale.")

    task.status = "failed"
    task.delivery_status = "idle"
    task.run_token = None
    task.run_lease_until = None
    task.worker_id = None
    task.finished_at = failed_at
    task.error_code = error_code
    task.error_domain = error_domain
    task.error_message = error_message
    root.status = "failed"
    root.stage = task.operation
    root.finished_at = failed_at
    root.error_code = error_code
    root.error_domain = error_domain
    root.error_message = error_message

    if root.batch_item_id:
        item = db.get(ImportBatchItem, root.batch_item_id)
        if item:
            was_failed = item.status == "failed"
            item.status = "failed"
            item.current_stage = task.operation
            item.error_message = error_message
            item.finished_at = failed_at
            item.details = {**(item.details or {}), "stage": task.operation}
            if not was_failed and root.batch_id:
                reconcile_import_batch(db, root.batch_id)


def complete_stage_and_skip_remaining(
    db,
    *,
    task_id: str,
    run_token: str,
    output_fingerprint: str | None = None,
    now: datetime | None = None,
) -> list[PaperTask]:
    completed_at = now or datetime.now(UTC)
    task = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task_id)
        .with_for_update()
    )
    if not task or task.run_token != run_token:
        raise PipelineInvariantError("Pipeline stage is no longer owned by this run.")
    root = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task.root_task_id)
        .with_for_update()
    )
    if not root or root.operation != PIPELINE_ROOT_OPERATION:
        raise PipelineInvariantError("Pipeline root task was not found.")
    if (
        task.pipeline_version != root.pipeline_version
        or task.pipeline_generation != root.pipeline_generation
    ):
        raise PipelineInvariantError("Pipeline stage generation is stale.")

    task.status = "succeeded"
    task.progress = 1.0
    task.output_fingerprint = output_fingerprint
    task.run_token = None
    task.run_lease_until = None
    task.worker_id = None
    task.delivery_status = "idle"
    task.finished_at = completed_at
    existing = set(
        db.scalars(
            select(PaperTask.operation).where(
                PaperTask.root_task_id == root.task_id,
                PaperTask.pipeline_version == root.pipeline_version,
                PaperTask.pipeline_generation == root.pipeline_generation,
                PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
            )
        ).all()
    )
    skipped = []
    root_skipped = set((root.details or {}).get("skipped_stages") or [])
    for stage in PIPELINE_STAGE_ORDER[
        PIPELINE_STAGE_ORDER.index(task.operation) + 1 :
    ]:
        if stage in existing or stage in root_skipped:
            continue
        child = new_stage_task(root, stage, now=completed_at)
        child.status = "skipped"
        child.delivery_status = "idle"
        child.available_at = None
        child.finished_at = completed_at
        child.details = {**dict(child.details or {}), "skip_reason": "duplicate_ready"}
        db.add(child)
        skipped.append(child)

    root.status = "succeeded"
    root.stage = "succeeded"
    root.progress = 1.0
    root.finished_at = completed_at
    if root.batch_item_id:
        item = db.get(ImportBatchItem, root.batch_item_id)
        if item:
            was_succeeded = item.status == "succeeded"
            item.status = "succeeded"
            item.current_stage = "succeeded"
            item.details = {**(item.details or {}), "stage": "succeeded"}
            item.finished_at = completed_at
            if not was_succeeded and root.batch_id:
                reconcile_import_batch(db, root.batch_id)
    return skipped


def retry_pipeline_from_stage(
    db,
    *,
    task_id: str,
    priority: int | None = None,
    now: datetime | None = None,
) -> PaperTask:
    reset_at = now or datetime.now(UTC)
    target = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task_id)
        .with_for_update()
    )
    if not target or target.operation not in PIPELINE_STAGE_ORDER:
        raise PipelineInvariantError("Pipeline stage task was not found.")
    root = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == target.root_task_id)
        .with_for_update()
    )
    if not root or root.operation != PIPELINE_ROOT_OPERATION:
        raise PipelineInvariantError("Pipeline root task was not found.")
    stages = db.scalars(
        select(PaperTask)
        .where(
            PaperTask.root_task_id == root.task_id,
            PaperTask.pipeline_version == root.pipeline_version,
            PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
        )
        .with_for_update()
    ).all()
    if target.run_token or target.status in {"pending", "running", "waiting_external"}:
        raise PipelineInvariantError("Pipeline stage is still active.")
    target_index = PIPELINE_STAGE_ORDER.index(target.operation)
    by_operation = {stage.operation: stage for stage in stages}
    skipped_stages = set((root.details or {}).get("skipped_stages") or [])
    for operation in PIPELINE_STAGE_ORDER[:target_index]:
        previous = by_operation.get(operation)
        if operation not in skipped_stages and (
            not previous or previous.status not in COMPLETED_STAGE_STATUSES
        ):
            raise PipelineInvariantError(
                f"Cannot retry {target.operation} before {operation} is complete."
            )
    generation = root.pipeline_generation + 1
    root.pipeline_generation = generation
    if priority is not None:
        root.priority = priority
    completed = set(skipped_stages)
    for stage in stages:
        if priority is not None:
            stage.priority = priority
        stage.pipeline_generation = generation
        stage.idempotency_key = f"{root.task_id}:{generation}:{stage.operation}"
        stage.cancel_requested_at = None
        stage.run_token = None
        stage.run_lease_until = None
        stage.worker_id = None
        stage.delivery_lease_until = None
        stage.error_code = None
        stage.error_domain = None
        stage.error_message = None
        stage.next_retry_at = None
        stage.finished_at = None
        stage.details = {
            **dict(stage.details or {}),
            "pipeline_generation": generation,
            "failure_count": 0,
        }
        index = PIPELINE_STAGE_ORDER.index(stage.operation)
        if index < target_index and stage.status in COMPLETED_STAGE_STATUSES:
            stage.delivery_status = "idle"
            stage.available_at = None
            completed.add(stage.operation)
        elif index == target_index:
            stage.input_fingerprint = None
            stage.output_fingerprint = None
            stage.status = "pending"
            stage.progress = 0.0
            stage.delivery_status = "pending"
            stage.available_at = reset_at
            stage.next_action = (
                "submit" if stage.operation == "parse" else "execute"
            )
        else:
            stage.input_fingerprint = None
            stage.output_fingerprint = None
            stage.status = "superseded"
            stage.progress = 0.0
            stage.delivery_status = "idle"
            stage.available_at = None

    root.status = "pending"
    root.stage = target.operation
    root.progress = pipeline_progress(completed)
    root.cancel_requested_at = None
    root.finished_at = None
    root.error_code = None
    root.error_domain = None
    root.error_message = None
    root.details = {
        **dict(root.details or {}),
        "pipeline_generation": generation,
    }
    if root.paper_id:
        paper = db.get(Paper, root.paper_id)
        if paper:
            status_fields = (
                ("parse_status", "queued"),
                ("evidence_status", "not_started"),
                ("multimodal_status", "not_started"),
                ("embedding_status", "not_started"),
                ("index_status", "not_started"),
            )
            first_field = {
                "remote_fetch": 0,
                "register": 0,
                "parse": 0,
                "evidence": 1,
                "resource_context": 2,
                "vectorize": 3,
                "index": 4,
            }[target.operation]
            for field, value in status_fields[first_field:]:
                setattr(paper, field, value)
    if root.batch_item_id:
        item = db.get(ImportBatchItem, root.batch_item_id)
        if item:
            was_failed = item.status == "failed"
            item.status = "pending"
            item.current_stage = target.operation
            item.details = {**(item.details or {}), "stage": target.operation}
            item.error_message = None
            item.finished_at = None
            if was_failed and root.batch_id:
                reconcile_import_batch(db, root.batch_id)
    return root


def request_pipeline_cancel(
    db,
    *,
    root_task_id: str,
    now: datetime | None = None,
) -> PaperTask:
    cancelled_at = now or datetime.now(UTC)
    root = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == root_task_id)
        .with_for_update()
    )
    if not root or root.operation != PIPELINE_ROOT_OPERATION:
        raise PipelineInvariantError("Pipeline root task was not found.")
    root.cancel_requested_at = cancelled_at
    stages = db.scalars(
        select(PaperTask)
        .where(
            PaperTask.root_task_id == root.task_id,
            PaperTask.pipeline_version == root.pipeline_version,
            PaperTask.pipeline_generation == root.pipeline_generation,
            PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
        )
        .with_for_update()
    ).all()
    running = False
    for stage in stages:
        if stage.run_token:
            stage.cancel_requested_at = cancelled_at
            running = True
        elif stage.status not in COMPLETED_STAGE_STATUSES | {
            "failed",
            "cancelled",
            "superseded",
        }:
            stage.status = "cancelled"
            stage.delivery_status = "idle"
            stage.available_at = None
            stage.finished_at = cancelled_at
    root.status = "cancelling" if running else "cancelled"
    root.stage = "cancelling" if running else "cancelled"
    root.finished_at = None if running else cancelled_at
    if root.batch_item_id:
        item = db.get(ImportBatchItem, root.batch_item_id)
        if item:
            item.status = root.status
            item.current_stage = root.stage
            item.details = {**(item.details or {}), "stage": root.stage}
            item.finished_at = None if running else cancelled_at
            if root.batch_id:
                reconcile_import_batch(db, root.batch_id)
    return root


def cancel_owned_stage_and_root(
    db,
    *,
    task_id: str,
    run_token: str,
    now: datetime | None = None,
) -> None:
    cancelled_at = now or datetime.now(UTC)
    task = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task_id)
        .with_for_update()
    )
    if not task or task.run_token != run_token:
        raise PipelineInvariantError("Pipeline stage is no longer owned by this run.")
    root = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == task.root_task_id)
        .with_for_update()
    )
    if not root:
        raise PipelineInvariantError("Pipeline root task was not found.")
    task.status = "cancelled"
    task.delivery_status = "idle"
    task.run_token = None
    task.run_lease_until = None
    task.worker_id = None
    task.finished_at = cancelled_at
    root.status = "cancelled"
    root.stage = "cancelled"
    root.finished_at = cancelled_at
    if root.batch_item_id:
        item = db.get(ImportBatchItem, root.batch_item_id)
        if item:
            item.status = "cancelled"
            item.current_stage = "cancelled"
            item.details = {**(item.details or {}), "stage": "cancelled"}
            item.finished_at = cancelled_at
            if root.batch_id:
                reconcile_import_batch(db, root.batch_id)


def reconcile_pipeline_root(db, root_task_id: str) -> PaperTask:
    root = db.scalar(
        select(PaperTask)
        .where(PaperTask.task_id == root_task_id)
        .with_for_update()
    )
    if not root or root.operation != PIPELINE_ROOT_OPERATION:
        raise PipelineInvariantError("Pipeline root task was not found.")
    stages = db.scalars(
        select(PaperTask).where(
            PaperTask.root_task_id == root.task_id,
            PaperTask.pipeline_version == root.pipeline_version,
            PaperTask.pipeline_generation == root.pipeline_generation,
            PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
        )
    ).all()
    completed = {
        stage.operation
        for stage in stages
        if stage.status in COMPLETED_STAGE_STATUSES
    }
    completed.update((root.details or {}).get("skipped_stages") or [])
    root.progress = pipeline_progress(completed)
    failed = next((stage for stage in stages if stage.status == "failed"), None)
    current = next(
        (
            stage
            for operation in PIPELINE_STAGE_ORDER
            for stage in stages
            if stage.operation == operation
            and stage.status not in COMPLETED_STAGE_STATUSES
            and stage.status != "superseded"
        ),
        None,
    )
    if root.cancel_requested_at:
        root.status = "cancelling" if any(stage.run_token for stage in stages) else "cancelled"
        root.stage = root.status
        root.finished_at = None if root.status == "cancelling" else datetime.now(UTC)
    elif failed:
        root.status = "failed"
        root.stage = failed.operation
    elif len(completed) == len(PIPELINE_STAGE_ORDER):
        root.status = "succeeded"
        root.stage = "succeeded"
        root.progress = 1.0
        root.error_code = None
        root.error_domain = None
        root.error_message = None
    elif current:
        root.stage = current.operation
        root.status = (
            "running"
            if current.run_token
            else "waiting_external"
            if current.status == "waiting_external"
            else "retrying"
            if current.status == "retry_wait"
            else "pending"
        )
        root.error_code = None
        root.error_domain = None
        root.error_message = None
    if root.batch_item_id:
        item = db.get(ImportBatchItem, root.batch_item_id)
        if item:
            item.status = root.status
            item.current_stage = root.stage
            item.details = {**(item.details or {}), "stage": root.stage}
            item.error_message = root.error_message
            item.finished_at = (
                root.finished_at
                if root.status in {"succeeded", "failed", "cancelled"}
                else None
            )
            if root.batch_id:
                reconcile_import_batch(db, root.batch_id)
    return root
