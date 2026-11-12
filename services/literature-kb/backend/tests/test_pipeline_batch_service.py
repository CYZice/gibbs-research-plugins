from types import SimpleNamespace

from app.services.pipeline_batch_service import (
    PipelineBatchService,
    remote_item_key,
)


class _ScalarResult:
    @staticmethod
    def all():
        return []


class _RecordingDb:
    def __init__(self):
        self.operations = []

    def flush(self):
        self.operations.append("flush")

    def scalars(self, _statement):
        self.operations.append("insert")
        return _ScalarResult()

    def add_all(self, _objects):
        self.operations.append("add_all")


def test_remote_item_key_is_stable_across_json_key_order():
    first = {"preferred_source": None, "options": [{"source": "core", "external_id": "1"}]}
    second = {"options": [{"external_id": "1", "source": "core"}], "preferred_source": None}

    assert remote_item_key(first) == remote_item_key(second)


def test_batch_preparation_deduplicates_items_before_database_insert():
    batch = SimpleNamespace(batch_id="batch_1", priority=50)
    payload = {"source": "core", "external_id": "1"}
    service = PipelineBatchService(None)

    prepared = service._prepare(batch, [payload, dict(payload)])

    assert len(prepared) == 1
    assert prepared[0].root.operation == "pipeline"
    assert prepared[0].first_stage.operation == "remote_fetch"
    assert prepared[0].first_stage.root_task_id == prepared[0].root.task_id
    assert prepared[0].first_stage.batch_item_id == prepared[0].item_id


def test_append_flushes_new_batch_before_inserting_items():
    db = _RecordingDb()
    batch = SimpleNamespace(
        batch_id="batch_1",
        priority=50,
        total_items=0,
        expanded_items=0,
    )
    service = PipelineBatchService(db)

    service.append_remote_items(
        batch,
        [{"source": "core", "external_id": "1"}],
    )

    assert db.operations[:2] == ["flush", "insert"]


def test_jsonl_manifest_expands_in_resumable_chunks(tmp_path):
    path = tmp_path / "papers.jsonl"
    path.write_text(
        "\n".join(
            (
                '{"source":"core","external_id":"1"}',
                "not-json",
                '{"source":"arxiv","external_id":"1706.03762"}',
            )
        ),
        encoding="utf-8",
    )

    first, done = PipelineBatchService._manifest_chunk(path, 0, 2)
    second, final = PipelineBatchService._manifest_chunk(path, 2, 2)

    assert not done
    assert first[0][1]["external_id"] == "1"
    assert isinstance(first[1][1], ValueError)
    assert final
    assert second[0][1]["external_id"] == "1706.03762"


def test_retry_failed_items_selects_a_stage_instead_of_the_failed_root(monkeypatch):
    item = SimpleNamespace(
        pipeline_task_id="root_1",
        status="failed",
    )
    root = SimpleNamespace(
        task_id="root_1",
        pipeline_generation=1,
    )
    stage = SimpleNamespace(task_id="stage_1")

    class Result:
        @staticmethod
        def all():
            return [item]

    class Db:
        @staticmethod
        def scalars(_statement):
            return Result()

        @staticmethod
        def get(_model, _key):
            return root

        @staticmethod
        def scalar(statement):
            assert "paper_tasks.operation IN" in str(statement)
            return stage

    retried = []
    monkeypatch.setattr(
        "app.services.pipeline_batch_service.retry_pipeline_from_stage",
        lambda _db, *, task_id, priority: retried.append((task_id, priority)),
    )
    monkeypatch.setattr(
        "app.services.pipeline_batch_service.reconcile_import_batch",
        lambda *_args: None,
    )
    batch = SimpleNamespace(batch_id="batch_1", priority=70, status="failed")

    count = PipelineBatchService(Db()).retry_failed_items(batch)

    assert count == 1
    assert retried == [("stage_1", 70)]
    assert batch.status == "running"
