from __future__ import annotations

import json
import re
from hashlib import sha256
from dataclasses import dataclass
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.ids import new_id
from app.models.evidence_unit import EvidenceUnit
from app.models.paper import Paper
from app.models.paper_content import PaperContent
from app.models.resource_asset import ResourceAsset
from app.models.operational_control import EvidenceJudgement
from app.schemas.v1 import EvidenceFilters, EvidenceScope, EvidenceSearchRequest, WorkflowClaim
from app.services.model_gateway import ModelGateway, get_model_gateway, runtime_model_name
from app.services.retrieval_service import RetrievalService


@dataclass(slots=True)
class ExtractedClaim:
    claim_id: str
    text: str
    start: int
    end: int
    span_id: str = ""


class EvidencePackWorkflow:
    prompt_version = "evidence-judge-v2-batch"
    def __init__(
        self,
        db: Session,
        settings: Settings,
        gateway: ModelGateway | None = None,
        *,
        access_scope: str | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.gateway = gateway or get_model_gateway(settings)
        self.access_scope = access_scope

    def build(
        self,
        claims: list[WorkflowClaim | ExtractedClaim],
        scope: EvidenceScope,
        filters: EvidenceFilters,
        top_k_per_claim: int,
        on_stage: Callable[[str, float], None] | None = None,
    ) -> dict:
        if on_stage:
            on_stage("retrieval_and_judging", 0.35)
        results = []
        for index, claim in enumerate(claims, start=1):
            claim_id = getattr(claim, "claim_id", None) or f"claim_{index}"
            text = claim.text.strip()
            candidates = RetrievalService(
                self.db,
                self.settings,
                self.gateway,
                access_scope=self.access_scope,
            ).search(
                EvidenceSearchRequest(
                    query=text,
                    scope=scope,
                    filters=filters,
                    top_k=top_k_per_claim,
                    page_size=top_k_per_claim,
                )
            )
            evidence = self._judge_batch(text, candidates[:5])
            states = [item["verdict"] for item in evidence]
            status = (
                "supported" if "supported" in states else
                "partially_supported" if "partially_supported" in states else
                "contradicted" if "contradicted" in states else "unverified"
            )
            results.append(
                {"claim_id": claim_id, "claim": text, "status": status, "evidence": evidence}
            )
        return {"claims": results}

    def _judge_batch(self, claim: str, candidates) -> list[dict]:
        prepared, verdicts, missing = [], {}, []
        model = runtime_model_name(self.gateway, "structured", self.settings.structured_model)
        for candidate in candidates:
            evidence = candidate.evidence
            content_hash = sha256(candidate.representation.text.encode()).hexdigest()
            claim_hash = sha256(claim.encode()).hexdigest()
            cache_key = sha256(f"{claim_hash}:{evidence.evidence_id}:{content_hash}:{model}:{self.prompt_version}".encode()).hexdigest()
            cached = self.db.scalar(select(EvidenceJudgement).where(EvidenceJudgement.cache_key == cache_key))
            if cached:
                verdicts[evidence.evidence_id] = cached.result
                prepared.append((candidate, cache_key, claim_hash, content_hash))
                continue
            missing.append(evidence.evidence_id)
            prepared.append((candidate, cache_key, claim_hash, content_hash))
        if missing:
            payload = []
            for candidate, *_ in prepared:
                if candidate.evidence.evidence_id in missing:
                    payload.append({
                        "evidence_id": candidate.evidence.evidence_id,
                        "quote": candidate.representation.text,
                        "context": self._context(candidate.evidence),
                    })
            response = self.gateway.complete_json(
                "You are a scientific evidence judge. Return JSON only as {items: [{evidence_id, verdict, confidence, reason}]}. "
                "Use only supported, partially_supported, contradicted, or unverified. Copy every evidence_id exactly and judge only against the supplied source text.",
                json.dumps({"claim": claim, "evidence": payload}, ensure_ascii=False),
            )
            rows = response.get("items") if isinstance(response, dict) else None
            if isinstance(rows, list):
                verdicts.update({str(row.get("evidence_id")): row for row in rows if isinstance(row, dict) and row.get("evidence_id")})
        results = []
        for candidate, cache_key, claim_hash, content_hash in prepared:
            verdict = verdicts.get(candidate.evidence.evidence_id)
            if not isinstance(verdict, dict):
                results.append(self._judge(claim, candidate))
                continue
            normalized = self._normalize_verdict(verdict)
            if self.db.scalar(select(EvidenceJudgement.judgement_id).where(EvidenceJudgement.cache_key == cache_key)) is None:
                self.db.add(EvidenceJudgement(
                    judgement_id=new_id("judgement"), cache_key=cache_key, claim_hash=claim_hash,
                    evidence_id=candidate.evidence.evidence_id, model=model,
                    prompt_version=self.prompt_version, content_sha256=content_hash, result=normalized,
                ))
            results.append(self._result(candidate, normalized))
        self.db.commit()
        return results

    def _judge(self, claim: str, candidate) -> dict:
        evidence = candidate.evidence
        context = self._context(evidence)
        verdict = self.gateway.complete_json(
            "You are a scientific evidence judge. Return JSON only with verdict "
            "(supported, partially_supported, contradicted, unverified), confidence (0..1), "
            "and reason. Judge only the provided claim against the canonical source text.",
            json.dumps({"claim": claim, "quote": candidate.representation.text, "context": context}, ensure_ascii=False),
        )
        return self._result(candidate, self._normalize_verdict(verdict))

    def _context(self, evidence: EvidenceUnit) -> str:
        content = self.db.get(PaperContent, evidence.paper_id)
        if content is None or evidence.char_start is None or evidence.char_end is None:
            return evidence.raw_content
        start = max(0, evidence.char_start - 1200)
        end = min(content.char_length, evidence.char_end + 2400)
        return content.text[start:end]

    def _normalize_verdict(self, verdict: dict) -> dict:
        state = str(verdict.get("verdict", "unverified"))
        if state == "verified":
            state = "supported"
        if state not in {"supported", "partially_supported", "contradicted", "unverified"}:
            state = "unverified"
        confidence = verdict.get("confidence", verdict.get("support_score", 0.0))
        confidence = float(confidence) if isinstance(confidence, int | float) else 0.0
        return {"verdict": state, "confidence": max(0.0, min(1.0, confidence)), "reason": str(verdict.get("reason", ""))}

    def _result(self, candidate, verdict: dict) -> dict:
        evidence = candidate.evidence
        paper = self.db.get(Paper, evidence.paper_id)
        assets = self.db.scalars(
            select(ResourceAsset).where(ResourceAsset.asset_id.in_(evidence.asset_ids or []))
        ).all()
        return {
            "evidence_id": evidence.evidence_id,
            "quote": candidate.representation.text,
            "paper": {
                "paper_id": paper.paper_id,
                "title": paper.title,
                "year": paper.year,
                "doi": paper.doi,
                "venue": paper.venue,
            },
            "source_location": {
                "page": evidence.page,
                "bbox": evidence.bbox,
                "coordinate_space": evidence.coordinate_space if evidence.bbox else None,
                "char_start": evidence.char_start,
                "char_end": evidence.char_end,
                "section_title": evidence.section_title,
                "section_type": evidence.section_type,
            },
            "assets": [
                {
                    "asset_id": asset.asset_id,
                    "asset_type": asset.asset_type,
                    "mime_type": asset.mime_type,
                    "caption": asset.caption,
                    "location": asset.location,
                }
                for asset in assets
            ],
            "retrieval_score": candidate.score,
            **verdict,
        }


class CitationGroundingWorkflow:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        gateway: ModelGateway | None = None,
        *,
        access_scope: str | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.gateway = gateway or get_model_gateway(settings)
        self.access_scope = access_scope

    def ground(
        self, draft: str, scope: EvidenceScope, filters: EvidenceFilters, top_k_per_claim: int,
        on_stage: Callable[[str, float], None] | None = None,
    ) -> dict:
        if on_stage:
            on_stage("claim_extraction", 0.2)
        spans, claims = self._extract_claims(draft)
        if on_stage:
            on_stage("retrieval_and_judging", 0.45)
        pack = EvidencePackWorkflow(
            self.db,
            self.settings,
            self.gateway,
            access_scope=self.access_scope,
        ).build(
            claims, scope, filters, top_k_per_claim
        )
        if on_stage:
            on_stage("citation_assembly", 0.85)
        citations, inserts, unverified, contradictions = [], [], [], []
        keys: dict[str, str] = {}
        for claim, result in zip(claims, pack["claims"], strict=True):
            positive = [
                item for item in result["evidence"]
                if item["verdict"] in {"supported", "partially_supported"}
            ]
            if not positive:
                contradictory = [
                    item for item in result["evidence"] if item["verdict"] == "contradicted"
                ]
                reason = (
                    "contradicted" if contradictory else
                    "no_evidence" if not result["evidence"] else "unsupported"
                )
                inserts.append((claim.end, " [unverified]"))
                unverified.append(
                    {"claim_id": claim.claim_id, "claim": claim.text, "reason": reason}
                )
                if contradictory:
                    contradictions.append(
                        {"claim_id": claim.claim_id, "claim": claim.text, "evidence": contradictory}
                    )
                continue
            best = sorted(
                positive,
                key=lambda item: (
                    0 if item["verdict"] == "supported" else 1,
                    -float(item.get("confidence", 0.0)),
                    -float(item.get("retrieval_score", 0.0)),
                ),
            )[0]
            key = keys.setdefault(best["evidence_id"], f"[{len(keys) + 1}]")
            if not any(existing["citation_key"] == key for existing in citations):
                citations.append({"citation_key": key, **best})
            inserts.append((claim.end, f" {key}"))
        grounded = draft
        for offset, marker in sorted(inserts, reverse=True):
            grounded = grounded[:offset] + marker + grounded[offset:]
        selected_ids = {claim.span_id for claim in claims}
        return {
            "grounded_answer": grounded,
            "draft_spans": [
                {"span_id": span.span_id, "text": span.text, "start": span.start, "end": span.end,
                 "selected": span.span_id in selected_ids}
                for span in spans
            ],
            "claims": pack["claims"],
            "citations": citations,
            "unverified_claims": unverified,
            "contradictory_evidence": contradictions,
        }

    def _extract_claims(self, draft: str) -> tuple[list[ExtractedClaim], list[ExtractedClaim]]:
        spans = self._split_draft(draft)
        response = self.gateway.complete_json(
            "Select independently verifiable scientific statements from the supplied draft spans. "
            "Return JSON only as {claims: [{span_id, claim}]}. span_id must be copied exactly. "
            "Do not return character offsets. Return every factual span; omitted spans are "
            "conservatively checked so unsupported text cannot bypass grounding.",
            json.dumps(
                {"draft_spans": [
                    {"span_id": span.span_id, "text": span.text} for span in spans
                ]},
                ensure_ascii=False,
            ),
        )
        raw_claims = response.get("claims", response.get("items", []))
        if not isinstance(raw_claims, list):
            raise ValueError("Claim extractor returned invalid claims.")
        by_id = {span.span_id: span for span in spans}
        selected = []
        seen = set()
        for item in raw_claims:
            if not isinstance(item, dict):
                raise ValueError("Claim extractor returned an invalid claim.")
            span_id, extracted = item.get("span_id"), item.get("claim")
            if not isinstance(span_id, str) or not isinstance(extracted, str) or not extracted.strip():
                raise ValueError("Claim extractor omitted span_id or claim.")
            if span_id not in by_id:
                raise ValueError("Claim extractor returned an unknown draft span.")
            if span_id in seen:
                raise ValueError("Claim extractor returned a duplicate draft span.")
            seen.add(span_id)
            span = by_id[span_id]
            selected.append(ExtractedClaim("", span.text, span.start, span.end, span_id))
        for span in spans:
            if span.span_id not in seen:
                selected.append(
                    ExtractedClaim("", span.text, span.start, span.end, span.span_id)
                )
        selected.sort(key=lambda claim: claim.start)
        return spans, [
            ExtractedClaim(f"claim_{index}", claim.text, claim.start, claim.end, claim.span_id)
            for index, claim in enumerate(selected, start=1)
        ]

    @staticmethod
    def _split_draft(draft: str) -> list[ExtractedClaim]:
        # ponytail: deterministic sentence spans avoid model-generated offsets; use a full NLP splitter only if this corpus needs abbreviation-aware citation placement.
        spans, cursor = [], 0
        for match in re.finditer(r"[.!?。！？]+(?=\s|$)", draft):
            start, end = CitationGroundingWorkflow._trim_span(draft, cursor, match.end())
            if start < end:
                spans.append(ExtractedClaim(f"span_{len(spans) + 1}", draft[start:end], start, end, ""))
            cursor = match.end()
        start, end = CitationGroundingWorkflow._trim_span(draft, cursor, len(draft))
        if start < end:
            spans.append(ExtractedClaim(f"span_{len(spans) + 1}", draft[start:end], start, end, ""))
        if not spans:
            raise ValueError("Draft contains no citable text spans.")
        return [
            ExtractedClaim(span.claim_id, span.text, span.start, span.end, span.claim_id)
            for span in spans
        ]

    @staticmethod
    def _trim_span(draft: str, start: int, end: int) -> tuple[int, int]:
        while start < end and draft[start].isspace():
            start += 1
        while end > start and draft[end - 1].isspace():
            end -= 1
        return start, end

