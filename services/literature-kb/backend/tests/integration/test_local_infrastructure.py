from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import redis
from rq import Queue, SimpleWorker
from rq.scheduler import RQScheduler
from sqlalchemy import delete, select

pytestmark = pytest.mark.integration
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:16380/0")
POSTGRES_DSN = os.getenv(
    "TEST_POSTGRES_DSN",
    "postgresql://literature_test:literature_test@127.0.0.1:15433/literature_kb_test",
)


def probe(value: str) -> str:
    return value


@pytest.fixture(scope="module", autouse=True)
def local_infrastructure():
    if os.getenv("RUN_REAL_INFRA_TESTS") != "1":
        pytest.skip("Run backend/scripts/run_local_integration_tests.ps1.")


def test_real_postgres_redis_and_rq_round_trip():
    connection = redis.Redis.from_url(REDIS_URL)
    connection.flushdb()
    assert connection.ping()
    with psycopg.connect(POSTGRES_DSN) as database:
        assert database.execute("SELECT 1").fetchone() == (1,)

    queue = Queue("local-integration", connection=connection)
    job = queue.enqueue(probe, "ready")
    SimpleWorker([queue], connection=connection).work(burst=True)
    job.refresh()
    assert job.return_value() == "ready"


def test_current_pipeline_schema_is_migrated():
    with psycopg.connect(POSTGRES_DSN) as database:
        columns = {
            row[0]
            for row in database.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'paper_tasks'
                """
            )
        }
        indexes = {
            row[0]
            for row in database.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE tablename = 'paper_tasks'
                """
            )
        }
        staged_artifact_columns = {
            row[0]
            for row in database.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'staged_artifacts'
                """
            )
        }
        import_item_columns = {
            row[0]
            for row in database.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'import_batch_items'
                """
            )
        }

    assert {
        "queue_name",
        "delivery_status",
        "run_token",
        "run_lease_until",
        "pipeline_version",
        "pipeline_generation",
        "idempotency_key",
    }.issubset(columns)
    assert {
        "ix_paper_tasks_dispatch",
        "uq_pipeline_stage_per_root",
        "uq_paper_tasks_idempotency",
    }.issubset(indexes)
    assert {"artifact_id", "storage_key", "sha256", "paper_id"}.issubset(
        staged_artifact_columns
    )
    assert "staged_artifact_id" in import_item_columns


def test_pipeline_dispatch_and_claim_are_durable(monkeypatch):
    from app.core.config import get_settings
    from app.db.session import SessionLocal
    from app.models.paper_task import PaperTask
    from app.services import pipeline_dispatcher
    from app.services.pipeline_batch_service import PipelineBatchService
    from app.services.pipeline_service import claim_stage_task

    monkeypatch.setenv("PIPELINE_ENABLED", "true")
    get_settings.cache_clear()
    connection = redis.Redis.from_url(REDIS_URL)
    connection.flushdb()
    db = SessionLocal()
    try:
        service = PipelineBatchService(db)
        batch = service.create_remote_batch()
        service.append_remote_items(
            batch,
            [
                {
                    "options": [
                        {"source": "arxiv", "external_id": "1706.03762"}
                    ],
                    "preferred_source": "arxiv",
                    "license_acknowledged": False,
                }
            ],
        )
        batch.intake_closed = True
        db.commit()
        stage = db.scalar(
            select(PaperTask).where(
                PaperTask.batch_id == batch.batch_id,
                PaperTask.operation == "remote_fetch",
            )
        )
        assert stage is not None
    finally:
        db.close()

    monkeypatch.setattr(
        pipeline_dispatcher,
        "queue_delivery_capacity",
        lambda *_args, **_kwargs: 1,
    )
    result = pipeline_dispatcher.dispatch_pipeline_once(connection)
    assert result["enqueued"] == 1

    first = SessionLocal()
    second = SessionLocal()
    try:
        first_stage = first.get(PaperTask, stage.task_id)
        assert first_stage.delivery_status == "enqueued"
        claimed = claim_stage_task(
            first,
            task_id=first_stage.task_id,
            delivery_generation=first_stage.delivery_generation,
            run_token="run_integration_1",
            worker_id="worker_1",
            run_lease_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        first.commit()
        duplicate = claim_stage_task(
            second,
            task_id=first_stage.task_id,
            delivery_generation=first_stage.delivery_generation,
            run_token="run_integration_2",
            worker_id="worker_2",
            run_lease_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        second.commit()
    finally:
        first.close()
        second.close()
        connection.flushdb()
        get_settings.cache_clear()

    assert claimed is not None
    assert duplicate is None


def test_harvest_download_waits_for_real_postgres_item_lock(monkeypatch):
    from app.core.config import get_settings
    from app.db.session import SessionLocal
    from app.models.harvest import CorpusDocument, HarvestJob, HarvestJobItem
    from app.services.harvest_service import HarvestService
    from app.services.harvest_tasks import run_harvest_download

    job_id = "harvest_lock_integration"
    document_id = "corpus_lock_integration"
    item_id = "harvestitem_lock_integration"
    monkeypatch.setenv("HARVEST_ENABLED", "true")
    get_settings.cache_clear()

    setup = SessionLocal()
    try:
        setup.execute(
            delete(HarvestJobItem).where(HarvestJobItem.item_id == item_id)
        )
        setup.execute(
            delete(CorpusDocument).where(
                CorpusDocument.document_id == document_id
            )
        )
        setup.execute(delete(HarvestJob).where(HarvestJob.job_id == job_id))
        setup.add(
            HarvestJob(
                job_id=job_id,
                name="PostgreSQL lock integration",
                status="downloading",
                scope={},
                filters={},
                enumeration_complete=True,
            )
        )
        setup.add(
            CorpusDocument(
                document_id=document_id,
                provider="openalex",
                external_id="W_LOCK_INTEGRATION",
                metadata_payload={"id": "https://openalex.org/W_LOCK_INTEGRATION"},
                candidate_urls=[],
            )
        )
        setup.add(
            HarvestJobItem(
                item_id=item_id,
                job_id=job_id,
                document_id=document_id,
                status="resolving",
            )
        )
        setup.commit()
    finally:
        setup.close()

    entered_download = threading.Event()

    def fake_download(self, item):
        item.attempts += 1
        item.status = "no_source"
        self.db.commit()
        entered_download.set()

    monkeypatch.setattr(HarvestService, "download_item", fake_download)
    locker = SessionLocal()
    try:
        locked = locker.scalar(
            select(HarvestJobItem)
            .where(HarvestJobItem.item_id == item_id)
            .with_for_update()
        )
        assert locked is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_harvest_download, item_id)
            time.sleep(0.25)
            assert not future.done()
            assert not entered_download.is_set()
            locker.commit()
            result = future.result(timeout=5)
    finally:
        locker.close()

    verify = SessionLocal()
    try:
        item = verify.get(HarvestJobItem, item_id)
        assert result == {"status": "no_source", "item_id": item_id}
        assert entered_download.is_set()
        assert item is not None
        assert item.attempts == 1
        assert item.status == "no_source"
        verify.execute(
            delete(HarvestJobItem).where(HarvestJobItem.item_id == item_id)
        )
        verify.execute(
            delete(CorpusDocument).where(
                CorpusDocument.document_id == document_id
            )
        )
        verify.execute(delete(HarvestJob).where(HarvestJob.job_id == job_id))
        verify.commit()
    finally:
        verify.close()
        get_settings.cache_clear()


def test_real_rq_scheduled_job_is_promoted_and_executed():
    connection = redis.Redis.from_url(REDIS_URL)
    queue = Queue("local-scheduled", connection=connection)
    job = queue.enqueue_in(timedelta(seconds=1), probe, "scheduled")
    time.sleep(1.2)
    scheduler = RQScheduler([queue], connection=connection, interval=1)
    scheduler.acquire_locks()
    try:
        scheduler.enqueue_scheduled_jobs()
    finally:
        scheduler.release_locks()
    SimpleWorker([queue], connection=connection).work(burst=True)
    job.refresh()
    assert job.return_value() == "scheduled"
