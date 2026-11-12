from types import SimpleNamespace

from app.core.config import ModelEndpointConfig, Settings
from app.services.embedding_index_service import (
    VECTOR_DIMENSION,
    EmbeddingIndexService,
    _EmbeddingTarget,
)


class Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def all(self):
        return self.rows


def test_embedding_batches_respect_count_and_character_budgets():
    service = EmbeddingIndexService(
        None,
        Settings(embedding_batch_size=3, embedding_batch_max_chars=1000),
        model_gateway=SimpleNamespace(),
    )
    targets = [
        _EmbeddingTarget(str(index), "x" * length, str(index))
        for index, length in enumerate((400, 400, 400, 100, 100, 100, 100))
    ]

    batches = service._embedding_batches(targets)

    assert [len(batch) for batch in batches] == [2, 3, 2]
    assert all(sum(len(item.text) for item in batch) <= 1000 for batch in batches)


def test_oversized_representation_is_skipped_without_blocking_short_text():
    embedded = []
    deleted = []
    existing = SimpleNamespace(
        representation_id="repr_large",
        status="success",
        embedding_dim=VECTOR_DIMENSION,
        text_hash="large-hash",
    )

    class Db:
        commits = 0

        def scalars(self, _statement):
            return Result([existing])

        def delete(self, item):
            deleted.append(item)

        def execute(self, _statement):
            pass

        def commit(self):
            self.commits += 1

    class Gateway:
        def resolve_embedding_endpoint(self, _fallback):
            return ModelEndpointConfig(role="embedding", model="test-embedding")

        def embed_texts_for_endpoint(self, texts, _endpoint):
            embedded.extend(texts)
            return [[0.0] * VECTOR_DIMENSION for _ in texts]

    large = SimpleNamespace(
        representation_id="repr_large",
        text="x" * 1001,
        text_hash="large-hash",
        representation_metadata={"source": "table_caption_context"},
    )
    short = SimpleNamespace(
        representation_id="repr_short",
        text="short evidence",
        text_hash="short-hash",
        representation_metadata=None,
    )
    service = EmbeddingIndexService(
        Db(),
        Settings(
            app_env="test",
            embedding_max_input_chars=1000,
            embedding_batch_max_chars=2000,
        ),
        Gateway(),
    )

    result = service._embed([large, short], "paper_1", False)

    assert embedded == ["short evidence"]
    assert deleted == [existing]
    assert large.representation_metadata == {
        "source": "table_caption_context",
        "embedding": {
            "status": "skipped",
            "reason": "input_too_large",
            "input_chars": 1001,
            "max_input_chars": 1000,
        },
    }
    assert result.embedded_count == 1
    assert result.skipped_count == 1
