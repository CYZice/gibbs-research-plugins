from __future__ import annotations

import time
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.evidence_embedding import EvidenceEmbedding
from app.models.evidence_representation import EvidenceRepresentation
from app.models.evidence_unit import EvidenceUnit
from app.models.paper import Paper
from app.models.paper_task import PaperTask
from app.schemas.v1 import EvidenceSearchRequest
from app.services.embedding_index_service import VECTOR_DIMENSION
from app.services.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    get_model_gateway,
    runtime_model_name,
)
from app.services.paper_search_service import search_terms
from app.services.paper_access_service import visible_paper_clause


class RetrievalError(Exception):
    code = "INVALID_ARGUMENT"


class RetrievalBackendError(RetrievalError):
    code = "BACKEND_UNAVAILABLE"


@dataclass(slots=True)
class RetrievalCandidate:
    evidence: EvidenceUnit
    representation: EvidenceRepresentation
    score: float


class RetrievalService:
    """Evidence retrieval whose SQL candidate set is shared by every retrieval stage."""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        model_gateway: ModelGateway | None = None,
        access_scope: str | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.model_gateway = model_gateway or get_model_gateway(settings)
        self.access_scope = access_scope
        self.metrics: dict[str, int | str | bool] = {}

    def search(self, request: EvidenceSearchRequest) -> list[RetrievalCandidate]:
        query = request.query.strip()
        if not query:
            raise RetrievalError("query must not be empty")
        model = runtime_model_name(self.model_gateway, "embedding", self.settings.embedding_model)
        rebuild = self.db.scalar(
            select(PaperTask).where(PaperTask.operation == "reembed_all")
            .order_by(PaperTask.created_at.desc()).limit(1)
        )
        if rebuild and (rebuild.details or {}).get("target_model") == model and rebuild.status != "succeeded":
            raise RetrievalBackendError("Embedding rebuild is in progress.")
        self._validate_scope(request)
        candidate_limit = min(1000, max(request.top_k * 4, request.top_k))
        try:
            dense = self._dense(query, request, candidate_limit)
        except ModelGatewayError as exc:
            # Local keyword retrieval remains authoritative when the optional
            # embedding provider is unavailable (for example, a local 403).
            # This keeps already-indexed Evidence searchable without inventing
            # results or hiding the source bbox metadata.
            dense = []
            self.metrics.update({
                "dense_applied": False,
                "dense_degraded_reason": type(exc).__name__,
            })
        keyword = self._keyword(query, request, candidate_limit)
        fused = self._rrf(dense, keyword)
        return self._rerank(query, fused)[: request.top_k]

    def _rerank(self, query: str, candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
        started = time.perf_counter()
        head = candidates[:50]
        try:
            ranked = (
                self.model_gateway.rerank(
                    query, [item.representation.text[:8000] for item in head], len(head)
                )
                if head
                else []
            )
        except (ModelGatewayError, ValueError) as exc:
            self.metrics.update(
                {
                    "rerank_candidates": len(head),
                    "rerank_applied": False,
                    "rerank_degraded_reason": type(exc).__name__,
                    "rerank_latency_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            return candidates
        seen = set()
        reordered = []
        for result in ranked:
            if 0 <= result.index < len(head) and result.index not in seen:
                item = head[result.index]
                reordered.append(
                    RetrievalCandidate(item.evidence, item.representation, result.score)
                )
                seen.add(result.index)
        applied = bool(reordered)
        self.metrics.update(
            {
                "rerank_candidates": len(head),
                "rerank_applied": applied,
                "rerank_latency_ms": int((time.perf_counter() - started) * 1000),
                **({} if applied else {"rerank_degraded_reason": "disabled_or_empty"}),
            }
        )
        if not applied:
            return candidates
        reordered.extend(item for index, item in enumerate(head) if index not in seen)
        return reordered + candidates[50:]

    def _base_statement(self) -> Select:
        statement = (
            select(EvidenceUnit, EvidenceRepresentation)
            .join(Paper, Paper.paper_id == EvidenceUnit.paper_id)
            .join(
                EvidenceRepresentation,
                EvidenceRepresentation.evidence_id == EvidenceUnit.evidence_id,
            )
            .where(EvidenceUnit.exclude_from_retrieval.is_(False))
            .where(EvidenceRepresentation.status == "success")
            .where(EvidenceRepresentation.is_active.is_(True))
            .where(EvidenceRepresentation.indexable.is_(True))
            .where(Paper.evidence_status == "ready")
            .where(Paper.embedding_status == "ready")
        )
        if self.access_scope is not None:
            statement = statement.where(
                visible_paper_clause(Paper.paper_id, self.access_scope)
            )
        return statement

    def _constrained(self, statement: Select, request: EvidenceSearchRequest) -> Select:
        scope = request.scope
        filters = request.filters
        if scope.paper_ids:
            statement = statement.where(Paper.paper_id.in_(scope.paper_ids))
        if scope.evidence_ids:
            statement = statement.where(EvidenceUnit.evidence_id.in_(scope.evidence_ids))
        if filters.year:
            if filters.year.gte is not None:
                statement = statement.where(Paper.year >= filters.year.gte)
            if filters.year.lte is not None:
                statement = statement.where(Paper.year <= filters.year.lte)
        if filters.venue:
            statement = statement.where(Paper.venue.in_(filters.venue))
        if filters.doi:
            statement = statement.where(Paper.doi == filters.doi)
        if filters.language:
            statement = statement.where(Paper.language.in_(filters.language))
        if filters.publication_type:
            statement = statement.where(Paper.publication_type.in_(filters.publication_type))
        if filters.modality:
            statement = statement.where(EvidenceUnit.modality.in_(filters.modality))
        if filters.block_type:
            statement = statement.where(EvidenceUnit.block_type.in_(filters.block_type))
        if filters.section_type:
            statement = statement.where(EvidenceUnit.section_type.in_(filters.section_type))
        else:
            statement = statement.where(
                (EvidenceUnit.section_type.is_(None))
                | (~EvidenceUnit.section_type.in_(("references", "reference")))
            ).where(EvidenceUnit.block_type != "reference_list")
        if filters.has_assets is True:
            statement = statement.where(EvidenceUnit.asset_ids.is_not(None)).where(
                EvidenceUnit.asset_ids != []
            )
        if filters.has_assets is False:
            statement = statement.where(
                (EvidenceUnit.asset_ids.is_(None)) | (EvidenceUnit.asset_ids == [])
            )
        return statement

    def _validate_scope(self, request: EvidenceSearchRequest) -> None:
        evidence_ids = list(dict.fromkeys(request.scope.evidence_ids))
        if not evidence_ids:
            return
        rows = self.db.execute(
            select(EvidenceUnit.evidence_id, EvidenceUnit.paper_id).where(
                EvidenceUnit.evidence_id.in_(evidence_ids)
            )
        ).all()
        found = {row.evidence_id: row.paper_id for row in rows}
        missing = [evidence_id for evidence_id in evidence_ids if evidence_id not in found]
        if missing:
            raise RetrievalError(f"Unknown evidence_ids: {', '.join(missing)}")
        allowed_papers = set(request.scope.paper_ids)
        if allowed_papers:
            invalid = [
                evidence_id
                for evidence_id, paper_id in found.items()
                if paper_id not in allowed_papers
            ]
            if invalid:
                raise RetrievalError("scope.evidence_ids must belong to scope.paper_ids")

    def _dense(
        self, query: str, request: EvidenceSearchRequest, limit: int
    ) -> list[RetrievalCandidate]:
        vectors = self.model_gateway.embed_texts([query])
        if len(vectors) != 1 or len(vectors[0]) != VECTOR_DIMENSION:
            raise RetrievalBackendError("embedding backend returned an invalid query vector")
        model = runtime_model_name(self.model_gateway, "embedding", self.settings.embedding_model)
        distance = EvidenceEmbedding.embedding.l2_distance(vectors[0]).label("distance")
        statement = (
            self._base_statement()
            .add_columns(distance)
            .join(
                EvidenceEmbedding,
                EvidenceEmbedding.representation_id == EvidenceRepresentation.representation_id,
            )
            .where(EvidenceEmbedding.embedding_model == model)
            .where(EvidenceEmbedding.status == "success")
            .where(EvidenceEmbedding.embedding_dim == VECTOR_DIMENSION)
            .where(EvidenceEmbedding.text_hash == EvidenceRepresentation.text_hash)
        )
        rows = self.db.execute(
            self._constrained(statement, request).order_by(distance).limit(limit)
        ).all()
        return [
            RetrievalCandidate(
                evidence=row[0], representation=row[1], score=1.0 / (1.0 + float(row[2]))
            )
            for row in rows
        ]

    def _keyword(
        self, query: str, request: EvidenceSearchRequest, limit: int
    ) -> list[RetrievalCandidate]:
        terms = [term for term in query.split() if term]
        if not terms:
            return []
        conditions = [EvidenceRepresentation.text.ilike(f"%{term}%") for term in terms]
        tsquery = func.plainto_tsquery("simple", search_terms(query))
        fulltext = EvidenceRepresentation.search_vector.op("@@")(tsquery)
        score = (
            func.ts_rank_cd(EvidenceRepresentation.search_vector, tsquery)
            + sum(case((condition, 1), else_=0) for condition in conditions)
        ).label("keyword_score")
        statement = self._constrained(self._base_statement().add_columns(score), request).where(
            or_(fulltext, and_(*conditions))
        )
        rows = self.db.execute(
            statement.order_by(score.desc(), EvidenceUnit.evidence_id).limit(limit)
        ).all()
        return [
            RetrievalCandidate(evidence=row[0], representation=row[1], score=float(row[2]))
            for row in rows
        ]

    def _rrf(
        self, dense: list[RetrievalCandidate], keyword: list[RetrievalCandidate]
    ) -> list[RetrievalCandidate]:
        combined: dict[str, RetrievalCandidate] = {}
        scores: dict[str, float] = {}
        for rank, item in enumerate(dense, start=1):
            combined[item.evidence.evidence_id] = item
            scores[item.evidence.evidence_id] = scores.get(item.evidence.evidence_id, 0.0) + 1 / (
                60 + rank
            )
        for rank, item in enumerate(keyword, start=1):
            combined.setdefault(item.evidence.evidence_id, item)
            scores[item.evidence.evidence_id] = scores.get(item.evidence.evidence_id, 0.0) + 1 / (
                60 + rank
            )
        ranked = [
            RetrievalCandidate(item.evidence, item.representation, scores[evidence_id])
            for evidence_id, item in sorted(
                combined.items(), key=lambda pair: scores[pair[0]], reverse=True
            )
        ]
        deduplicated, seen = [], set()
        for item in ranked:
            evidence = item.evidence
            key = (
                tuple(sorted(evidence.asset_ids or [])),
                sha256(evidence.raw_content.strip().encode()).hexdigest(),
                evidence.page,
                tuple(evidence.bbox or []),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)
        return deduplicated
