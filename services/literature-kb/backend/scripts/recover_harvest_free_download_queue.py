from __future__ import annotations

import argparse
from datetime import UTC, datetime

import redis
from rq import Queue
from rq.job import Job
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.harvest import HarvestJob, HarvestJobItem
from app.services.harvest_tasks import HARVEST_DOWNLOAD_QUEUE


def parse_args():
    parser = argparse.ArgumentParser(
        description="Recover queued free-download Harvest jobs without re-enumerating metadata."
    )
    parser.add_argument("--job-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    settings = get_settings()
    db = SessionLocal()
    queue = Queue(
        HARVEST_DOWNLOAD_QUEUE,
        connection=redis.Redis.from_url(settings.redis_url),
    )
    queued_ids = queue.get_job_ids()
    target_jobs = set(args.job_id)
    candidate_items = 0
    active_items = 0
    for job_id in target_jobs:
        candidate_items += db.scalar(
            select(func.count())
            .select_from(HarvestJobItem)
            .where(
                HarvestJobItem.job_id == job_id,
                HarvestJobItem.status.in_(("pending", "resolving", "downloading")),
            )
        ) or 0
        active_items += db.scalar(
            select(func.count())
            .select_from(HarvestJobItem)
            .where(
                HarvestJobItem.job_id == job_id,
                HarvestJobItem.status.in_(("resolving", "downloading")),
            )
        ) or 0
    selected_queue_ids = []
    for queued in queued_ids:
        try:
            job = Job.fetch(queued, connection=queue.connection)
        except Exception:
            continue
        item_id = (job.args or [None])[0]
        if not item_id:
            continue
        item = db.get(HarvestJobItem, item_id)
        if (
            item
            and item.job_id in target_jobs
            and job.func_name.endswith("run_harvest_download")
        ):
            selected_queue_ids.append(job.id)
    print(
        {
            "job_ids": sorted(target_jobs),
            "download_queue_entries": len(selected_queue_ids),
            "candidate_items": candidate_items,
            "active_items": active_items,
            "apply": args.apply,
        }
    )
    if not args.apply:
        return
    for queue_job_id in selected_queue_ids:
        try:
            job = Job.fetch(queue_job_id, connection=queue.connection)
            job.delete(remove_from_queue=True, delete_dependents=True)
        except Exception:
            pass
    now = datetime.now(UTC).isoformat()
    for item in db.scalars(
        select(HarvestJobItem).where(
            HarvestJobItem.job_id.in_(sorted(target_jobs)),
            HarvestJobItem.status.in_(("resolving", "downloading")),
        )
    ).all():
        item.status = "pending"
        item.error_code = None
        item.error_message = None
        item.next_attempt_at = None
        item.details = {
            **dict(item.details or {}),
            "delivery_recovered_at": now,
            "delivery_recovery_count": int(
                (item.details or {}).get("delivery_recovery_count") or 0
            )
            + 1,
        }
    for job in db.scalars(
        select(HarvestJob).where(HarvestJob.job_id.in_(sorted(target_jobs)))
    ).all():
        if job.status == "paused":
            continue
        job.status = "downloading" if job.enumeration_complete else "enumerating"
        job.next_retry_at = None
    db.commit()


if __name__ == "__main__":
    main()
