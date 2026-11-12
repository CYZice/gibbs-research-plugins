import base64
import json
import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from rq.exceptions import DuplicateJobError
from rq.job import JobStatus

from app.core.config import ModelEndpointConfig, Settings
from app.schemas.v1 import SystemHealthComponent
from app.services import task_queue
from app.services.model_gateway import DashScopeModelGateway
from app.services.provider_control_service import ProviderError, SecretCipher
from app.services.task_queue import _is_retryable, _retry_details
from app.services.trace import trace_operation


class _Response:
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after else {}


class _Http:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls = 0
        self.kwargs = []

    def post(self, *_args, **_kwargs):
        self.kwargs.append(_kwargs)
        response = self.responses[self.calls]
        self.calls += 1
        return response


class _Job:
    retries_left = 3


def test_endpoint_limits_default_to_unlimited():
    endpoint = ModelEndpointConfig(role="embedding", model="test")

    assert endpoint.rpm_limit is None
    assert endpoint.tpm_limit is None


def test_model_gateway_retries_only_transient_http_statuses(monkeypatch):
    http = _Http([_Response(503), _Response(200)])
    gateway = DashScopeModelGateway(Settings(app_env="test", model_retry_max_attempts=3), http)
    delays = []
    monkeypatch.setattr("app.services.model_gateway.time.sleep", delays.append)

    assert gateway._post_with_retry("https://provider.example") is http.responses[1]
    assert http.calls == 2
    assert delays and 1 <= delays[0] <= 1.5


def test_model_gateway_classifies_quota_403_separately_from_auth():
    response = _Response(403)
    response.text = "user quota is not enough"
    gateway = DashScopeModelGateway(Settings(app_env="test"), _Http([response]))

    class Control:
        error = None
        def lease_endpoint(self, *_args, preferred=None, **_kwargs):
            return nullcontext(preferred)
        def report_success(self, *_args): pass
        def report_failure(self, _endpoint, error): self.error = error
        def record_event(self, *_args): pass

    control = Control()
    gateway.control = control
    endpoint = ModelEndpointConfig(
        role="embedding", provider="dashscope", base_url="https://example.test",
        api_key="key", model="embed", credential_id="credential",
    )

    result = gateway._controlled_post(endpoint, "/embeddings", "embedding", max_attempts=1, json={})

    assert result is response
    assert control.error.error_code == "PROVIDER_QUOTA_EXHAUSTED"


def test_retry_details_keep_task_queued_until_retry_is_exhausted():
    details = _retry_details({"attempt": 1}, requests.Timeout("provider timed out"), _Job(), "dashscope")

    assert _is_retryable(requests.Timeout("provider timed out"))
    assert details["error_code"] == "PROVIDER_TIMEOUT"
    assert details["next_retry_at"]
    assert details["max_attempts"] == 4
    assert not _is_retryable(RuntimeError("HTTP 400 Bad Request"))


def test_rebuild_parent_waits_for_every_paper_child(monkeypatch):
    parent = SimpleNamespace(
        task_id="rebuild_1",
        status="running",
        progress=0.0,
        finished_at=None,
        details={"target_model": "text-embedding-3-small"},
    )
    children = [
        SimpleNamespace(status="succeeded", details={"parent_task_id": "rebuild_1"}),
        SimpleNamespace(status="succeeded", details={"parent_task_id": "rebuild_1"}),
        SimpleNamespace(status="running", details={"parent_task_id": "rebuild_1"}),
    ]

    class Rows:
        def all(self):
            return children

    class Db:
        commits = 0

        def get(self, _model, task_id):
            return parent if task_id == "rebuild_1" else None

        def scalars(self, _statement):
            return Rows()

        def commit(self):
            self.commits += 1

    finished = []
    monkeypatch.setattr(task_queue, "_finish_embedding_rebuild", lambda _db, task: finished.append(task))

    task_queue._update_embedding_rebuild_parent(Db(), children[0])

    assert parent.status == "running"
    assert parent.details["completed_papers"] == 2
    assert parent.details["failed_papers"] == 0
    assert not finished

    children[2].status = "succeeded"
    task_queue._update_embedding_rebuild_parent(Db(), children[2])

    assert finished == [parent]


def test_enqueue_failure_is_retained_for_watchdog_recovery(monkeypatch):
    task = SimpleNamespace(
        task_id="task_1",
        paper_id="paper_1",
        status="queued",
        error_message=None,
        details={
            "stage": "queued",
            "queue": {"attempt": 0, "max_attempts": 4, "job_id": "task_1"},
        },
    )

    class Db:
        def commit(self):
            pass

        def refresh(self, _owner, **_kwargs):
            pass

    def fail_enqueue(_task_id, job_id=None):
        raise task_queue.redis.exceptions.ConnectionError("redis down")

    monkeypatch.setattr(task_queue, "enqueue_task", fail_enqueue)
    monkeypatch.setattr(task_queue, "_mark_batch_item", lambda *_args: None)

    task_queue.enqueue_submitted_process(Db(), task)

    assert task.status == "retrying"
    assert task.details["stage"] == "retrying"
    assert task.details["queue"]["error_code"] == "QUEUE_UNAVAILABLE"
    assert task.details["queue"]["enqueue_attempt"] == 1
    assert task.details["queue"]["next_enqueue_at"]


def test_second_enqueue_failure_still_waits_for_watchdog_retry(monkeypatch):
    task = SimpleNamespace(
        task_id="task_1",
        paper_id="paper_1",
        status="queued",
        error_message=None,
        details={"stage": "retrying", "queue": {"enqueue_attempt": 1}},
    )

    class Db:
        def get(self, *_args):
            return None

    monkeypatch.setattr(task_queue, "_mark_batch_item", lambda *_args: None)

    finished = task_queue._record_enqueue_failure(
        Db(), task, task_queue.redis.exceptions.ConnectionError("redis down")
    )

    assert finished == []
    assert task.status == "retrying"
    assert task.details["stage"] == "retrying"
    assert task.details["queue"]["enqueue_attempt"] == 2
    assert task.details["queue"]["next_enqueue_at"]


def test_third_enqueue_failure_is_terminal(monkeypatch):
    task = SimpleNamespace(
        task_id="task_1",
        paper_id="paper_1",
        status="queued",
        error_message=None,
        details={"stage": "retrying", "queue": {"enqueue_attempt": 2}},
    )

    class Db:
        def get(self, *_args):
            return None

    monkeypatch.setattr(task_queue, "_finish_batch_item", lambda *_args: ["task_1"])

    finished = task_queue._record_enqueue_failure(
        Db(), task, task_queue.redis.exceptions.ConnectionError("redis down")
    )

    assert finished == ["task_1"]
    assert task.status == "failed"
    assert task.details["stage"] == "failed"
    assert task.details["queue"]["enqueue_attempt"] == 3
    assert task.details["queue"]["error_code"] == "QUEUE_UNAVAILABLE"
    assert "next_enqueue_at" not in task.details["queue"]


def test_enqueue_task_reuses_an_active_duplicate_job(monkeypatch):
    captured = {}

    class Db:
        def get(self, _model, _task_id):
            return SimpleNamespace(operation="process")

        def close(self):
            pass

    class Connection:
        def get(self, _key):
            return None

    class QueueStub:
        def __init__(self, _name, connection):
            captured["connection"] = connection

        def enqueue(self, *_args, **kwargs):
            captured["kwargs"] = kwargs
            raise DuplicateJobError()

    existing = SimpleNamespace(id="task_1", get_status=lambda: JobStatus.QUEUED)
    monkeypatch.setattr(task_queue, "SessionLocal", Db)
    monkeypatch.setattr(task_queue.redis.Redis, "from_url", lambda _url: Connection())
    monkeypatch.setattr(task_queue, "Queue", QueueStub)
    monkeypatch.setattr(task_queue.Job, "fetch", lambda *_args, **_kwargs: existing)

    assert task_queue.enqueue_task("task_1") == "task_1"
    assert captured["kwargs"]["unique"] is True
    assert captured["kwargs"]["job_timeout"] == 7200


def test_enqueue_task_rejects_a_terminal_duplicate_job(monkeypatch):
    class Db:
        def get(self, _model, _task_id):
            return SimpleNamespace(operation="process")

        def close(self):
            pass

    class Connection:
        def get(self, _key):
            return None

    class QueueStub:
        def __init__(self, _name, connection):
            pass

        def enqueue(self, *_args, **_kwargs):
            raise DuplicateJobError()

    existing = SimpleNamespace(id="task_1", get_status=lambda: JobStatus.FAILED)
    monkeypatch.setattr(task_queue, "SessionLocal", Db)
    monkeypatch.setattr(task_queue.redis.Redis, "from_url", lambda _url: Connection())
    monkeypatch.setattr(task_queue, "Queue", QueueStub)
    monkeypatch.setattr(task_queue.Job, "fetch", lambda *_args, **_kwargs: existing)

    with pytest.raises(task_queue.QueueJobTerminatedError):
        task_queue.enqueue_task("task_1")


def test_terminal_duplicate_is_not_recorded_as_redis_outage(monkeypatch):
    task = SimpleNamespace(
        task_id="task_1",
        paper_id="paper_1",
        status="retrying",
        error_message=None,
        finished_at=None,
        details={"stage": "retrying", "queue": {"job_id": "task_1", "enqueue_attempt": 1}},
    )

    class Db:
        def commit(self):
            pass

        def refresh(self, _owner, **_kwargs):
            pass

        def get(self, *_args):
            return None

    def fail_enqueue(*_args, **_kwargs):
        raise task_queue.QueueJobTerminatedError("job already failed")

    monkeypatch.setattr(task_queue, "enqueue_task", fail_enqueue)
    monkeypatch.setattr(task_queue, "_finish_batch_item", lambda *_args: [])

    task_queue.enqueue_submitted_process(Db(), task)

    assert task.status == "failed"
    assert task.details["queue"]["error_code"] == "QUEUE_JOB_TERMINATED"
    assert "next_enqueue_at" not in task.details["queue"]


def test_already_started_worker_state_is_not_overwritten(monkeypatch):
    task = SimpleNamespace(
        task_id="task_1",
        status="queued",
        error_message=None,
        details={"stage": "queued", "queue": {"job_id": "task_1"}},
    )

    class Db:
        def commit(self):
            pass

        def refresh(self, owner, **_kwargs):
            owner.status = "running"
            owner.details = {"stage": "parsing", "queue": {"delivery_status": "started"}}

    monkeypatch.setattr(task_queue, "enqueue_task", lambda *_args, **_kwargs: "task_1")

    task_queue.enqueue_submitted_process(Db(), task)

    assert task.status == "running"
    assert task.details["stage"] == "parsing"


def test_third_workflow_enqueue_failure_is_terminal():
    run = SimpleNamespace(
        status="retrying",
        error_message=None,
        finished_at=None,
        details={"stage": "retrying", "queue": {"enqueue_attempt": 2}},
    )

    terminal = task_queue._record_owner_enqueue_failure(
        run,
        task_queue.redis.exceptions.ConnectionError("redis down"),
        retryable=True,
    )

    assert terminal is True
    assert run.status == "failed"
    assert run.details["queue"]["enqueue_attempt"] == 3
    assert "next_enqueue_at" not in run.details["queue"]


def test_trace_operation_redacts_query_body_and_records_hash(caplog):
    caplog.set_level("INFO", logger="app.trace")
    with trace_operation("papers.search", request_id="req_test", query_length=7, query_sha256="hash") as trace:
        trace.record("papers_search_candidates", {"lexical_candidates": 3})

    payloads = [json.loads(record.message) for record in caplog.records]
    start = next(payload for payload in payloads if payload["event"] == "trace_start")
    assert start["operation"] == "papers.search"
    assert start["metadata"] == {"query_length": 7, "query_sha256": "hash"}


def test_worker_enables_rq_scheduler_for_delayed_retries():
    source = (Path(__file__).parents[1] / "app" / "worker.py").read_text(encoding="utf-8")

    assert "with_scheduler=True" in source


def test_workflow_queue_uses_configured_job_timeout(monkeypatch):
    captured = {}

    class Connection:
        def get(self, _key):
            return None

    class FakeQueue:
        def __init__(self, name, connection):
            captured["name"] = name
            captured["connection"] = connection

        def enqueue(self, function, *args, **kwargs):
            captured["function"] = function
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(id="job_test")

    monkeypatch.setattr(
        task_queue,
        "get_settings",
        lambda: Settings(app_env="test", workflow_job_timeout_seconds=900),
    )
    monkeypatch.setattr(task_queue.redis.Redis, "from_url", lambda _url: Connection())
    monkeypatch.setattr(task_queue, "Queue", FakeQueue)

    assert task_queue.enqueue_workflow("run_test") == "job_test"
    assert captured["name"] == task_queue.INTERACTIVE_QUEUE
    assert captured["kwargs"]["job_timeout"] == 900
    assert captured["kwargs"]["unique"] is True


def test_workflow_enqueue_outage_is_retained_for_watchdog(monkeypatch):
    run = SimpleNamespace(
        run_id="run_1",
        status="queued",
        error_message=None,
        finished_at=None,
        details={
            "stage": "queued",
            "queue": {"job_id": "run_1", "delivery_status": "pending", "enqueue_attempt": 0},
        },
    )

    class Db:
        def commit(self):
            pass

        def refresh(self, _owner, **_kwargs):
            pass

    def fail_enqueue(*_args, **_kwargs):
        raise task_queue.redis.exceptions.TimeoutError("redis response timed out")

    monkeypatch.setattr(task_queue, "_enqueue_unique", fail_enqueue)

    task_queue.enqueue_submitted_workflow(Db(), run)

    assert run.status == "retrying"
    assert run.details["queue"]["delivery_status"] == "retrying"
    assert run.details["queue"]["error_code"] == "QUEUE_UNAVAILABLE"
    assert run.details["queue"]["next_enqueue_at"]


def test_terminal_and_stale_jobs_cannot_claim_database_owner():
    terminal = SimpleNamespace(
        status="failed", details={"queue": {"job_id": "task_1"}}
    )
    current = SimpleNamespace(
        status="queued", details={"queue": {"job_id": "task_2"}}
    )

    assert not task_queue._can_start_owner(terminal, SimpleNamespace(id="task_1"))
    assert not task_queue._can_start_owner(current, SimpleNamespace(id="old_job"))
    assert task_queue._can_start_owner(current, SimpleNamespace(id="task_2"))
    current.status = "running"
    assert task_queue._can_start_owner(current, SimpleNamespace(id="task_2"))


def test_pending_delivery_is_retried_after_orphan_grace():
    now = datetime.now(UTC)
    queue = {"delivery_status": "pending"}

    assert not task_queue._delivery_retry_due(queue, now, now, now - timedelta(minutes=5))
    assert task_queue._delivery_retry_due(
        queue, now - timedelta(minutes=5), now, now - timedelta(minutes=5)
    )


def test_orphan_workflow_gets_a_new_delivery_job_id():
    run = SimpleNamespace(
        status="running",
        details={"stage": "retrieval_and_judging", "queue": {"job_id": "run_1"}},
    )

    task_queue._recover_orphan(run, "run_1", "workflow")

    assert run.status == "retrying"
    assert run.details["stage"] == "retrying"
    assert run.details["queue"]["job_id"] == "run_1-recovery-1"
    assert run.details["queue"]["error_code"] == "ORPHAN_WORKFLOW_RECOVERED"


def test_cancelled_legacy_process_task_does_not_dispatch_more_imports():
    item = SimpleNamespace(batch_id="batch_1", status="retrying", error_message=None)
    batch = SimpleNamespace(
        batch_id="batch_1", failed_items=0, completed_items=0, status="processing"
    )
    task = SimpleNamespace(
        status="retrying",
        cancel_requested_at=None,
        finished_at=None,
        error_message=None,
        details={"batch_item_id": "item_1", "queue": {"next_enqueue_at": "later"}},
    )

    class Db:
        def get(self, model, _owner_id):
            return item if model is task_queue.ImportBatchItem else batch

        def scalar(self, _statement):
            return None

    next_ids = task_queue.cancel_process_task(Db(), task)

    assert next_ids == []
    assert task.status == "cancelled"
    assert item.status == "cancelled"
    assert batch.failed_items == 1
    assert task.details["queue"]["delivery_status"] == "cancelled"
    assert "next_enqueue_at" not in task.details["queue"]


def test_one_legacy_processing_task_completes_all_attached_batch_items():
    items = {
        "item_1": SimpleNamespace(
            batch_id="batch_1", status="running", error_message=None, details={}
        ),
        "item_2": SimpleNamespace(
            batch_id="batch_2", status="queued", error_message=None, details={}
        ),
    }
    batches = {
        "batch_1": SimpleNamespace(
            batch_id="batch_1", failed_items=0, completed_items=0, status="processing"
        ),
        "batch_2": SimpleNamespace(
            batch_id="batch_2", failed_items=0, completed_items=0, status="processing"
        ),
    }
    task = SimpleNamespace(
        task_id="task_shared",
        details={"batch_item_id": "item_1", "batch_item_ids": ["item_1", "item_2"]},
    )

    class Db:
        def get(self, model, owner_id):
            return items[owner_id] if model is task_queue.ImportBatchItem else batches[owner_id]

        def scalar(self, _statement):
            return None

    next_ids = task_queue._finish_batch_item(Db(), task, "succeeded")

    assert next_ids == []
    assert all(item.status == "succeeded" for item in items.values())
    assert all(item.details["stage"] == "ready" for item in items.values())
    assert all(batch.status == "succeeded" for batch in batches.values())


def test_watchdog_tick_failure_does_not_escape(monkeypatch):
    def fail_watchdog():
        raise RuntimeError("db down")

    monkeypatch.setattr(task_queue, "watchdog", fail_watchdog)

    result = task_queue.watchdog_tick()

    assert result == {"error_code": "WATCHDOG_TICK_FAILED", "error_type": "RuntimeError"}


def test_concurrent_watchdog_tick_is_skipped(monkeypatch):
    captured = {}

    class Lock:
        def acquire(self, **kwargs):
            captured["acquire"] = kwargs
            return False

    class Connection:
        def lock(self, key, timeout):
            captured["lock"] = (key, timeout)
            return Lock()

    monkeypatch.setattr(task_queue.redis.Redis, "from_url", lambda _url: Connection())
    monkeypatch.setattr(
        task_queue,
        "get_settings",
        lambda: SimpleNamespace(redis_url="redis://test", task_orphan_grace_seconds=300),
    )

    result = task_queue.watchdog()

    assert result == {"skipped": "already_running"}
    assert captured["lock"] == (task_queue.WATCHDOG_LOCK_KEY, 300)
    assert captured["acquire"] == {"blocking": False}


def test_terminal_maintenance_job_is_replaced_atomically(monkeypatch):
    captured = {}

    class Existing:
        def get_status(self):
            return JobStatus.FAILED

        def delete(self, **kwargs):
            captured["deleted"] = kwargs

    class QueueStub:
        def __init__(self, name, connection):
            captured["queue"] = name

        def enqueue(self, function, **kwargs):
            captured["function"] = function
            captured["kwargs"] = kwargs

    monkeypatch.setattr(task_queue.Job, "fetch", lambda *_args, **_kwargs: Existing())
    monkeypatch.setattr(task_queue, "Queue", QueueStub)

    task_queue._ensure_repeating_job(
        object(), "maintenance_watchdog", task_queue.watchdog_tick, times=10, interval=60
    )

    assert captured["deleted"] == {"remove_from_queue": True}
    assert captured["kwargs"]["unique"] is True


def test_system_health_accepts_structured_scheduler_details():
    component = SystemHealthComponent(
        name="rq_scheduler", status="healthy", latency_ms=0,
        detail={"scheduled": 2, "overdue": 0},
    )

    assert component.detail == {"scheduled": 2, "overdue": 0}


def test_provider_key_encryption_requires_master_key_and_round_trips():
    with pytest.raises(ValueError):
        SecretCipher(None).encrypt("secret-key")
    cipher = SecretCipher(base64.b64encode(os.urandom(32)).decode())
    encrypted = cipher.encrypt("secret-key")

    assert "secret-key" not in encrypted
    assert cipher.decrypt(encrypted) == "secret-key"


def test_structured_provider_error_controls_task_retry():
    retryable = ProviderError(
        "rate limited", role="embedding", provider="dashscope",
        http_status=429, error_code="PROVIDER_RATE_LIMITED", retryable=True,
    )
    permanent = ProviderError(
        "bad key", role="embedding", provider="dashscope",
        http_status=401, error_code="PROVIDER_AUTH_FAILED", retryable=False,
    )

    assert _is_retryable(retryable)
    assert not _is_retryable(permanent)


def test_controlled_provider_retry_rotates_away_from_rate_limited_key(monkeypatch):
    first = ModelEndpointConfig(role="embedding", provider="dashscope", base_url="https://one", api_key="key-one", model="embed", credential_id="one")
    second = first.model_copy(update={"base_url": "https://two", "api_key": "key-two", "credential_id": "two"})
    http = _Http([_Response(429, "0"), _Response(200)])
    gateway = DashScopeModelGateway(Settings(app_env="test", model_retry_max_attempts=2), http)

    class Control:
        def resolve_endpoint(self, _role, _exclude): return second
        def lease_endpoint(self, *_args, preferred=None, **_kwargs):
            return nullcontext(preferred)
        def report_success(self, *_args): pass
        def report_failure(self, *_args): pass
        def record_event(self, *_args): pass

    gateway.control = Control()
    monkeypatch.setattr("app.services.model_gateway.time.sleep", lambda _value: None)
    response = gateway._controlled_post(first, "/embeddings", "embedding", json={})

    assert response.status_code == 200
    assert http.kwargs[0]["headers"]["Authorization"] == "Bearer key-one"
    assert http.kwargs[1]["headers"]["Authorization"] == "Bearer key-two"


def test_online_rerank_can_disable_gateway_retries():
    endpoint = ModelEndpointConfig(
        role="rerank", provider="dashscope", base_url="https://one",
        api_key="key-one", model="rerank", credential_id="one",
    )
    http = _Http([_Response(429, "0"), _Response(200)])
    gateway = DashScopeModelGateway(Settings(app_env="test", model_retry_max_attempts=3), http)

    class Control:
        def lease_endpoint(self, *_args, preferred=None, **_kwargs):
            return nullcontext(preferred)
        def report_success(self, *_args): pass
        def report_failure(self, *_args): pass
        def record_event(self, *_args): pass

    gateway.control = Control()
    with pytest.raises(ProviderError):
        gateway._controlled_post(endpoint, "/reranks", "rerank", max_attempts=1, json={})

    assert len(http.kwargs) == 1
