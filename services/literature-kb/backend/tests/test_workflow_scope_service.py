from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.evidence_unit import EvidenceUnit
from app.models.resource_asset import ResourceAsset
from app.models.workflow_run import WorkflowRun
from app.services import workflow_scope_service
from app.services.workflow_scope_service import (
    WorkflowScopeViolation,
    build_workflow_scope_binding,
    validate_workflow_caller_scope,
    validate_workflow_request_scope,
    validate_workflow_result_scope,
    workflow_scope_projection,
)


class Db:
    def __init__(self, *, evidence=None, assets=None):
        self.evidence = evidence or {}
        self.assets = assets or {}

    def get(self, model, item_id):
        if model is EvidenceUnit:
            return self.evidence.get(item_id)
        if model is ResourceAsset:
            return self.assets.get(item_id)
        raise AssertionError(model)


def _run(*, workflow="evidence_pack", scope=None, access_scope="scope-a"):
    payload = {"scope": scope or {"paper_ids": ["paper-1"], "evidence_ids": []}}
    binding = build_workflow_scope_binding(access_scope, payload["scope"])
    return WorkflowRun(
        run_id="run_1",
        workflow=workflow,
        status="running",
        progress=0.5,
        access_scope=access_scope,
        scope_digest=binding.scope_digest,
        request_payload=payload,
        result_payload=None,
        details={},
    )


def _evidence_item(*, evidence_id="ev-1", paper_id="paper-1", asset_id=None):
    return {
        "evidence_id": evidence_id,
        "quote": "bounded quote",
        "paper": {"paper_id": paper_id, "title": "Paper"},
        "source_location": {"page": 1},
        "assets": ([{"asset_id": asset_id}] if asset_id else []),
        "verdict": "supported",
        "confidence": 0.9,
        "reason": "matches",
    }


def test_scope_digest_is_stable_and_does_not_expose_access_scope():
    first = build_workflow_scope_binding(
        "private-scope",
        {"paper_ids": ["paper-2", "paper-1", "paper-1"], "evidence_ids": ["ev-1"]},
    )
    second = build_workflow_scope_binding(
        "private-scope",
        {"paper_ids": ["paper-1", "paper-2"], "evidence_ids": ["ev-1"]},
    )

    assert first == second
    assert first.paper_ids == ["paper-1", "paper-2"]
    assert "private-scope" not in str(first.model_dump(mode="json"))


def test_request_and_caller_scope_fail_closed(monkeypatch):
    monkeypatch.setattr(
        workflow_scope_service,
        "can_access_paper",
        lambda _db, paper_id, scope: paper_id == "paper-1" and scope == "scope-a",
    )
    db = Db(evidence={"ev-1": SimpleNamespace(paper_id="paper-1")})

    binding = validate_workflow_request_scope(
        db,
        "scope-a",
        {"paper_ids": ["paper-1"], "evidence_ids": ["ev-1"]},
    )
    run = _run(scope={"paper_ids": ["paper-1"], "evidence_ids": ["ev-1"]})
    run.scope_digest = binding.scope_digest
    validate_workflow_caller_scope(run, "scope-a")

    with pytest.raises(WorkflowScopeViolation):
        validate_workflow_caller_scope(run, "scope-b")
    with pytest.raises(WorkflowScopeViolation):
        validate_workflow_request_scope(
            db,
            "scope-a",
            {"paper_ids": ["paper-2"], "evidence_ids": []},
        )


def test_terminal_result_validates_evidence_assets_and_persists_binding(monkeypatch):
    monkeypatch.setattr(workflow_scope_service, "can_access_paper", lambda *_args: True)
    run = _run()
    db = Db(
        evidence={"ev-1": SimpleNamespace(paper_id="paper-1")},
        assets={"asset-1": SimpleNamespace(paper_id="paper-1", evidence_id="ev-1")},
    )
    result = {
        "claims": [
            {
                "claim_id": "claim-1",
                "claim": "Claim",
                "status": "supported",
                "evidence": [_evidence_item(asset_id="asset-1")],
            }
        ]
    }

    validated = validate_workflow_result_scope(db, run, result)
    assert validated["scope_binding"]["scope_digest"] == run.scope_digest
    assert validated["scope_binding"]["result_validated"] is True

    run.status = "succeeded"
    run.result_payload = validated
    assert workflow_scope_projection(run).result_validated is True


def test_terminal_result_rejects_cross_scope_evidence_or_asset(monkeypatch):
    monkeypatch.setattr(workflow_scope_service, "can_access_paper", lambda *_args: True)
    run = _run()
    db = Db(
        evidence={
            "ev-2": SimpleNamespace(paper_id="paper-2"),
            "ev-1": SimpleNamespace(paper_id="paper-1"),
        },
        assets={"asset-2": SimpleNamespace(paper_id="paper-2", evidence_id="ev-1")},
    )

    for evidence in (
        _evidence_item(evidence_id="ev-2", paper_id="paper-2"),
        _evidence_item(asset_id="asset-2"),
    ):
        with pytest.raises(WorkflowScopeViolation):
            validate_workflow_result_scope(
                db,
                run,
                {"claims": [{"claim_id": "c", "claim": "C", "status": "supported", "evidence": [evidence]}]},
            )

    with pytest.raises(WorkflowScopeViolation):
        validate_workflow_result_scope(
            db,
            run,
            {"claims": [{"claim_id": "c", "claim": "C", "status": "supported", "evidence": None}]},
        )


def test_citation_result_validates_nested_claims_citations_and_contradictions(monkeypatch):
    monkeypatch.setattr(workflow_scope_service, "can_access_paper", lambda *_args: True)
    run = _run(workflow="citation_grounding")
    db = Db(evidence={"ev-1": SimpleNamespace(paper_id="paper-1")})
    evidence = _evidence_item()
    result = {
        "grounded_answer": "Claim. [1]",
        "draft_spans": [{"span_id": "span-1", "text": "Claim.", "start": 0, "end": 6, "selected": True}],
        "claims": [{"claim_id": "claim-1", "claim": "Claim.", "status": "supported", "evidence": [evidence]}],
        "citations": [{"citation_key": "[1]", **evidence}],
        "unverified_claims": [],
        "contradictory_evidence": [{"claim_id": "claim-1", "claim": "Claim.", "evidence": [evidence]}],
    }

    validated = validate_workflow_result_scope(db, run, result)
    assert validated["scope_binding"]["result_validated"] is True
