from types import SimpleNamespace

from sqlalchemy import select

from app.models.evidence_unit import EvidenceUnit
from app.schemas.v1 import EvidenceSearchRequest
from app.services.model_gateway import RerankResult
from app.services.retrieval_service import RetrievalCandidate, RetrievalService
from app.services.workflow_service import EvidencePackWorkflow


def test_default_evidence_search_excludes_reference_sections():
    request = EvidenceSearchRequest(query="photocatalytic", scope={}, filters={})
    statement = RetrievalService.__new__(RetrievalService)._constrained(
        select(EvidenceUnit), request
    )

    compiled = statement.compile()
    assert "section_type" in str(compiled)
    assert "block_type" in str(compiled)
    assert set(compiled.params["section_type_1"]) == {"references", "reference"}
    assert compiled.params["block_type_1"] == "reference_list"


def test_explicit_reference_filter_is_not_overridden():
    request = EvidenceSearchRequest(
        query="prior work", scope={}, filters={"section_type": ["references"]}
    )
    statement = RetrievalService.__new__(RetrievalService)._constrained(
        select(EvidenceUnit), request
    )

    compiled = statement.compile()
    assert compiled.params == {"section_type_1": ["references"]}


def test_pack_result_uses_normalized_representation_instead_of_raw_mineru_json():
    class Result:
        def all(self):
            return []

    class Database:
        def get(self, _model, _key):
            return SimpleNamespace(
                paper_id="paper_1", title="Paper", year=2025, doi=None, venue=None
            )

        def scalars(self, _statement):
            return Result()

    evidence = SimpleNamespace(
        evidence_id="ev_1",
        paper_id="paper_1",
        raw_content='{"image_source": {}}',
        asset_ids=[],
        page=1,
        bbox=[0.1, 0.1, 0.2, 0.2],
        coordinate_space="normalized",
        char_start=10,
        char_end=20,
        section_title="Results",
        section_type="body",
    )
    candidate = SimpleNamespace(
        evidence=evidence,
        representation=SimpleNamespace(text="Fig. 1. Electrochemical properties."),
        score=0.8,
    )

    result = EvidencePackWorkflow.__new__(EvidencePackWorkflow)
    result.db = Database()
    payload = result._result(
        candidate, {"verdict": "supported", "confidence": 1.0, "reason": "Direct support."}
    )

    assert payload["quote"] == "Fig. 1. Electrochemical properties."
    assert "image_source" not in payload["quote"]


def test_evidence_rerank_uses_representation_text_and_preserves_missing_candidates():
    class Gateway:
        def rerank(self, _query, documents, _top_k):
            assert documents == ["first", "second"]
            return [RerankResult(index=1, score=0.9)]

    service = RetrievalService.__new__(RetrievalService)
    service.model_gateway = Gateway()
    service.metrics = {}
    first = RetrievalCandidate(
        SimpleNamespace(evidence_id="ev_1"), SimpleNamespace(text="first"), 0.2
    )
    second = RetrievalCandidate(
        SimpleNamespace(evidence_id="ev_2"), SimpleNamespace(text="second"), 0.1
    )

    result = service._rerank("query", [first, second])

    assert [item.evidence.evidence_id for item in result] == ["ev_2", "ev_1"]
    assert service.metrics["rerank_applied"] is True
