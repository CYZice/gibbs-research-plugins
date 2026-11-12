from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis
from rq import Queue, Retry, Worker
from rq.exceptions import DuplicateJobError, NoSuchJobError
from rq.job import Job, JobStatus
from rq.registry import StartedJobRegistry
from sqlalchemy import and_, case, func, or_, select, update

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.import_batch import ImportBatch
from app.models.operational_control import (
    ExecutionAttempt,
    ProviderConfig,
    ProviderCredential,
)
from app.models.paper_task import PaperTask
from app.services.pipeline_batch_service import PipelineBatchService
from app.services.pipeline_service import PIPELINE_VERSION
from app.services.task_queue_constants import (
    CONTENT_QUEUE,
    EMBEDDING_QUEUE,
    INDEX_QUEUE,
    INGEST_QUEUE,
    PARSE_QUEUE,
    REMOTE_FETCH_QUEUE,
)

PIPELINE_QUEUE_NAMES = (
    INGEST_QUEUE,
    REMOTE_FETCH_QUEUE,
    PARSE_QUEUE,
    CONTENT_QUEUE,
    EMBEDDING_QUEUE,
    INDEX_QUEUE,
)
PIPELINE_JOB_RETRY_INTERVALS = [60, 300, 900]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryReservation:
    task_id: str
    queue_name: str
    generation: int

    @property
    def job_id(self) -> str:
        return f"{self.task_id}-{self.generation}"


def reserve_due_deliveries(
    db,
    queue_name: str,
    *,
    limit: int,
    lease_seconds: int,
    overdue_seconds: int = 900,
    now: datetime | None = None,
) -> list[DeliveryReservation]:
    if limit <= 0:
        return []
    claimed_at = now or datetime.now(UTC)
    overdue_before = claimed_at - timedelta(seconds=overdue_seconds)
    base_filters = (
            PaperTask.pipeline_version == PIPELINE_VERSION,
            PaperTask.queue_name == queue_name,
            PaperTask.delivery_status == "pending",
            PaperTask.available_at <= claimed_at,
            or_(
                PaperTask.batch_id.is_(None),
                ~PaperTask.batch_id.in_(
                    select(ImportBatch.batch_id).where(
                        ImportBatch.status.in_(
                            ("paused", "cancelling", "cancelled")
                        )
                    )
                ),
            ),
    )
    priority_filters = {
        "interactive": PaperTask.priority >= 80,
        "normal": and_(PaperTask.priority >= 20, PaperTask.priority < 80),
        "maintenance": PaperTask.priority < 20,
    }
    candidates: dict[str, list[PaperTask]] = {
        category: [] for category in priority_filters
    }
    seen_task_ids = set()
    for category, priority_filter in priority_filters.items():
        rows = db.scalars(
            select(PaperTask)
            .where(*base_filters, priority_filter)
            .order_by(
                case(
                    (PaperTask.available_at <= overdue_before, 1),
                    else_=0,
                ).desc(),
                PaperTask.priority.desc(),
                PaperTask.created_at,
            )
            .limit(limit * 10)
            .with_for_update(skip_locked=True)
        ).all()
        for task in rows:
            priority = int(getattr(task, "priority", 50) or 50)
            actual_category = (
                "interactive"
                if priority >= 80
                else "normal"
                if priority >= 20
                else "maintenance"
            )
            if actual_category == category and task.task_id not in seen_task_ids:
                candidates[category].append(task)
                seen_task_ids.add(task.task_id)

    buckets: dict[str, dict[str, list[PaperTask]]] = {}
    for category, category_tasks in candidates.items():
        category_buckets: dict[str, list[PaperTask]] = {}
        for task in category_tasks:
            category_buckets.setdefault(
                getattr(task, "batch_id", None) or task.task_id, []
            ).append(task)
        buckets[category] = category_buckets

    def take_one(category: str) -> PaperTask | None:
        category_buckets = buckets[category]
        if not category_buckets:
            return None
        key = next(iter(category_buckets))
        task = category_buckets[key].pop(0)
        if category_buckets[key]:
            category_buckets[key] = category_buckets.pop(key)
        else:
            del category_buckets[key]
        return task

    rows = []
    overdue = [
        task
        for category_tasks in candidates.values()
        for task in category_tasks
        if getattr(task, "available_at", claimed_at) <= overdue_before
    ]
    if overdue:
        oldest = min(
            overdue,
            key=lambda task: (
                getattr(task, "available_at", claimed_at),
                getattr(task, "created_at", claimed_at),
            ),
        )
        category = (
            "interactive"
            if int(getattr(oldest, "priority", 50) or 50) >= 80
            else "normal"
            if int(getattr(oldest, "priority", 50) or 50) >= 20
            else "maintenance"
        )
        category_buckets = buckets[category]
        key = getattr(oldest, "batch_id", None) or oldest.task_id
        category_buckets[key].remove(oldest)
        if not category_buckets[key]:
            del category_buckets[key]
        rows.append(oldest)

    quota_cycle = (
        *(("interactive",) * 5),
        *(("normal",) * 3),
        "maintenance",
    )
    cycle_index = 0
    while len(rows) < limit and any(buckets.values()):
        preferred = quota_cycle[cycle_index % len(quota_cycle)]
        cycle_index += 1
        task = take_one(preferred)
        if task is None:
            task = next(
                (
                    candidate
                    for category in ("interactive", "normal", "maintenance")
                    if (candidate := take_one(category)) is not None
                ),
                None,
            )
        if task is not None:
            rows.append(task)
    reservations = []
    for task in rows:
        task.delivery_generation += 1
        task.delivery_status = "dispatching"
        task.delivery_lease_until = claimed_at + timedelta(seconds=lease_seconds)
        reservations.append(
            DeliveryReservation(task.task_id, queue_name, task.delivery_generation)
        )
    return reservations


def mark_delivery_enqueued(
    db,
    *,
    task_id: str,
    generation: int,
) -> bool:
    result = db.execute(
        update(PaperTask)
        .where(
            PaperTask.task_id == task_id,
            PaperTask.delivery_generation == generation,
            PaperTask.delivery_status == "dispatching",
        )
        .values(delivery_status="enqueued", delivery_lease_until=None)
    )
    return bool(result.rowcount)


def return_delivery_to_pending(
    db,
    *,
    task_id: str,
    generation: int,
) -> bool:
    result = db.execute(
        update(PaperTask)
        .where(
            PaperTask.task_id == task_id,
            PaperTask.delivery_generation == generation,
            PaperTask.delivery_status == "dispatching",
        )
        .values(delivery_status="pending", delivery_lease_until=None)
    )
    return bool(result.rowcount)


def release_expired_delivery_leases(
    db, *, now: datetime | None = None
) -> list[str]:
    released_at = now or datetime.now(UTC)
    task_ids = list(
        db.scalars(
            select(PaperTask.task_id)
            .where(
                PaperTask.delivery_status == "dispatching",
                PaperTask.pipeline_version == PIPELINE_VERSION,
                PaperTask.delivery_lease_until < released_at,
            )
            .with_for_update(skip_locked=True)
        ).all()
    )
    if task_ids:
        db.execute(
            update(PaperTask)
            .where(PaperTask.task_id.in_(task_ids))
            .values(delivery_status="pending", delivery_lease_until=None)
        )
    return task_ids


def recover_missing_enqueued_deliveries(
    db, connection, *, limit: int = 1000
) -> list[str]:
    rows = db.execute(
        select(
            PaperTask.task_id,
            PaperTask.delivery_generation,
        )
        .where(
            PaperTask.delivery_status == "enqueued",
            PaperTask.pipeline_version == PIPELINE_VERSION,
        )
        .order_by(PaperTask.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    missing = []
    active_statuses = {
        JobStatus.QUEUED,
        JobStatus.STARTED,
        JobStatus.SCHEDULED,
        JobStatus.DEFERRED,
    }
    for task_id, generation in rows:
        try:
            job = Job.fetch(
                DeliveryReservation(task_id, "", generation).job_id,
                connection=connection,
            )
            if job.get_status(refresh=False) in active_statuses:
                continue
        except NoSuchJobError:
            pass
        missing.append(task_id)
    if missing:
        db.execute(
            update(PaperTask)
            .where(
                PaperTask.task_id.in_(missing),
                PaperTask.delivery_status == "enqueued",
            )
            .values(delivery_status="pending", delivery_lease_until=None)
        )
    return missing


def release_expired_run_leases(
    db, *, now: datetime | None = None
) -> list[str]:
    released_at = now or datetime.now(UTC)
    rows = db.execute(
        select(
            PaperTask.task_id,
            PaperTask.root_task_id,
            PaperTask.run_token,
        )
        .where(
            PaperTask.run_token.is_not(None),
            PaperTask.pipeline_version == PIPELINE_VERSION,
            PaperTask.run_lease_until < released_at,
            PaperTask.status.in_(("running", "waiting_external")),
        )
        .with_for_update(skip_locked=True)
    ).all()
    task_ids = [row[0] for row in rows]
    if task_ids:
        db.execute(
            update(PaperTask)
            .where(PaperTask.task_id.in_(task_ids))
            .values(
                status="retry_wait",
                delivery_status="pending",
                available_at=released_at,
                run_token=None,
                run_lease_until=None,
                worker_id=None,
                heartbeat_at=released_at,
                error_code="RUN_LEASE_EXPIRED",
                error_domain="platform",
                error_message="Worker run lease expired; the stage will be retried.",
            )
        )
        db.execute(
            update(ExecutionAttempt)
            .where(
                ExecutionAttempt.owner_id.in_(task_ids),
                ExecutionAttempt.run_token.in_(
                    [row[2] for row in rows if row[2]]
                ),
                ExecutionAttempt.status == "running",
            )
            .values(
                status="interrupted",
                error_code="RUN_LEASE_EXPIRED",
                error_message=(
                    "Worker run lease expired; the stage will be retried."
                ),
                finished_at=released_at,
            )
        )
        root_ids = list({row[1] for row in rows if row[1]})
        if root_ids:
            db.execute(
                update(PaperTask)
                .where(
                    PaperTask.task_id.in_(root_ids),
                    PaperTask.operation == "pipeline",
                    PaperTask.status.in_(
                        ("pending", "running", "waiting_external", "retrying")
                    ),
                )
                .values(status="retrying", heartbeat_at=released_at)
            )
    return task_ids


def queue_delivery_capacity(
    connection,
    queue_name: str,
    *,
    buffer_multiplier: int,
) -> int:
    queue = Queue(queue_name, connection=connection)
    listeners = sum(
        queue_name in worker.queue_names()
        for worker in Worker.all(connection=connection)
    )
    buffer_size = listeners * buffer_multiplier
    occupied = queue.count + StartedJobRegistry(
        queue_name, connection=connection
    ).count
    return max(0, buffer_size - occupied)


def embedding_delivery_capacity(connection, db) -> int:
    listeners = sum(
        EMBEDDING_QUEUE in worker.queue_names()
        for worker in Worker.all(connection=connection)
    )
    config = db.scalar(
        select(ProviderConfig).where(
            ProviderConfig.role == "embedding",
            ProviderConfig.enabled.is_(True),
        )
    )
    if (
        not config
        or connection.get("provider:breaker:embedding")
        or listeners <= 0
    ):
        return 0
    now = datetime.now(UTC)
    key_capacity = int(
        db.scalar(
            select(func.coalesce(func.sum(ProviderCredential.max_concurrency), 0))
            .where(
                ProviderCredential.provider_config_id
                == config.provider_config_id,
                ProviderCredential.status == "active",
                or_(
                    ProviderCredential.cooldown_until.is_(None),
                    ProviderCredential.cooldown_until <= now,
                ),
            )
        )
        or 0
    )
    queue = Queue(EMBEDDING_QUEUE, connection=connection)
    occupied = queue.count + StartedJobRegistry(
        EMBEDDING_QUEUE, connection=connection
    ).count
    return max(0, min(listeners, key_capacity) - occupied)


def dispatch_pipeline_once(connection=None) -> dict:
    settings = get_settings()
    if not settings.pipeline_enabled:
        return {"status": "disabled", "reserved": 0, "enqueued": 0, "failed": 0}

    redis_connection = connection or redis.Redis.from_url(settings.redis_url)
    released_delivery = []
    released_runs = []
    recovered_enqueued = []
    expanded_batch_id = None
    db = SessionLocal()
    try:
        expanded_batch = PipelineBatchService(db).expand_next_manifest()
        expanded_batch_id = expanded_batch.batch_id if expanded_batch else None
        released_delivery = release_expired_delivery_leases(db)
        released_runs = release_expired_run_leases(db)
        recovered_enqueued = recover_missing_enqueued_deliveries(
            db, redis_connection
        )
        db.commit()
    finally:
        db.close()

    reservations: list[DeliveryReservation] = []
    for queue_name in PIPELINE_QUEUE_NAMES:
        if redis_connection.get(f"admin:queue:paused:{queue_name}"):
            continue
        db = SessionLocal()
        try:
            dispatching = int(
                db.scalar(
                    select(func.count())
                    .select_from(PaperTask)
                    .where(
                        PaperTask.queue_name == queue_name,
                        PaperTask.delivery_status == "dispatching",
                    )
                )
                or 0
            )
            available_capacity = (
                embedding_delivery_capacity(redis_connection, db)
                if queue_name == EMBEDDING_QUEUE
                else queue_delivery_capacity(
                    redis_connection,
                    queue_name,
                    buffer_multiplier=(
                        settings.pipeline_redis_buffer_multiplier
                    ),
                )
            )
            capacity = max(0, available_capacity - dispatching)
            if capacity <= 0:
                continue
            queue_reservations = reserve_due_deliveries(
                db,
                queue_name,
                limit=capacity,
                lease_seconds=settings.pipeline_delivery_lease_seconds,
                overdue_seconds=settings.pipeline_overdue_seconds,
            )
            db.commit()
            reservations.extend(queue_reservations)
        finally:
            db.close()

    enqueued = 0
    failed = 0
    for reservation in reservations:
        queue = Queue(reservation.queue_name, connection=redis_connection)
        try:
            queue.enqueue(
                "app.services.pipeline_tasks.execute_pipeline_stage",
                reservation.task_id,
                reservation.generation,
                job_id=reservation.job_id,
                job_timeout=settings.ingest_job_timeout_seconds,
                retry=Retry(max=3, interval=PIPELINE_JOB_RETRY_INTERVALS),
                unique=True,
            )
        except DuplicateJobError:
            job = Job.fetch(reservation.job_id, connection=redis_connection)
            if job.is_finished or job.is_failed or job.is_canceled or job.is_stopped:
                failed += 1
                db = SessionLocal()
                try:
                    return_delivery_to_pending(
                        db,
                        task_id=reservation.task_id,
                        generation=reservation.generation,
                    )
                    db.commit()
                finally:
                    db.close()
                continue
        except redis.exceptions.RedisError:
            failed += 1
            logger.warning(
                "Pipeline delivery failed for %s generation %s",
                reservation.task_id,
                reservation.generation,
                exc_info=True,
            )
            db = SessionLocal()
            try:
                return_delivery_to_pending(
                    db,
                    task_id=reservation.task_id,
                    generation=reservation.generation,
                )
                db.commit()
            finally:
                db.close()
            continue

        db = SessionLocal()
        try:
            if mark_delivery_enqueued(
                db,
                task_id=reservation.task_id,
                generation=reservation.generation,
            ):
                enqueued += 1
            db.commit()
        finally:
            db.close()

    return {
        "status": "ok",
        "expanded_batch_id": expanded_batch_id,
        "released_delivery": len(released_delivery),
        "released_runs": len(released_runs),
        "recovered_enqueued": len(recovered_enqueued),
        "reserved": len(reservations),
        "enqueued": enqueued,
        "failed": failed,
    }
