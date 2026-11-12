from __future__ import annotations

import re
import time
from dataclasses import dataclass

from sqlalchemy import Text, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.evidence_representation import EvidenceRepresentation
from app.models.evidence_unit import EvidenceUnit
from app.models.paper import Paper
from app.services.paper_access_service import visible_paper_clause
from app.models.paper_content import PaperContent
from app.models.paper_identifier import PaperIdentifier
from app.models.paper_search_document import PaperSearchDocument
from app.schemas.v1 import PaperFilters, PaperSearchRequest
from app.services.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    get_model_gateway,
    runtime_model_name,
)

INDEX_VERSION = "v1"


def normalized_identifier(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith(("doi:", "http://doi.org/", "https://doi.org/", "https://dx.doi.org/")):
        return re.sub(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", normalized)
    normalized = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/|^arxiv:", "", normalized)
    normalized = re.sub(r"\.pdf$", "", normalized)
    return re.sub(r"v\d+$", "", normalized)


def search_terms(text: str) -> str:
    """Keep CJK phrase recall without a database-specific tokenizer."""
    parts = []
    for token in re.findall(r"[A-Za-z0-9_+.-]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            parts.extend(token[index : index + 2] for index in range(max(1, len(token) - 1)))
            parts.append(token)
        else:
            parts.append(token)
    return " ".join(parts)


def search_values(values: list | None) -> str:
    return ", ".join(
        str(item.get("name") or item.get("value") or "") if isinstance(item, dict) else str(item)
        for item in (values or [])
        if item
    )


class PaperSearchIndexService:
    """Build the small paper-level index; evidence remains the detailed source of truth."""

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db, self.settings = db, settings

    def build_for_paper(self, paper: Paper, include_embedding: bool = False) -> None:
        content = self.db.get(PaperContent, paper.paper_id)
        authors = " ".join(
            str(item.get("name", "")) if isinstance(item, dict) else str(item)
            for item in (paper.authors or [])
        )
        keywords = " ".join(paper.keywords or [])
        # Repeating high-value fields is the smallest portable weighting scheme for simple tsvector.
        raw = "\n".join(
            part
            for part in (
                paper.title or "",
                paper.title or "",
                authors,
                authors,
                keywords,
                keywords,
                paper.abstract or "",
                content.text if content else "",
            )
            if part
        )
        text = search_terms(raw)
        tier = (
            "semantic"
            if paper.embedding_status == "ready"
            else "fulltext"
            if content
            else "metadata"
        )
        document = self.db.get(PaperSearchDocument, paper.paper_id)
        values = {
            "search_text": text,
            "search_index_version": INDEX_VERSION,
            "search_tier": tier,
        }
        if document:
            for name, value in values.items():
                setattr(document, name, value)
            document.search_vector = func.to_tsvector("simple", text)
        else:
            document = PaperSearchDocument(
                paper_id=paper.paper_id, search_vector=func.to_tsvector("simple", text), **values
            )
            self.db.add(document)
        self._sync_identifiers(paper)
        self.db.flush()
        self.db.commit()
        if include_embedding:
            self.embed_for_paper(paper, suppress_errors=True)

    def embed_for_paper(
        self, paper: Paper, *, suppress_errors: bool = False
    ) -> bool:
        keywords = " ".join(paper.keywords or [])
        text = "\n".join(
            value
            for value in (paper.title or "", paper.abstract or "", keywords)
            if value
        )
        if not text:
            return False
        self.db.commit()
        try:
            gateway = get_model_gateway(self.settings)
            vector = gateway.embed_texts([text])[0]
            if len(vector) != 1024:
                raise ValueError("Paper embedding has an invalid dimension.")
            model = runtime_model_name(
                gateway, "embedding", self.settings.embedding_model
            )
        except (ModelGatewayError, IndexError, ValueError):
            if suppress_errors:
                return False
            raise
        document = self.db.get(PaperSearchDocument, paper.paper_id)
        if not document:
            self.build_for_paper(paper, include_embedding=False)
            document = self.db.get(PaperSearchDocument, paper.paper_id)
        if not document:
            raise RuntimeError("Paper search document could not be created.")
        document.embedding = vector
        document.embedding_model = model
        self.db.commit()
        return True

    def _sync_identifiers(self, paper: Paper) -> None:
        for kind, value in (("doi", paper.doi), ("arxiv", paper.arxiv_id)):
            if not value:
                continue
            normalized = normalized_identifier(value)
            existing = self.db.scalar(
                select(PaperIdentifier).where(
                    PaperIdentifier.identifier_type == kind,
                    PaperIdentifier.normalized_value == normalized,
                )
            )
            if existing is None:
                self.db.add(
                    PaperIdentifier(
                        paper_id=paper.paper_id, identifier_type=kind, normalized_value=normalized
                    )
                )


@dataclass(slots=True)
class PaperSearchResult:
    paper: Paper
    score: float | None
    matched_fields: list[str]


class PaperSearchService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        model_gateway: ModelGateway | None = None,
        access_scope: str | None = None,
    ) -> None:
        self.db, self.settings = db, settings
        self.model_gateway = model_gateway
        self.access_scope = access_scope
        self.metrics: dict[str, int | str | bool] = {}

    def search(self, request: PaperSearchRequest) -> tuple[list[PaperSearchResult], bool]:
        query = (request.query or "").strip()
        if not query:
            started = time.perf_counter()
            results = self._browse(request.filters, request.sort.field, request.sort.order)
            self.metrics = {
                "browse_candidates": len(results),
                "browse_latency_ms": int((time.perf_counter() - started) * 1000),
            }
            return results, False
        lexical = self._lexical(query, request.filters)
        semantic, fallback = (
            self._semantic(query, request.filters)
            if request.search_mode != "lexical"
            else ([], False)
        )
        if request.search_mode == "semantic":
            candidates = semantic
        elif request.search_mode == "lexical":
            candidates, fallback = lexical, False
        else:
            candidates = self._rrf(lexical, semantic)
        return self._rerank(query, candidates), fallback

    def facets(self, filters: PaperFilters, requested: list[str]) -> dict[str, list[dict]]:
        if not requested:
            return {}
        statement = self._filtered(select(Paper), filters)
        results: dict[str, list[dict]] = {}
        columns = {
            "year": Paper.year,
            "venue": Paper.venue,
            "language": Paper.language,
            "publication_type": Paper.publication_type,
            "source_provenance": Paper.source_provenance,
        }
        for name in requested:
            if name in columns:
                rows = self.db.execute(
                    statement.with_only_columns(columns[name], func.count())
                    .where(columns[name].is_not(None))
                    .group_by(columns[name])
                    .order_by(func.count().desc())
                    .limit(20)
                ).all()
                results[name] = [{"value": value, "count": int(count)} for value, count in rows]
            elif name == "has_fulltext":
                results[name] = self._boolean_facet(
                    statement,
                    exists(
                        select(PaperContent.paper_id).where(PaperContent.paper_id == Paper.paper_id)
                    ),
                )
            elif name == "ai_ready":
                results[name] = self._boolean_facet(statement, Paper.evidence_status == "ready")
        return results

    def best_evidence(self, paper_id: str, query: str | None) -> EvidenceUnit | None:
        statement = (
            select(EvidenceUnit)
            .join(
                EvidenceRepresentation,
                EvidenceRepresentation.evidence_id == EvidenceUnit.evidence_id,
            )
            .where(
                EvidenceUnit.paper_id == paper_id,
                EvidenceUnit.exclude_from_retrieval.is_(False),
                EvidenceRepresentation.is_active.is_(True),
                EvidenceRepresentation.indexable.is_(True),
            )
        )
        base = statement
        if query:
            tsquery = func.plainto_tsquery("simple", search_terms(query))
            rank = func.ts_rank_cd(EvidenceRepresentation.search_vector, tsquery)
            statement = statement.where(
                EvidenceRepresentation.search_vector.op("@@")(tsquery)
            ).order_by(rank.desc())
            result = self.db.scalar(
                statement.order_by(EvidenceUnit.block_order, EvidenceUnit.evidence_id).limit(1)
            )
            if result:
                return result
        return self.db.scalar(
            base.order_by(EvidenceUnit.block_order, EvidenceUnit.evidence_id).limit(1)
        )

    def _filtered(self, statement, filters: PaperFilters):
        if self.access_scope is not None:
            statement = statement.where(
                visible_paper_clause(Paper.paper_id, self.access_scope)
            )
        if filters.paper_ids:
            statement = statement.where(Paper.paper_id.in_(filters.paper_ids))
        if filters.year:
            if filters.year.gte is not None:
                statement = statement.where(Paper.year >= filters.year.gte)
            if filters.year.lte is not None:
                statement = statement.where(Paper.year <= filters.year.lte)
        for field, values in (
            (Paper.venue, filters.venue),
            (Paper.language, filters.language),
            (Paper.publication_type, filters.publication_type),
            (Paper.source_provenance, filters.source_provenance),
            (Paper.parse_status, filters.parse_status),
        ):
            if values:
                statement = statement.where(field.in_(values))
        if filters.doi:
            statement = statement.where(Paper.doi == filters.doi)
        if filters.authors:
            statement = statement.where(
                or_(*[cast(Paper.authors, Text).ilike(f"%{item}%") for item in filters.authors])
            )
        if filters.keywords:
            statement = statement.where(
                or_(*[cast(Paper.keywords, Text).ilike(f"%{item}%") for item in filters.keywords])
            )
        if filters.identifier_type and filters.identifier_value:
            statement = statement.where(
                exists(
                    select(PaperIdentifier.identifier_id).where(
                        PaperIdentifier.paper_id == Paper.paper_id,
                        PaperIdentifier.identifier_type == filters.identifier_type,
                        PaperIdentifier.normalized_value
                        == normalized_identifier(filters.identifier_value),
                    )
                )
            )
        has_content = exists(
            select(PaperContent.paper_id).where(PaperContent.paper_id == Paper.paper_id)
        )
        if filters.has_fulltext is not None:
            statement = statement.where(has_content if filters.has_fulltext else ~has_content)
        if filters.ai_ready is not None:
            ready = (Paper.evidence_status == "ready") & (Paper.embedding_status == "ready")
            statement = statement.where(ready if filters.ai_ready else ~ready)
        if filters.indexed is not None:
            indexed = exists(
                select(PaperSearchDocument.paper_id).where(
                    PaperSearchDocument.paper_id == Paper.paper_id
                )
            )
            statement = statement.where(indexed if filters.indexed else ~indexed)
        return statement

    def _browse(
        self, filters: PaperFilters, field: str | None, order: str
    ) -> list[PaperSearchResult]:
        sort = {
            "year": Paper.year,
            "publication_date": Paper.publication_date,
            "created_at": Paper.created_at,
            "updated_at": Paper.updated_at,
            "paper_quality.score": Paper.paper_quality_score,
        }.get(field or "publication_date", Paper.publication_date)
        statement = self._filtered(select(Paper), filters).order_by(
            sort.desc() if order == "desc" else sort.asc(), Paper.paper_id
        )
        return [PaperSearchResult(paper, None, []) for paper in self.db.scalars(statement).all()]

    def _lexical(self, query: str, filters: PaperFilters) -> list[PaperSearchResult]:
        started = time.perf_counter()
        terms = search_terms(query)
        tsquery = func.plainto_tsquery("simple", terms)
        rank = func.ts_rank_cd(PaperSearchDocument.search_vector, tsquery).label("rank")
        statement = (
            self._filtered(
                select(Paper, rank).outerjoin(
                    PaperSearchDocument, PaperSearchDocument.paper_id == Paper.paper_id
                ),
                filters,
            )
            .where(
                or_(
                    PaperSearchDocument.search_vector.op("@@")(tsquery),
                    PaperSearchDocument.search_text.ilike(f"%{query}%"),
                    Paper.title.ilike(f"%{query}%"),
                    Paper.abstract.ilike(f"%{query}%"),
                    Paper.doi.ilike(f"%{query}%"),
                    Paper.venue.ilike(f"%{query}%"),
                )
            )
            .order_by(rank.desc(), Paper.paper_id)
            .limit(1000)
        )
        results = [
            PaperSearchResult(paper, float(score or 0), self._matched_fields(paper, query))
            for paper, score in self.db.execute(statement).all()
        ]
        self.metrics.update(
            {
                "lexical_candidates": len(results),
                "lexical_latency_ms": int((time.perf_counter() - started) * 1000),
            }
        )
        return results

    def _semantic(self, query: str, filters: PaperFilters) -> tuple[list[PaperSearchResult], bool]:
        started = time.perf_counter()
        try:
            gateway = self.model_gateway or get_model_gateway(self.settings)
            vector = gateway.embed_texts(
                [query], timeout_seconds=self.settings.paper_search_semantic_timeout_seconds
            )[0]
            if len(vector) != 1024:
                self.metrics.update(
                    {
                        "semantic_candidates": 0,
                        "semantic_latency_ms": int((time.perf_counter() - started) * 1000),
                        "semantic_degraded_reason": "invalid_embedding_dimension",
                    }
                )
                return [], True
        except (ModelGatewayError, ValueError, IndexError) as exc:
            self.metrics.update(
                {
                    "semantic_candidates": 0,
                    "semantic_latency_ms": int((time.perf_counter() - started) * 1000),
                    "semantic_degraded_reason": type(exc).__name__,
                }
            )
            return [], True
        distance = PaperSearchDocument.embedding.l2_distance(vector).label("distance")
        statement = (
            self._filtered(
                select(Paper, distance).join(
                    PaperSearchDocument, PaperSearchDocument.paper_id == Paper.paper_id
                ),
                filters,
            )
            .where(PaperSearchDocument.embedding.is_not(None))
            .order_by(distance)
            .limit(1000)
        )
        results = [
            PaperSearchResult(paper, 1 / (1 + float(value)), [])
            for paper, value in self.db.execute(statement).all()
        ]
        self.metrics.update(
            {
                "semantic_candidates": len(results),
                "semantic_latency_ms": int((time.perf_counter() - started) * 1000),
            }
        )
        return results, False

    def _rerank(self, query: str, candidates: list[PaperSearchResult]) -> list[PaperSearchResult]:
        started = time.perf_counter()
        head = candidates[:50]
        if not head:
            self.metrics.update({"rerank_candidates": 0, "rerank_applied": False})
            return candidates
        documents = [
            "\n".join(
                str(value)
                for value in (
                    item.paper.title,
                    search_values(item.paper.authors),
                    search_values(item.paper.keywords),
                    item.paper.abstract,
                )
                if value
            )[:8000]
            for item in head
        ]
        try:
            gateway = self.model_gateway or get_model_gateway(self.settings)
            ranked = gateway.rerank(query, documents, len(head))
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
                reordered.append(PaperSearchResult(item.paper, result.score, item.matched_fields))
                seen.add(result.index)
        if not reordered:
            self.metrics.update(
                {
                    "rerank_candidates": len(head),
                    "rerank_applied": False,
                    "rerank_degraded_reason": "disabled_or_empty",
                    "rerank_latency_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            return candidates
        reordered.extend(item for index, item in enumerate(head) if index not in seen)
        self.metrics.update(
            {
                "rerank_candidates": len(head),
                "rerank_applied": True,
                "rerank_latency_ms": int((time.perf_counter() - started) * 1000),
            }
        )
        return reordered + candidates[50:]

    def _rrf(
        self, lexical: list[PaperSearchResult], semantic: list[PaperSearchResult]
    ) -> list[PaperSearchResult]:
        started = time.perf_counter()
        merged: dict[str, PaperSearchResult] = {}
        scores: dict[str, float] = {}
        for values in (lexical, semantic):
            for rank, result in enumerate(values, start=1):
                merged.setdefault(result.paper.paper_id, result)
                scores[result.paper.paper_id] = scores.get(result.paper.paper_id, 0.0) + 1 / (
                    60 + rank
                )
        results = [
            PaperSearchResult(item.paper, scores[paper_id], item.matched_fields)
            for paper_id, item in sorted(
                merged.items(), key=lambda pair: scores[pair[0]], reverse=True
            )
        ]
        self.metrics.update(
            {
                "rrf_candidates": len(results),
                "rrf_latency_ms": int((time.perf_counter() - started) * 1000),
            }
        )
        return results

    def _matched_fields(self, paper: Paper, query: str) -> list[str]:
        needle = query.lower()
        return [
            name
            for name, value in (
                ("title", paper.title),
                ("abstract", paper.abstract),
                ("doi", paper.doi),
                ("venue", paper.venue),
            )
            if value and needle in value.lower()
        ]

    def _boolean_facet(self, statement, condition) -> list[dict]:
        true_count = int(
            self.db.scalar(statement.with_only_columns(func.count()).where(condition)) or 0
        )
        false_count = int(
            self.db.scalar(statement.with_only_columns(func.count()).where(~condition)) or 0
        )
        return [{"value": True, "count": true_count}, {"value": False, "count": false_count}]
