from types import SimpleNamespace

import requests

from app.models.paper_task import PaperTask
from app.services.external_errors import classify_external_error
from app.services.pipeline_tasks import _handle_stage_failure
from app.services.provider_control_service import ProviderError


class CodedError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def test_external_error_classifier_keeps_transient_and_permanent_failures_separate():
    timeout = classify_external_error(requests.Timeout("provider timed out"))
    invalid = classify_external_error(RuntimeError("invalid PDF format"))
    limited = classify_external_error(RuntimeError("HTTP 429 rate limit"))

    assert (timeout.code, timeout.retryable, timeout.recovery_action) == (
        "PROVIDER_TIMEOUT", True, "retry_later"
    )
    assert (invalid.domain, invalid.retryable, invalid.recovery_action) == (
        "input", False, "fix_input"
    )
    assert (limited.code, limited.retryable, limited.recovery_action) == (
        "EXTERNAL_RATE_LIMITED", True, "cooldown_and_retry"
    )


def test_external_error_classifier_reads_discovery_and_download_error_codes():
    limited = classify_external_error(
        CodedError(
            "UPSTREAM_RATE_LIMITED",
            "arXiv permits one request every three seconds.",
            retryable=True,
        )
    )
    storage = classify_external_error(
        CodedError("PDF_STORAGE_FAILED", "Could not store downloaded PDF.", retryable=True)
    )

    assert (limited.code, limited.retryable, limited.recovery_action) == (
        "EXTERNAL_RATE_LIMITED",
        True,
        "cooldown_and_retry",
    )
    assert (storage.code, storage.retryable) == ("PDF_STORAGE_FAILED", True)


def test_postgres_nul_error_is_not_retried_as_a_transient_failure():
    classified = classify_external_error(
        RuntimeError("PostgreSQL text fields cannot contain NUL (0x00) bytes")
    )

    assert (classified.code, classified.domain, classified.retryable) == (
        "DATA_INTEGRITY_BLOCKED",
        "data",
        False,
    )


def test_provider_capacity_wait_is_backpressure_not_a_pipeline_failure():
    task = SimpleNamespace(
        task_id="task_1",
        root_task_id="root_1",
        run_token="run_1",
        details={},
        error_code=None,
        error_domain=None,
        error_message=None,
        operation="vectorize",
        next_action="execute",
        max_attempts=4,
        paper_id="paper_1",
    )
    root = SimpleNamespace(status="running", stage="vectorize")
    attempt = SimpleNamespace(
        status="running",
        stage="vectorize",
        error_code=None,
        error_message=None,
        next_retry_at=None,
        finished_at=None,
    )

    class Db:
        committed = False

        @staticmethod
        def get(model, key):
            if model is PaperTask:
                return task if key == task.task_id else root
            raise AssertionError("Backpressure must not mark a Paper failed.")

        @staticmethod
        def scalar(_statement):
            return attempt

        @staticmethod
        def execute(_statement):
            return SimpleNamespace(rowcount=1)

        def commit(self):
            self.committed = True

    db = Db()
    _handle_stage_failure(
        db,
        task.task_id,
        task.run_token,
        ProviderError(
            "all keys are busy",
            role="embedding",
            provider="dashscope",
            error_code="PROVIDER_CAPACITY_WAIT",
            retry_after=1,
        ),
    )

    assert "failure_count" not in task.details
    assert task.details["backpressure_count"] == 1
    assert root.status == "retrying"
    assert attempt.status == "retrying"
    assert db.committed is True


def test_vectorize_progress_resets_consecutive_failure_count():
    task = SimpleNamespace(
        task_id="task_1",
        root_task_id="root_1",
        run_token="run_1",
        details={
            "failure_count": 3,
            "last_failure_progress": 32,
            "vectorize_progress": {"embedded_count": 96},
        },
        error_code=None,
        error_domain=None,
        error_message=None,
        operation="vectorize",
        next_action="execute",
        max_attempts=4,
        paper_id=None,
    )
    root = SimpleNamespace(status="running", stage="vectorize")
    attempt = SimpleNamespace(
        attempt_id="attempt_1",
        status="running",
        stage="vectorize",
        error_code=None,
        error_message=None,
        next_retry_at=None,
        finished_at=None,
    )

    class Db:
        @staticmethod
        def get(model, key):
            if model is PaperTask:
                return task if key == task.task_id else root
            return None

        @staticmethod
        def scalar(_statement):
            return attempt

        @staticmethod
        def execute(_statement):
            return SimpleNamespace(rowcount=1)

        @staticmethod
        def commit():
            pass

    _handle_stage_failure(
        Db(),
        task.task_id,
        task.run_token,
        requests.Timeout("provider timed out"),
    )

    assert task.details["failure_count"] == 1
    assert task.details["last_failure_progress"] == 96
    assert root.status == "retrying"
