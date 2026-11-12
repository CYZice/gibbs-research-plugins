from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from app.models.import_batch import ImportBatch, ImportBatchItem
from app.services.pipeline_service import (
    PIPELINE_QUEUE_BY_STAGE,
    PipelineInvariantError,
    build_claim_statement,
    complete_stage_and_schedule_successors,
    complete_stage_and_skip_remaining,
    dependencies_satisfied,
    fail_stage_and_root,
    new_pipeline_tasks,
    pipeline_progress,
    ready_successors,
    reconcile_import_batch,
    release_stage_run,
    request_pipeline_cancel,
    retry_pipeline_from_stage,
)
from app.services.task_queue_constants import INGEST_QUEUE, PARSE_QUEUE


def test_pipeline_dependencies_preserve_the_required_order():
    assert not dependencies_satisfied("register", set())
    assert dependencies_satisfied("register", {"remote_fetch"})
    assert not dependencies_satisfied("parse", {"remote_fetch"})
    assert dependencies_satisfied("parse", {"remote_fetch", "register"})
    assert not dependencies_satisfied("resource_context", {"parse"})
    assert dependencies_satisfied("resource_context", {"parse", "evidence"})
    assert not dependencies_satisfied("vectorize", {"evidence"})
    assert dependencies_satisfied(
        "vectorize", {"parse", "evidence", "resource_context"}
    )
    assert not dependencies_satisfied("index", {"evidence", "resource_context"})
    assert dependencies_satisfied("index", {"vectorize"})


def test_ready_successors_wait_for_every_dependency():
    assert ready_successors("remote_fetch", set()) == ("register",)
    assert ready_successors("register", {"remote_fetch"}) == ("parse",)
    assert ready_successors("parse", {"remote_fetch", "register"}) == ("evidence",)
    assert ready_successors("evidence", {"parse"}) == ("resource_context",)
    assert ready_successors("evidence", {"parse", "resource_context"}) == ("vectorize",)
    assert ready_successors("resource_context", {"parse", "evidence"}) == (
        "vectorize",
    )


def test_unknown_pipeline_stage_is_rejected():
    with pytest.raises(PipelineInvariantError):
        dependencies_satisfied("vision", set())


def test_pipeline_progress_counts_only_completed_stage_weights():
    assert pipeline_progress(set()) == 0.0
    assert pipeline_progress({"remote_fetch", "register", "parse"}) == 0.5
    assert pipeline_progress(
        {
            "remote_fetch",
            "register",
            "parse",
            "evidence",
            "resource_context",
            "vectorize",
            "index",
        }
    ) == 1.0


def test_new_pipeline_creates_only_the_initial_database_stage():
    now = datetime(2026, 7, 18, tzinfo=UTC)

    root, stage = new_pipeline_tasks(
        paper_id="paper_1",
        initial_stage="parse",
        provider="local",
        batch_id="batch_1",
        batch_item_id="item_1",
        now=now,
    )

    assert root.operation == "pipeline"
    assert root.stage == "parse"
    assert stage.root_task_id == root.task_id
    assert stage.operation == "parse"
    assert stage.queue_name == PARSE_QUEUE == PIPELINE_QUEUE_BY_STAGE["parse"]
    assert stage.delivery_status == "pending"
    assert stage.available_at == now
    assert stage.next_action == "submit"
    assert stage.pipeline_version == 3
    assert root.details["skipped_stages"] == [
        "remote_fetch",
        "register",
    ]


def test_register_is_the_common_pipeline_intake_for_staged_pdfs():
    now = datetime(2026, 7, 24, tzinfo=UTC)

    root, stage = new_pipeline_tasks(
        paper_id=None,
        initial_stage="register",
        provider="openalex_harvest",
        batch_id="batch_1",
        batch_item_id="item_1",
        now=now,
    )

    assert root.details["skipped_stages"] == ["remote_fetch"]
    assert stage.operation == "register"
    assert stage.queue_name == INGEST_QUEUE
    assert stage.delivery_status == "pending"
    assert ready_successors(
        "register", {"remote_fetch"}
    ) == ("parse",)


def test_claim_statement_requires_generation_and_unowned_enqueued_task():
    statement = build_claim_statement(
        task_id="task_1",
        delivery_generation=3,
        run_token="run_1",
        worker_id="worker_1",
        run_lease_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "paper_tasks.delivery_generation = 3" in sql
    assert "paper_tasks.delivery_status = 'enqueued'" in sql
    assert "paper_tasks.run_token IS NULL" in sql
    assert "RETURNING" in sql


def test_waiting_external_release_requires_a_future_delivery_time():
    class Db:
        def execute(self, _statement):
            raise AssertionError("invalid release must not execute SQL")

    with pytest.raises(PipelineInvariantError):
        release_stage_run(
            Db(),
            task_id="task_1",
            run_token="run_1",
            status="waiting_external",
            next_action="poll",
        )


def test_completing_evidence_creates_only_resource_context():
    root, task = new_pipeline_tasks(
        paper_id="paper_1",
        initial_stage="evidence",
        provider="local",
        batch_item_id="item_1",
    )
    task.run_token = "run_1"
    item = ImportBatchItem(
        item_id="item_1",
        batch_id="batch_1",
        source_key="source_1",
        status="running",
        current_stage="evidence",
        details={"stage": "evidence"},
    )
    added = []
    scalar_rows = [task, root]

    class Rows:
        def all(self):
            return [task]

    class Db:
        def scalar(self, _statement):
            return scalar_rows.pop(0)

        def scalars(self, _statement):
            return Rows()

        def add(self, row):
            added.append(row)

        @staticmethod
        def get(_model, _key):
            return item

    successors = complete_stage_and_schedule_successors(
        Db(), task_id=task.task_id, run_token="run_1"
    )

    assert task.status == "succeeded"
    assert [row.operation for row in successors] == ["resource_context"]
    assert added == successors
    assert root.stage == "resource_context"
    assert root.status == "running"
    assert item.current_stage == "resource_context"
    assert item.details["stage"] == "resource_context"


def test_duplicate_ready_skips_every_remaining_stage():
    root, task = new_pipeline_tasks(
        paper_id="paper_1",
        initial_stage="register",
        provider="core",
        batch_item_id="item_1",
    )
    task.run_token = "run_1"
    item = ImportBatchItem(
        item_id="item_1",
        batch_id="batch_1",
        source_key="source_1",
        status="running",
        current_stage="register",
        details={"stage": "register"},
    )
    added = []
    scalar_rows = [task, root]

    class Rows:
        def all(self):
            return ["remote_fetch", "register"]

    class Db:
        def scalar(self, _statement):
            return scalar_rows.pop(0)

        def scalars(self, _statement):
            return Rows()

        def add(self, row):
            added.append(row)

        @staticmethod
        def get(_model, _key):
            return item

    skipped = complete_stage_and_skip_remaining(
        Db(), task_id=task.task_id, run_token="run_1"
    )

    assert [row.operation for row in skipped] == [
        "parse",
        "evidence",
        "resource_context",
        "vectorize",
        "index",
    ]
    assert all(row.status == "skipped" for row in skipped)
    assert added == skipped
    assert root.status == "succeeded"
    assert root.progress == 1.0
    assert item.current_stage == "succeeded"
    assert item.details["stage"] == "succeeded"


def test_retry_reuses_stage_row_with_a_new_generation():
    root, task = new_pipeline_tasks(
        paper_id=None,
        initial_stage="remote_fetch",
        provider="core",
        batch_item_id="item_1",
    )
    task.status = "failed"
    item = ImportBatchItem(
        item_id="item_1",
        batch_id="batch_1",
        source_key="source_1",
        status="failed",
        current_stage="remote_fetch",
        details={"stage": "failed"},
    )
    scalar_rows = [task, root]

    class Rows:
        def all(self):
            return [task]

    class Db:
        def scalar(self, _statement):
            return scalar_rows.pop(0)

        def scalars(self, _statement):
            return Rows()

        @staticmethod
        def get(_model, _key):
            return item

    retried = retry_pipeline_from_stage(Db(), task_id=task.task_id)

    assert retried.pipeline_generation == 2
    assert task.pipeline_generation == 2
    assert task.status == "pending"
    assert task.delivery_status == "pending"
    assert task.idempotency_key.endswith(":2:remote_fetch")
    assert item.current_stage == "remote_fetch"
    assert item.details["stage"] == "remote_fetch"


def test_cancel_pending_pipeline_does_not_wait_for_a_worker():
    root, task = new_pipeline_tasks(
        paper_id=None,
        initial_stage="remote_fetch",
        provider="core",
    )
    scalar_rows = [root]

    class Rows:
        def all(self):
            return [task]

    class Db:
        def scalar(self, _statement):
            return scalar_rows.pop(0)

        def scalars(self, _statement):
            return Rows()

    cancelled = request_pipeline_cancel(Db(), root_task_id=root.task_id)

    assert cancelled.status == "cancelled"
    assert task.status == "cancelled"
    assert task.delivery_status == "idle"


def test_stale_pipeline_generation_cannot_commit_results():
    root, task = new_pipeline_tasks(
        paper_id="paper_1",
        initial_stage="parse",
        provider="local",
    )
    task.run_token = "run_1"
    root.pipeline_generation = 2
    scalar_rows = [task, root]

    class Db:
        def scalar(self, _statement):
            return scalar_rows.pop(0)

    with pytest.raises(PipelineInvariantError, match="generation is stale"):
        complete_stage_and_schedule_successors(
            Db(), task_id=task.task_id, run_token="run_1"
        )


def test_failed_stage_updates_batch_item_stage_details():
    root, task = new_pipeline_tasks(
        paper_id="paper_1",
        initial_stage="evidence",
        provider="local",
        batch_item_id="item_1",
    )
    task.run_token = "run_1"
    item = ImportBatchItem(
        item_id="item_1",
        batch_id="batch_1",
        source_key="source_1",
        status="running",
        current_stage="evidence",
        details={"stage": "register"},
    )
    scalar_rows = [task, root]

    class Db:
        def scalar(self, _statement):
            return scalar_rows.pop(0)

        @staticmethod
        def get(_model, _key):
            return item

    fail_stage_and_root(
        Db(),
        task_id=task.task_id,
        run_token="run_1",
        error_code="CONTENT_BUILD_FAILED",
        error_domain="internal",
        error_message="failed",
    )

    assert item.status == "failed"
    assert item.current_stage == "evidence"
    assert item.details["stage"] == "evidence"


def test_batch_reconciliation_counts_cancelled_items_without_losing_successes():
    batch = ImportBatch(
        batch_id="batch_1",
        source="remote:auto",
        status="running",
        total_items=2,
        completed_items=0,
        failed_items=0,
        intake_closed=True,
    )

    class Counts:
        @staticmethod
        def one():
            return 2, 1, 0, 1

    class Db:
        @staticmethod
        def scalar(_statement):
            return batch

        @staticmethod
        def flush():
            return None

        @staticmethod
        def execute(_statement):
            return Counts()

    reconciled = reconcile_import_batch(Db(), batch.batch_id)

    assert reconciled is batch
    assert batch.completed_items == 1
    assert batch.failed_items == 0
    assert batch.details["cancelled_items"] == 1
    assert batch.status == "completed_with_errors"
