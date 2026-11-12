import argparse
import os

import redis
from rq import SimpleWorker, Worker

from app.core.config import get_settings
from app.services.task_queue import ensure_maintenance_jobs
from app.services.task_queue_constants import (
    CONTENT_QUEUE,
    EMBEDDING_QUEUE,
    HARVEST_DOWNLOAD_QUEUE,
    HARVEST_EXPAND_QUEUE,
    INDEX_QUEUE,
    INGEST_QUEUE,
    INTERACTIVE_QUEUE,
    MAINTENANCE_QUEUE,
    PARSE_QUEUE,
    REMOTE_FETCH_QUEUE,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--role",
        choices=(
            "interactive",
            "ingest",
            "maintenance",
            "remote",
            "parse",
            "content",
            "embedding",
            "index",
            "harvest",
            "all",
        ),
        default="all",
    )
    args = parser.parse_args()
    settings = get_settings()
    settings.validate_runtime_config()
    # RQ's fork- and spawn-based workers both rely on POSIX wait APIs. Windows runs jobs inline.
    worker = SimpleWorker if os.name == "nt" else Worker
    queues = {
        "interactive": [INTERACTIVE_QUEUE],
        "ingest": [INGEST_QUEUE],
        "maintenance": [MAINTENANCE_QUEUE],
        "remote": [REMOTE_FETCH_QUEUE],
        "parse": [PARSE_QUEUE],
        "content": [CONTENT_QUEUE],
        "embedding": [EMBEDDING_QUEUE],
        "index": [INDEX_QUEUE],
        "harvest": [HARVEST_EXPAND_QUEUE, HARVEST_DOWNLOAD_QUEUE],
        "all": [
            INTERACTIVE_QUEUE,
            INGEST_QUEUE,
            MAINTENANCE_QUEUE,
            REMOTE_FETCH_QUEUE,
            PARSE_QUEUE,
            CONTENT_QUEUE,
            EMBEDDING_QUEUE,
            INDEX_QUEUE,
            HARVEST_EXPAND_QUEUE,
            HARVEST_DOWNLOAD_QUEUE,
        ],
    }[args.role]
    if args.role in {"maintenance", "all"}:
        ensure_maintenance_jobs()
    # Retry jobs use RQ's scheduled registry; at least one worker must promote them.
    worker(queues, connection=redis.Redis.from_url(settings.redis_url)).work(
        with_scheduler=True
    )
