from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.models.evidence_unit import EvidenceUnit
from app.models.resource_asset import ResourceAsset
from app.models.workflow_run import WorkflowRun
from app.schemas.v1 import EvidenceScope, WorkflowScopeBinding
from app.services.paper_access_service import can_access_paper, normalize_scope


class WorkflowScopeViolation(RuntimeError):
    error_code = "WORKFLOW_SCOPE_VIOLATION"
    retryable = False


def _normalized_scope(scope: EvidenceScope | Mapping[str, Any] | None) -> EvidenceScope:
    parsed = scope if isinstance(scope, EvidenceScope) else EvidenceScope.model_validate(scope or {})
    return EvidenceScope(
        paper_ids=sorted(set(parsed.paper_ids)),
        evidence_ids=sorted(set(parsed.evidence_ids)),
    )


def build_workflow_scope_binding(
    access_scope: str | None,
    scope: EvidenceScope | Mapping[str, Any] | None,
    *,
    result_validated: bool = False,
) -> WorkflowScopeBinding:
    normalized_access_scope = normalize_scope(access_scope)
    normalized_evidence_scope = _normalized_scope(scope)
    canonical = json.dumps(
        {
            "access_scope": normalized_access_scope,
            "evidence_scope": normalized_evidence_scope.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return WorkflowScopeBinding(
        scope_digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        paper_ids=normalized_evidence_scope.paper_ids,
        evidence_ids=normalized_evidence_scope.evidence_ids,
        scope_validated=True,
        result_validated=result_validated,
    )


def validate_workflow_request_scope(
    db: Session,
    access_scope: str | None,
    scope: EvidenceScope | Mapping[str, Any] | None,
) -> WorkflowScopeBinding:
    parsed = _normalized_scope(scope)
    paper_ids = set(parsed.paper_ids)
    for paper_id in parsed.paper_ids:
        if not can_access_paper(db, paper_id, access_scope):
            raise WorkflowScopeViolation("Workflow scope contains an unavailable paper.")
    for evidence_id in parsed.evidence_ids:
        evidence = db.get(EvidenceUnit, evidence_id)
        if (
            evidence is None
            or not can_access_paper(db, evidence.paper_id, access_scope)
            or (paper_ids and evidence.paper_id not in paper_ids)
        ):
            raise WorkflowScopeViolation("Workflow scope contains unavailable evidence.")
    return build_workflow_scope_binding(access_scope, parsed)


def validate_workflow_run_binding(run: WorkflowRun) -> WorkflowScopeBinding:
    request_payload = run.request_payload if isinstance(run.request_payload, Mapping) else {}
    binding = build_workflow_scope_binding(run.access_scope, request_payload.get("scope"))
    if not run.scope_digest or run.scope_digest != binding.scope_digest:
        raise WorkflowScopeViolation("Workflow scope binding is invalid.")
    return binding


def validate_workflow_caller_scope(run: WorkflowRun, access_scope: str | None) -> None:
    if normalize_scope(access_scope) != normalize_scope(run.access_scope):
        raise WorkflowScopeViolation("Workflow run was not found.")
    validate_workflow_run_binding(run)


def _iter_result_evidence(workflow: str, result: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    claims = result.get("claims")
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, Mapping):
            raise WorkflowScopeViolation("Workflow result contains an invalid claim.")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list):
            raise WorkflowScopeViolation("Workflow claim contains invalid evidence.")
        for item in evidence:
            if not isinstance(item, Mapping):
                raise WorkflowScopeViolation("Workflow result contains invalid evidence.")
            yield item
    if workflow != "citation_grounding":
        return
    citations = result.get("citations")
    if not isinstance(citations, list):
        raise WorkflowScopeViolation("Workflow result contains invalid citations.")
    for item in citations:
        if not isinstance(item, Mapping):
            raise WorkflowScopeViolation("Workflow result contains an invalid citation.")
        yield item
    contradictions = result.get("contradictory_evidence")
    if not isinstance(contradictions, list):
        raise WorkflowScopeViolation("Workflow result contains invalid contradictions.")
    for contradiction in contradictions:
        if not isinstance(contradiction, Mapping):
            raise WorkflowScopeViolation("Workflow result contains an invalid contradiction.")
        evidence = contradiction.get("evidence")
        if not isinstance(evidence, list):
            raise WorkflowScopeViolation("Workflow contradiction contains invalid evidence.")
        for item in evidence:
            if not isinstance(item, Mapping):
                raise WorkflowScopeViolation("Workflow result contains invalid contradictory evidence.")
            yield item


def validate_workflow_result_scope(
    db: Session,
    run: WorkflowRun,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    binding = validate_workflow_run_binding(run)
    paper_ids = set(binding.paper_ids)
    evidence_ids = set(binding.evidence_ids)
    validated_assets: set[tuple[str, str]] = set()

    for item in _iter_result_evidence(run.workflow, result):
        evidence_id = str(item.get("evidence_id") or "").strip()
        paper = item.get("paper")
        paper_id = str(paper.get("paper_id") or "").strip() if isinstance(paper, Mapping) else ""
        evidence = db.get(EvidenceUnit, evidence_id) if evidence_id else None
        if (
            evidence is None
            or not paper_id
            or evidence.paper_id != paper_id
            or not can_access_paper(db, paper_id, run.access_scope)
            or (paper_ids and paper_id not in paper_ids)
            or (evidence_ids and evidence_id not in evidence_ids)
        ):
            raise WorkflowScopeViolation("Workflow result contains evidence outside its scope.")
        assets = item.get("assets")
        if not isinstance(assets, list):
            raise WorkflowScopeViolation("Workflow evidence contains invalid assets.")
        for asset_item in assets:
            asset_id = (
                str(asset_item.get("asset_id") or "").strip()
                if isinstance(asset_item, Mapping)
                else ""
            )
            if not asset_id:
                raise WorkflowScopeViolation("Workflow result contains an invalid asset reference.")
            if (evidence_id, asset_id) in validated_assets:
                continue
            asset = db.get(ResourceAsset, asset_id)
            if (
                asset is None
                or asset.paper_id != paper_id
                or (asset.evidence_id is not None and asset.evidence_id != evidence_id)
            ):
                raise WorkflowScopeViolation("Workflow result contains an asset outside its scope.")
            validated_assets.add((evidence_id, asset_id))

    if run.workflow == "evidence_pack" and not isinstance(result.get("claims"), list):
        raise WorkflowScopeViolation("Evidence Pack result is invalid.")
    if run.workflow == "citation_grounding":
        required = ("grounded_answer", "draft_spans", "claims", "citations", "unverified_claims", "contradictory_evidence")
        if any(key not in result for key in required):
            raise WorkflowScopeViolation("Citation Grounding result is invalid.")

    final_binding = binding.model_copy(update={"result_validated": True})
    return {**dict(result), "scope_binding": final_binding.model_dump(mode="json")}


def workflow_scope_projection(run: WorkflowRun) -> WorkflowScopeBinding:
    binding = validate_workflow_run_binding(run)
    if run.status != "succeeded":
        return binding
    result = run.result_payload if isinstance(run.result_payload, Mapping) else {}
    stored = WorkflowScopeBinding.model_validate(result.get("scope_binding"))
    expected = binding.model_copy(update={"result_validated": True})
    if stored != expected:
        raise WorkflowScopeViolation("Workflow result scope binding is invalid.")
    return stored


__all__ = [
    "WorkflowScopeViolation",
    "build_workflow_scope_binding",
    "validate_workflow_caller_scope",
    "validate_workflow_request_scope",
    "validate_workflow_result_scope",
    "validate_workflow_run_binding",
    "workflow_scope_projection",
]
