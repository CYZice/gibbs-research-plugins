from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import redis
from rq import Queue, Retry
from rq.exceptions import DuplicateJobError, NoSuchJobError
from rq.job import Job
from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.harvest import HarvestJob, HarvestJobItem
from app.services.harvest_service import HarvestError, HarvestService
from app.services.task_queue_constants import (
    HARVEST_DOWNLOAD_QUEUE,
    HARVEST_EXPAND_QUEUE,
)

logger = logging.getLogger(__name__)


def harvest_rq_job_id(*parts: str) -> str:
    """Build an RQ-compatible job ID from application-generated identifiers."""
    return "-".join(parts)


def harvest_download_job_id(item_id: str) -> str:
    return harvest_rq_job_id("harvest", "download", item_id)


def _download_queue():
    settings = get_settings()
    return Queue(
        HARVEST_DOWNLOAD_QUEUE,
        connection=redis.Redis.from_url(settings.redis_url),
    )


def _expand_queue():
    settings = get_settings()
    return Queue(
        HARVEST_EXPAND_QUEUE,
        connection=redis.Redis.from_url(settings.redis_url),
    )


def _download_delivery_is_active(connection, item_id: str) -> bool:
    job_id = harvest_download_job_id(item_id)
    try:
        job = Job.fetch(job_id, connection=connection)
    except NoSuchJobError:
        return False
    status = job.get_status(refresh=False)
    if status in {"queued", "started", "scheduled", "deferred"}:
        return True
    registries = (
        StartedJobRegistry(HARVEST_DOWNLOAD_QUEUE, connection=connection),
        ScheduledJobRegistry(HARVEST_DOWNLOAD_QUEUE, connection=connection),
    )
    return any(job_id in registry.get_job_ids() for registry in registries)


def enqueue_harvest_expand(job_id: str, *, delay_seconds: int = 0) -> None:
    queue = _expand_queue()
    kwargs = {
        "job_id": harvest_rq_job_id(
            "harvest", "expand", job_id, datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        ),
        "retry": Retry(max=3, interval=[30, 120, 600]),
        "job_timeout": 900,
        "result_ttl": 0,
    }
    try:
        if delay_seconds:
            queue.enqueue_in(
                timedelta(seconds=delay_seconds),
                run_harvest_expand,
                job_id,
                **kwargs,
            )
        else:
            queue.enqueue(run_harvest_expand, job_id, **kwargs)
    except DuplicateJobError:
        pass


def enqueue_harvest_download(item_id: str, *, delay_seconds: int = 0) -> None:
    settings = get_settings()
    queue = _download_queue()
    kwargs = {
        "job_id": harvest_download_job_id(item_id),
        "retry": Retry(max=2, interval=[120, 600]),
        "job_timeout": max(300, settings.discovery_request_timeout_seconds * 6),
        "result_ttl": 0,
        "failure_ttl": 604800,
    }
    try:
        if delay_seconds:
            queue.enqueue_in(
                timedelta(seconds=delay_seconds),
                run_harvest_download,
                item_id,
                **kwargs,
            )
        else:
            queue.enqueue(run_harvest_download, item_id, **kwargs)
    except DuplicateJobError:
        pass


def enqueue_harvest_content_batch(item_id: str, *, delay_seconds: int = 2) -> None:
    settings = get_settings()
    queue = _download_queue()
    try:
        kwargs = {
            "job_id": harvest_rq_job_id(
                "harvest", "content", item_id, datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
            ),
            "retry": Retry(max=2, interval=[120, 600]),
            "job_timeout": max(
                300, settings.discovery_request_timeout_seconds * 12
            ),
            "result_ttl": 0,
        }
        if delay_seconds:
            queue.enqueue_in(
                timedelta(seconds=delay_seconds),
                run_harvest_content_batch,
                item_id,
                **kwargs,
            )
        else:
            queue.enqueue(run_harvest_content_batch, item_id, **kwargs)
    except DuplicateJobError:
        pass


def enqueue_harvest_free_download_dispatch(
    job_id: str, *, delay_seconds: int = 2
) -> None:
    queue = _expand_queue()
    kwargs = {
        "job_id": harvest_rq_job_id(
            "harvest",
            "free-dispatch",
            job_id,
            datetime.now(UTC).strftime("%Y%m%d%H%M%S%f"),
        ),
        "retry": Retry(max=3, interval=[30, 120, 600]),
        "job_timeout": 300,
        "result_ttl": 0,
    }
    try:
        if delay_seconds:
            queue.enqueue_in(
                timedelta(seconds=delay_seconds),
                run_harvest_free_download_dispatch,
                job_id,
                **kwargs,
            )
        else:
            queue.enqueue(run_harvest_free_download_dispatch, job_id, **kwargs)
    except DuplicateJobError:
        pass


def run_harvest_expand(job_id: str) -> dict:
    settings = get_settings()
    if not settings.harvest_enabled:
        return {"status": "disabled"}
    db = SessionLocal()
    try:
        job = db.scalar(
            select(HarvestJob)
            .where(HarvestJob.job_id == job_id)
            .with_for_update(skip_locked=True)
        )
        if not job:
            return {"status": "missing_or_busy"}
        inserted = HarvestService(db, settings).expand_one_page(job)
        db.commit()
        pending = db.scalars(
            select(HarvestJobItem)
            .where(
                HarvestJobItem.job_id == job_id,
                HarvestJobItem.status == "pending",
            )
            .order_by(HarvestJobItem.created_at)
            .limit(100)
        ).all()
        if job.ingest_mode != "metadata_only":
            for item in pending:
                item.status = "resolving"
            db.commit()
            for item in pending:
                enqueue_harvest_download(item.item_id)
        db.refresh(job)
        if not job.enumeration_complete and job.status not in {
            "paused",
            "cancelled",
            "failed",
            "waiting_quota",
        }:
            enqueue_harvest_expand(
                job_id,
                delay_seconds=settings.harvest_dispatch_interval_seconds,
            )
        return {"status": job.status, "inserted": inserted, "dispatched": len(pending)}
    except HarvestError as exc:
        db.rollback()
        job = db.get(HarvestJob, job_id)
        if job:
            job.error_code = exc.code
            job.error_message = str(exc)
            if exc.code == "OPENALEX_FREE_QUOTA_EXHAUSTED":
                job.status = "waiting_quota"
                try:
                    job.next_retry_at = HarvestService(
                        db, settings
                    ).credentials.quota_summary(db).get("next_reset_at")
                except Exception:
                    job.next_retry_at = datetime.now(UTC) + timedelta(hours=1)
            elif not exc.retryable:
                job.status = "failed"
            db.commit()
        if exc.retryable and exc.code != "OPENALEX_FREE_QUOTA_EXHAUSTED":
            raise
        return {"status": job.status if job else "missing", "error_code": exc.code}
    finally:
        db.close()


def run_harvest_download(item_id: str) -> dict:
    settings = get_settings()
    if not settings.harvest_enabled:
        return {"status": "disabled"}
    db = SessionLocal()
    try:
        item = db.scalar(
            select(HarvestJobItem)
            .where(HarvestJobItem.item_id == item_id)
            # The dispatcher may have committed the delivery milliseconds earlier.
            # Wait for that short transaction instead of silently dropping this job.
            .with_for_update()
        )
        if not item:
            return {"status": "missing_or_busy"}
        if item.status not in {"pending", "resolving", "waiting_quota"}:
            return {"status": item.status, "item_id": item_id}
        HarvestService(db, settings).download_item(item)
        # download_item commits the network/storage paths itself, but its fast paths
        # (for example, a document already staged by another harvest job) only
        # update ORM state. Commit here so a successful RQ delivery cannot leave
        # the item stuck in "resolving".
        db.commit()
        return {"status": item.status, "item_id": item_id}
    except Exception as exc:
        db.rollback()
        item = db.get(HarvestJobItem, item_id)
        if item:
            item.status = (
                "pending"
                if item.attempts < settings.harvest_max_attempts
                else "failed"
            )
            item.error_code = getattr(exc, "code", "HARVEST_DOWNLOAD_FAILED")
            item.error_message = str(exc)[:1000]
            item.next_attempt_at = (
                datetime.now(UTC) + timedelta(minutes=2)
                if item.status == "pending"
                else None
            )
            db.commit()
        raise
    finally:
        db.close()


def run_harvest_free_download_dispatch(job_id: str) -> dict:
    settings = get_settings()
    if not settings.harvest_enabled:
        return {"status": "disabled"}
    db = SessionLocal()
    try:
        job = db.scalar(
            select(HarvestJob)
            .where(HarvestJob.job_id == job_id)
            .with_for_update(skip_locked=True)
        )
        if not job:
            return {"status": "missing_or_busy"}
        if job.status in {"paused", "cancelled", "failed"}:
            return {"status": job.status}
        now = datetime.now(UTC)
        active = db.scalar(
            select(func.count())
            .select_from(HarvestJobItem)
            .where(
                HarvestJobItem.job_id == job_id,
                HarvestJobItem.status.in_(("resolving", "downloading")),
            )
        ) or 0
        capacity = max(0, settings.harvest_expand_buffer_items - active)
        rows = []
        if capacity:
            rows = db.scalars(
                select(HarvestJobItem)
                .where(
                    HarvestJobItem.job_id == job_id,
                    HarvestJobItem.status == "pending",
                    HarvestJobItem.details["free_download_requested_at"].astext.is_not(
                        None
                    ),
                    (
                        HarvestJobItem.next_attempt_at.is_(None)
                        | (HarvestJobItem.next_attempt_at <= now)
                    ),
                )
                .order_by(HarvestJobItem.created_at)
                .limit(min(100, capacity))
                .with_for_update(skip_locked=True)
            ).all()
            for item in rows:
                item.status = "resolving"
        db.commit()
        for item in rows:
            enqueue_harvest_download(item.item_id)

        remaining = db.scalar(
            select(func.count())
            .select_from(HarvestJobItem)
            .where(
                HarvestJobItem.job_id == job_id,
                HarvestJobItem.status.in_(("pending", "resolving", "downloading")),
            )
        ) or 0
        if remaining:
            enqueue_harvest_free_download_dispatch(
                job_id,
                delay_seconds=settings.harvest_dispatch_interval_seconds,
            )
            return {"status": "dispatching", "dispatched": len(rows), "active": remaining}
        HarvestService(db, settings).reconcile_job(job)
        db.commit()
        return {"status": job.status, "dispatched": len(rows), "active": 0}
    finally:
        db.close()


def run_harvest_content_batch(seed_item_id: str) -> dict:
    settings = get_settings()
    if not settings.harvest_enabled:
        return {"status": "disabled"}
    db = SessionLocal()
    claimed_ids = []
    try:
        if not settings.harvest_allow_openalex_content:
            rows = db.scalars(
                select(HarvestJobItem).where(
                    HarvestJobItem.status == "resolving",
                    HarvestJobItem.details["phase"].astext == "content",
                )
            ).all()
            for item in rows:
                details = dict(item.details or {})
                details.pop("phase", None)
                item.status = "no_source"
                item.error_code = "OPENALEX_CONTENT_DISABLED"
                item.error_message = "OpenAlex Content downloads are disabled by configuration."
                item.details = {
                    **details,
                    "content_eligible": True,
                    "content_status": "not_requested",
                    "content_reason": "content_disabled",
                }
                HarvestService(db, settings).reconcile_job(db.get(HarvestJob, item.job_id))
            db.commit()
            return {"status": "content_disabled", "reset": len(rows)}
        rows = db.scalars(
            select(HarvestJobItem)
            .join(HarvestJob, HarvestJob.job_id == HarvestJobItem.job_id)
            .where(
                HarvestJobItem.status == "resolving",
                HarvestJobItem.details["phase"].astext == "content",
                HarvestJob.status.in_(("enumerating", "downloading")),
            )
            .order_by(
                (HarvestJobItem.item_id != seed_item_id),
                HarvestJobItem.updated_at,
            )
            .limit(max(1, settings.harvest_cli_workers * 5))
            .with_for_update(skip_locked=True)
        ).all()
        if not rows:
            return {"status": "empty"}
        claimed_ids = [item.item_id for item in rows]
        for item in rows:
            item.status = "downloading"
            item.details = {
                **dict(item.details or {}),
                "content_status": "downloading",
            }
        db.commit()
        processed = HarvestService(db, settings).download_content_batch(rows)
        next_item_id = db.scalar(
            select(HarvestJobItem.item_id)
            .join(HarvestJob, HarvestJob.job_id == HarvestJobItem.job_id)
            .where(
                HarvestJobItem.status == "resolving",
                HarvestJobItem.details["phase"].astext == "content",
                HarvestJob.status.in_(("enumerating", "downloading")),
            )
            .order_by(HarvestJobItem.updated_at)
            .limit(1)
        )
        if next_item_id:
            enqueue_harvest_content_batch(next_item_id)
        return {"status": "ok", "processed": processed}
    except HarvestError as exc:
        db.rollback()
        rows = db.scalars(
            select(HarvestJobItem).where(
                HarvestJobItem.item_id.in_(claimed_ids),
            )
        ).all()
        for item in rows:
            job = db.get(HarvestJob, item.job_id)
            if exc.code == "OPENALEX_FREE_QUOTA_EXHAUSTED":
                item.status = "waiting_quota"
                item.details = {
                    **dict(item.details or {}),
                    "content_status": "quota_waiting",
                    "content_reason": "daily_free_quota_exhausted",
                }
                if job:
                    job.status = "waiting_quota"
                    job.next_retry_at = HarvestService(
                        db, settings
                    ).credentials.quota_summary(db).get("next_reset_at")
            else:
                item.status = "resolving"
                item.details = {
                    **dict(item.details or {}),
                    "content_status": "queued",
                }
            item.error_code = exc.code
            item.error_message = str(exc)[:1000]
        db.commit()
        if exc.retryable and exc.code != "OPENALEX_FREE_QUOTA_EXHAUSTED":
            raise
        return {"status": "waiting_quota", "error_code": exc.code}
    except Exception:
        db.rollback()
        for item in db.scalars(
            select(HarvestJobItem).where(
                HarvestJobItem.item_id.in_(claimed_ids),
                HarvestJobItem.status == "downloading",
            )
        ).all():
            item.status = "resolving"
        db.commit()
        raise
    finally:
        db.close()


def resume_harvest_jobs() -> dict:
    settings = get_settings()
    if not settings.harvest_enabled:
        return {"status": "disabled"}
    now = datetime.now(UTC)
    db = SessionLocal()
    connection = redis.Redis.from_url(settings.redis_url)
    try:
        jobs = db.scalars(
            select(HarvestJob).where(
                HarvestJob.status.in_(("enumerating", "downloading", "waiting_quota"))
            )
        ).all()
        resumed = 0
        dispatched = 0
        recovered = 0
        stale_before = now - timedelta(
            seconds=max(600, settings.discovery_request_timeout_seconds * 6)
        )
        for item in db.scalars(
            select(HarvestJobItem).where(
                HarvestJobItem.status.in_(("resolving", "downloading")),
                HarvestJobItem.updated_at <= stale_before,
            )
        ).all():
            if _download_delivery_is_active(connection, item.item_id):
                continue
            if (
                item.status == "resolving"
                and (item.details or {}).get("phase") == "content"
            ):
                enqueue_harvest_content_batch(item.item_id, delay_seconds=0)
                continue
            item.status = "pending"
            item.error_code = None
            item.error_message = None
            item.next_attempt_at = None
            item.details = {
                **dict(item.details or {}),
                "delivery_recovered_at": now.isoformat(),
                "delivery_recovery_count": int(
                    (item.details or {}).get("delivery_recovery_count") or 0
                )
                + 1,
            }
            recovered += 1
        for job in jobs:
            if job.status == "waiting_quota":
                if job.next_retry_at and job.next_retry_at > now:
                    continue
                content_seed_id = None
                for item in db.scalars(
                    select(HarvestJobItem).where(
                        HarvestJobItem.job_id == job.job_id,
                        HarvestJobItem.status == "waiting_quota",
                    )
                ).all():
                    if (item.details or {}).get("phase") == "content":
                        item.status = "resolving"
                        item.details = {
                            **dict(item.details or {}),
                            "content_status": "queued",
                        }
                        content_seed_id = content_seed_id or item.item_id
                    else:
                        item.status = "pending"
                    item.next_attempt_at = None
                job.status = "downloading" if job.enumeration_complete else "enumerating"
                job.next_retry_at = None
                resumed += 1
                if content_seed_id:
                    enqueue_harvest_content_batch(content_seed_id, delay_seconds=0)
            enqueue_harvest_expand(job.job_id)
            if job.ingest_mode == "metadata_only":
                enqueue_harvest_free_download_dispatch(job.job_id, delay_seconds=0)
                continue
            pending_items = db.scalars(
                select(HarvestJobItem)
                .where(
                    HarvestJobItem.job_id == job.job_id,
                    HarvestJobItem.status == "pending",
                    (
                        HarvestJobItem.next_attempt_at.is_(None)
                        | (HarvestJobItem.next_attempt_at <= now)
                    ),
                )
                .order_by(HarvestJobItem.created_at)
                .limit(100)
            ).all()
            for item in pending_items:
                item.status = "resolving"
            db.commit()
            for item in pending_items:
                enqueue_harvest_download(item.item_id)
                dispatched += 1
        db.commit()
        return {
            "status": "ok",
            "resumed": resumed,
            "recovered": recovered,
            "dispatched": dispatched,
        }
    finally:
        db.close()
