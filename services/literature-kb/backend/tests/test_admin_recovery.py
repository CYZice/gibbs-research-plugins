from types import SimpleNamespace

from app.api.routes.admin import _effective_process_status


def test_successful_embedding_repair_overrides_the_console_status_only():
    task = SimpleNamespace(task_id="task_process", status="failed", details={"recovery": {"status": "succeeded"}})

    assert _effective_process_status(task, set()) == "recovered"
    assert task.status == "failed"
