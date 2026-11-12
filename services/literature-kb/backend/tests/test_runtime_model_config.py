from types import SimpleNamespace

from app.core.config import ModelEndpointConfig, Settings
from app.models.paper import Paper
from app.models.paper_content import PaperContent
from app.schemas.v1 import EvidenceSearchRequest
from app.services.embedding_index_service import (
    VECTOR_DIMENSION,
    EmbeddingIndexService,
)
from app.services.retrieval_service import RetrievalService
from app.services.workflow_service import EvidencePackWorkflow


class Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def all(self):
        return self.rows


class ConfiguredGateway:
    def __init__(self, embedding_model="admin-embed-model", structured_model="admin-structured-model"):
        self.embedding_model = embedding_model
        self.structured_model = structured_model

    def configured_model(self, role: str, fallback: str) -> str:
        return self.embedding_model if role == "embedding" else self.structured_model

    def embed_texts(self, texts):
        return [[0.0] * VECTOR_DIMENSION for _ in texts]

    def complete_json(self, *_args):
        return {
            "items": [
                {
                    "evidence_id": "ev_1",
                    "verdict": "supported",
                    "confidence": 1.0,
                    "reason": "direct",
                }
            ]
        }


def test_embedding_index_records_configured_embedding_model():
    added = []

    class Db:
        def scalars(self, _statement):
            return Result()

        def execute(self, statement):
            added.append(statement)

        def commit(self):
            pass

    service = EmbeddingIndexService(
        Db(), Settings(app_env="test", embedding_model="settings-embed"), ConfiguredGateway()
    )
    rep = SimpleNamespace(representation_id="repr_1", text="quote", text_hash="hash")

    service._embed([rep], "paper_1", False)

    assert added[0].compile().params["embedding_model"] == "admin-embed-model"


def test_embedding_index_uses_one_endpoint_snapshot_for_all_batches():
    added, endpoints = [], []

    class Db:
        def scalars(self, _statement):
            return Result()

        def execute(self, statement):
            added.append(statement)

        def commit(self):
            pass

    class Gateway(ConfiguredGateway):
        def resolve_embedding_endpoint(self, _fallback):
            return ModelEndpointConfig(role="embedding", model="text-embedding-3-small")

        def embed_texts_for_endpoint(self, texts, endpoint):
            endpoints.append(endpoint.model)
            self.embedding_model = "changed-after-start"
            return [[0.0] * VECTOR_DIMENSION for _ in texts]

    service = EmbeddingIndexService(Db(), Settings(app_env="test", embedding_batch_size=1), Gateway())
    reps = [
        SimpleNamespace(representation_id="repr_1", text="one", text_hash="hash-1"),
        SimpleNamespace(representation_id="repr_2", text="two", text_hash="hash-2"),
    ]

    service._embed(reps, "paper_1", False)

    assert endpoints == ["text-embedding-3-small", "text-embedding-3-small"]
    assert [item.compile().params["embedding_model"] for item in added] == endpoints


def test_embedding_index_commits_each_completed_batch_and_reports_progress():
    progress = []

    class Db:
        commits = 0

        def scalars(self, _statement):
            return Result()

        def execute(self, _statement):
            pass

        def commit(self):
            self.commits += 1

    db = Db()
    service = EmbeddingIndexService(
        db, Settings(app_env="test", embedding_batch_size=1), ConfiguredGateway()
    )
    reps = [
        SimpleNamespace(representation_id="repr_1", text="one", text_hash="hash-1"),
        SimpleNamespace(representation_id="repr_2", text="two", text_hash="hash-2"),
    ]

    service._embed(reps, "paper_1", False, on_progress=lambda *value: progress.append(value))

    assert db.commits == 3
    assert progress == [(1, 2, 1, 2), (2, 2, 2, 2)]


def test_embedding_index_skips_blank_representations():
    embedded = []

    class Db:
        def scalars(self, _statement):
            return Result()

        def execute(self, _statement):
            pass

        def commit(self):
            pass

    class Gateway(ConfiguredGateway):
        def embed_texts(self, texts):
            embedded.extend(texts)
            return super().embed_texts(texts)

    service = EmbeddingIndexService(Db(), Settings(app_env="test"), Gateway())
    reps = [
        SimpleNamespace(representation_id="repr_blank", text=" \n ", text_hash="blank"),
        SimpleNamespace(representation_id="repr_text", text="quote", text_hash="text"),
    ]

    result = service._embed(reps, "paper_1", False)

    assert embedded == ["quote"]
    assert result.embedded_count == 1
    assert result.skipped_count == 1


def test_dense_retrieval_filters_by_configured_embedding_model():
    class Db:
        statement = None

        def execute(self, statement):
            self.statement = statement
            return Result()

    db = Db()
    service = RetrievalService(db, Settings(app_env="test", embedding_model="settings-embed"), ConfiguredGateway())

    service._dense("query", EvidenceSearchRequest(query="query", scope={}, filters={}), 5)

    assert "admin-embed-model" in db.statement.compile().params.values()


def test_workflow_judgement_cache_uses_configured_structured_model():
    added = []

    class Db:
        def scalar(self, _statement):
            return None

        def get(self, model, _key):
            if model is Paper:
                return SimpleNamespace(
                    paper_id="paper_1", title="Paper", year=2025, doi=None, venue=None
                )
            if model is PaperContent:
                return None
            return None

        def scalars(self, _statement):
            return Result()

        def add(self, item):
            added.append(item)

        def commit(self):
            pass

    evidence = SimpleNamespace(
        evidence_id="ev_1",
        paper_id="paper_1",
        raw_content="raw",
        asset_ids=[],
        page=1,
        bbox=None,
        coordinate_space=None,
        char_start=None,
        char_end=None,
        section_title="Results",
        section_type="body",
    )
    candidate = SimpleNamespace(
        evidence=evidence,
        representation=SimpleNamespace(text="Evidence text"),
        score=0.9,
    )
    workflow = EvidencePackWorkflow(
        Db(), Settings(app_env="test", structured_model="settings-structured"), ConfiguredGateway()
    )

    workflow._judge_batch("claim", [candidate])

    assert added[0].model == "admin-structured-model"
