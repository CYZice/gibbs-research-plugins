import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.services.model_gateway import ModelGateway, ModelGatewayError

VERDICTS = {"verified", "partially_supported", "contradicted", "unverified"}
SUPPORT_TYPES = {"direct", "inferred", "background", "contradictory", "unsupported"}


class VerificationResult(BaseModel):
    verdict: str
    support_type: str
    support_score: float = Field(ge=0.0, le=1.0)
    reason: str
    verifier: str = "llm"


class LLMClaimVerifier:
    def __init__(self, model_gateway: ModelGateway) -> None:
        self.model_gateway = model_gateway

    def verify(self, claim_text: str, hit: Any, content: Any) -> dict:
        quote = (content.quote or "").strip()
        context_text = content.context_text or ""
        if not quote or quote not in context_text:
            return self._unverified("Quote is not present in evidence context.")

        system = (
            "你是科学文献证据核验器。你的任务是判断 Evidence 是否支持 Claim。\n"
            "只能依据 Evidence quote 和 Evidence context。不能使用外部知识。\n"
            "不能补充原文没有的信息。LLM 只判断，不生成 quote。\n"
            "如果 Evidence 只是相关但不能证明 Claim，判 unverified 或 partially_supported。\n"
            "如果 Claim 含有“最、首次、显著、导致、证明、通常、所有、唯一、最高、最低”等强断言，"
            "必须要求 Evidence 明确支持这些限定。\n"
            "如果 Evidence 明确反驳 Claim，判 contradicted。\n"
            "请只输出 JSON：verdict, support_type, support_score, reason。"
        )
        user = json.dumps(
            {
                "claim": claim_text,
                "paper_title": getattr(hit, "paper_title", None),
                "section_type": getattr(hit, "section", None),
                "modality": getattr(hit, "modality", None),
                "retrieval_score": getattr(hit, "score", None),
                "evidence_quote": quote,
                "evidence_context": context_text,
            },
            ensure_ascii=False,
        )
        try:
            data = self.model_gateway.complete_json(system, user)
            result = self._normalize(data)
        except (ModelGatewayError, NotImplementedError, ValidationError, ValueError, TypeError):
            return self._unverified("Verifier failed closed.")
        return result.model_dump()

    def _normalize(self, data: dict[str, Any]) -> VerificationResult:
        verdict = str(data.get("verdict", "")).strip()
        support_type = str(data.get("support_type", "")).strip()
        if verdict not in VERDICTS:
            raise ValueError("invalid verifier verdict")
        if support_type not in SUPPORT_TYPES:
            raise ValueError("invalid verifier support_type")
        score = data.get("support_score", 0.0)
        if not isinstance(score, int | float):
            raise ValueError("invalid verifier support_score")
        score = max(0.0, min(1.0, float(score)))
        reason = str(data.get("reason") or "Verifier returned no reason.").strip()[:120]
        return VerificationResult(
            verdict=verdict,
            support_type=support_type,
            support_score=score,
            reason=reason,
        )

    def _unverified(self, reason: str) -> dict:
        return {
            "verdict": "unverified",
            "support_type": "unsupported",
            "support_score": 0.0,
            "reason": reason,
            "verifier": "llm",
        }
