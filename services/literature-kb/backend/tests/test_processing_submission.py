from types import SimpleNamespace

import pytest

from app.api.routes import admin
from app.api.routes.admin import _external_retryable, _latest_process_by_paper
from app.models.paper_task import PaperTask
from app.services.task_queue import submit_process_task


def test_submit_process_task_creates_one_active_pipeline_per_paper():
    paper = SimpleNamespace(paper_id="paper_1", parse_status="not_started")

    class Db:
        def __init__(self):
            self.active = None
            self.calls = 0
            self.added = []

        def scalar(self, _statement):
            self.calls += 1
            return paper if self.calls % 2 else self.active

        def add(self, value):
            self.added.append(value)

    db = Db()
    task, created = submit_process_task(db, paper.paper_id, provider="upload")
    db.active = task
    repeated, repeated_created = submit_process_task(db, paper.paper_id, provider="upload")

    assert created is True
    assert repeated_created is False
    assert repeated.task_id == task.task_id
    assert task.operation == "process"
    assert paper.parse_status == "queued"
    assert paper.evidence_status == "not_started"
    assert paper.embedding_status == "not_started"


def test_bulk_retry_filter_only_accepts_external_or_interrupted_process_errors():
    task = PaperTask(
        task_id="task_1",
        paper_id="paper_1",
        operation="process",
        status="failed",
        progress=0,
        details={"queue": {"error_code": "PROVIDER_RATE_LIMITED"}},
    )
    invalid_pdf = PaperTask(
        task_id="task_2",
        paper_id="paper_2",
        operation="process",
        status="failed",
        progress=0,
        details={"queue": {"error_code": "INVALID_INPUT"}},
    )
    interrupted = PaperTask(
        task_id="task_3",
        paper_id="paper_3",
        operation="process",
        status="interrupted",
        progress=0,
        details={},
    )

    assert _external_retryable(task)
    assert not _external_retryable(invalid_pdf)
    assert _external_retryable(interrupted)


def test_latest_process_selection_excludes_historical_failed_task():
    current = PaperTask(
        task_id="task_current",
        paper_id="paper_1",
        operation="process",
        status="succeeded",
        progress=1,
        details={"stage": "succeeded"},
    )
    historical = PaperTask(
        task_id="task_historical",
        paper_id="paper_1",
        operation="process",
        status="failed",
        progress=0,
        details={"queue": {"error_code": "PROVIDER_TIMEOUT"}},
    )

    latest = _latest_process_by_paper([current, historical])

    assert latest["paper_1"].task_id == "task_current"


def test_historical_task_cannot_be_retried():
    historical = PaperTask(
        task_id="task_historical",
        paper_id="paper_1",
        operation="process",
        status="failed",
        progress=0,
        details={},
    )
    current = PaperTask(
        task_id="task_current",
        paper_id="paper_1",
        operation="process",
        status="succeeded",
        progress=1,
        details={},
    )

    class Db:
        def get(self, _model, _task_id):
            return historical

        def scalar(self, _statement):
            return current

    with pytest.raises(Exception) as raised:
        admin.retry_task("task_historical", SimpleNamespace(headers={}), Db())

    assert getattr(raised.value, "status_code", None) == 409
