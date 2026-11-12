import re

import pytest

from app.core.config import Settings
from app.services.workflow_service import CitationGroundingWorkflow, EvidencePackWorkflow


class SpanGateway:
    def __init__(self, claims):
        self.claims = claims

    def complete_json(self, _system: str, _user: str) -> dict:
        return {"claims": self.claims}


def test_claim_extraction_uses_backend_spans_and_sorts_selected_claims() -> None:
    workflow = CitationGroundingWorkflow(
        None, Settings(app_env="test"),
        SpanGateway([{"span_id": "span_2", "claim": "Second claim"}, {"span_id": "span_1", "claim": "First claim"}]),
    )

    spans, claims = workflow._extract_claims("First claim. Second claim.")

    assert [(span.span_id, span.text, span.start, span.end) for span in spans] == [
        ("span_1", "First claim.", 0, 12),
        ("span_2", "Second claim.", 13, 26),
    ]
    assert [(claim.claim_id, claim.span_id, claim.text) for claim in claims] == [
        ("claim_1", "span_1", "First claim."),
        ("claim_2", "span_2", "Second claim."),
    ]


def test_claim_extraction_rejects_unknown_or_duplicate_span_ids() -> None:
    for claims, error in [
        ([{"span_id": "span_3", "claim": "Missing"}], "unknown"),
        ([{"span_id": "span_1", "claim": "One"}, {"span_id": "span_1", "claim": "Again"}], "duplicate"),
    ]:
        workflow = CitationGroundingWorkflow(None, Settings(app_env="test"), SpanGateway(claims))
        with pytest.raises(ValueError, match=error):
            workflow._extract_claims("First claim. Second claim.")


def test_claim_extraction_conservatively_checks_omitted_spans() -> None:
    workflow = CitationGroundingWorkflow(
        None,
        Settings(app_env="test"),
        SpanGateway([{"span_id": "span_1", "claim": "First claim"}]),
    )

    spans, claims = workflow._extract_claims("First claim. Unchecked claim.")

    assert [span.span_id for span in spans] == ["span_1", "span_2"]
    assert [(claim.claim_id, claim.span_id, claim.text) for claim in claims] == [
        ("claim_1", "span_1", "First claim."),
        ("claim_2", "span_2", "Unchecked claim."),
    ]


def test_sentence_spans_keep_unicode_offsets() -> None:
    spans = CitationGroundingWorkflow._split_draft("前言。 电解液稳定。")

    assert [(span.text, span.start, span.end) for span in spans] == [
        ("前言。", 0, 3),
        ("电解液稳定。", 4, 10),
    ]


def test_grounding_reuses_pack_inserts_only_best_marker_and_classifies_unverified(monkeypatch) -> None:
    def build(_self, _claims, _scope, _filters, _top_k):
        return {
            "claims": [
                {
                    "claim_id": "claim_1",
                    "claim": "First claim.",
                    "status": "supported",
                    "evidence": [
                        {"evidence_id": "partial", "verdict": "partially_supported", "confidence": 0.99, "retrieval_score": 0.99},
                        {"evidence_id": "best", "verdict": "supported", "confidence": 0.5, "retrieval_score": 0.5},
                    ],
                },
                {
                    "claim_id": "claim_2",
                    "claim": "Second claim.",
                    "status": "contradicted",
                    "evidence": [{"evidence_id": "contra", "verdict": "contradicted"}],
                },
            ]
        }

    monkeypatch.setattr(EvidencePackWorkflow, "build", build)
    workflow = CitationGroundingWorkflow(
        None, Settings(app_env="test"),
        SpanGateway([{"span_id": "span_1", "claim": "First"}, {"span_id": "span_2", "claim": "Second"}]),
    )

    draft = "First claim. Second claim."
    result = workflow.ground(draft, None, None, 5)

    assert result["grounded_answer"] == "First claim. [1] Second claim. [unverified]"
    assert re.sub(r" \[(?:\d+|unverified)\]", "", result["grounded_answer"]) == draft
    assert [item["evidence_id"] for item in result["citations"]] == ["best"]
    assert result["unverified_claims"] == [{"claim_id": "claim_2", "claim": "Second claim.", "reason": "contradicted"}]
    assert result["contradictory_evidence"][0]["evidence"][0]["evidence_id"] == "contra"


def test_grounding_passes_access_scope_to_internal_evidence_pack(monkeypatch) -> None:
    captured = []

    def build(self, _claims, _scope, _filters, _top_k):
        captured.append(self.access_scope)
        return {
            "claims": [
                {
                    "claim_id": "claim_1",
                    "claim": "Scoped claim.",
                    "status": "unverified",
                    "evidence": [],
                }
            ]
        }

    monkeypatch.setattr(EvidencePackWorkflow, "build", build)
    workflow = CitationGroundingWorkflow(
        None,
        Settings(app_env="test"),
        SpanGateway([{"span_id": "span_1", "claim": "Scoped claim"}]),
        access_scope="scope-a",
    )

    workflow.ground("Scoped claim.", None, None, 5)

    assert captured == ["scope-a"]
