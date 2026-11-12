from __future__ import annotations

import mimetypes
import tempfile
import uuid
from hashlib import sha256
from math import ceil
from pathlib import Path
from typing import Any

import redis
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.ids import new_id
from app.db.session import get_db
from app.models.citation_context import CitationContext
from app.models.evidence_representation import EvidenceRepresentation
from app.models.evidence_unit import EvidenceUnit
from app.models.import_batch import ImportBatch, ImportBatchItem
from app.models.paper import Paper
from app.models.paper_content import PaperContent
from app.models.paper_reference import PaperReference
from app.models.paper_task import PaperTask
from app.models.resource_asset import ResourceAsset
from app.models.workflow_run import WorkflowRun
from app.schemas.v1 import (
    AssetCounts,
    AssetDetailResponse,
    AssetPageResponse,
    AssetRef,
    BibliographyFormatRequest,
    BibliographyFormatResponse,
    Capabilities,
    CitationContextPageResponse,
    CitationGroundRequest,
    EvidenceCitation,
    EvidenceContentResponse,
    EvidenceHit,
    EvidencePackRequest,
    EvidenceSearchRequest,
    EvidenceSearchResponse,
    EvidenceSourceResponse,
    ImportBatchAppendRequest,
    ImportBatchAppendResponse,
    ImportBatchCreateRequest,
    ImportBatchItemsPage,
    ImportBatchResponse,
    ImportBatchRetryResponse,
    IngestPaperResponse,
    LiteratureImportRequest,
    LiteratureSearchRequest,
    LiteratureSearchResponse,
    MetaCatalogResponse,
    Pagination,
    PaperContentResponse,
    PaperQuality,
    PaperRef,
    PaperSearchRequest,
    PaperSearchResponse,
    PaperSummary,
    ProcessingStatus,
    RelationPageResponse,
    RepresentationRef,
    SourceLocation,
    TaskResponse,
    WorkflowRunResponse,
    WorkflowSubmissionResponse,
)
from app.services.literature_search_service import LiteratureSearchService
from app.services.paper_search_service import INDEX_VERSION, PaperSearchService
from app.services.paper_access_service import can_access_paper, normalize_scope
from app.services.pipeline_batch_service import PipelineBatchService
from app.services.pipeline_service import (
    PIPELINE_STAGE_ORDER,
    PIPELINE_VERSION,
    reconcile_import_batch,
)
from app.services.retrieval_service import RetrievalError, RetrievalService
from app.services.staged_artifact_service import StagedArtifactService
from app.services.task_queue import enqueue_submitted_workflow
from app.services.task_queue_constants import PIPELINE_INTAKE_PAUSE_KEY
from app.services.trace import trace_operation
from app.services.workflow_scope_service import (
    WorkflowScopeViolation,
    validate_workflow_caller_scope,
    validate_workflow_request_scope,
    workflow_scope_projection,
)

router = APIRouter(prefix="/api/v1", tags=["v1"])


def _request_id(request: Request) -> str:
    if request.headers.get("x-request-id"):
        return request.headers["x-request-id"]
    if not getattr(request.state, "request_id", None):
        request.state.request_id = f"req_{uuid.uuid4().hex[:12]}"
    return request.state.request_id


def _json(data: Any, request: Request, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(data),
        headers={"X-Request-ID": _request_id(request)},
    )


def _error(
    request: Request, code: str, message: str, status_code: int, **details: Any
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": request_id,
            }
        },
        headers={"X-Request-ID": request_id},
    )


def _pipeline_intake_paused() -> bool:
    settings = get_settings()
    if not settings.pipeline_enabled:
        return False
    try:
        return bool(
            redis.Redis.from_url(settings.redis_url).get(
                PIPELINE_INTAKE_PAUSE_KEY
            )
        )
    except redis.exceptions.RedisError:
        return False


def _access_scope(request: Request) -> str | None:
    value = getattr(request.state, "literature_scope", None)
    try:
        return normalize_scope(value)
    except ValueError:
        return "__invalid_scope__"


def _paper_is_visible(db: Session, request: Request, paper_id: str) -> bool:
    return can_access_paper(db, paper_id, _access_scope(request))


def _evidence_is_visible(
    db: Session,
    request: Request,
    evidence: EvidenceUnit | None,
) -> bool:
    return bool(
        evidence
        and _paper_is_visible(db, request, evidence.paper_id)
    )


def _page(page: int, page_size: int, total: int) -> dict:
    pages = ceil(total / page_size) if total else 0
    return Pagination(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=pages,
        has_next=page < pages,
        has_prev=page > 1,
    ).model_dump()


@router.get("/library/overview")
def library_overview(request: Request, db: Session = Depends(get_db)):
    statuses = {}
    for field in ("parse_status", "evidence_status", "embedding_status", "multimodal_status"):
        column = getattr(Paper, field)
        statuses[field.removesuffix("_status")] = {
            status: count
            for status, count in db.execute(select(column, func.count()).group_by(column)).all()
        }
    active_task = db.scalar(
        select(PaperTask)
        .where(PaperTask.status.in_(("queued", "running")))
        .order_by(PaperTask.updated_at.desc(), PaperTask.created_at.desc())
        .limit(1)
    )
    return _json(
        {
            "papers": int(db.scalar(select(func.count()).select_from(Paper)) or 0),
            "searchable": int(
                db.scalar(select(func.count()).select_from(Paper).where(Paper.evidence_status == "ready")) or 0
            ),
            "ai_ready": int(
                db.scalar(
                    select(func.count()).select_from(Paper).where(
                        Paper.evidence_status == "ready", Paper.embedding_status == "ready"
                    )
                )
                or 0
            ),
            "statuses": statuses,
            "active_task": _task(active_task).model_dump() if active_task else None,
        },
        request,
    )


def _location(item: EvidenceUnit | ResourceAsset | CitationContext) -> SourceLocation | None:
    if isinstance(item, ResourceAsset):
        location = SourceLocation.model_validate(item.location) if item.location else None
        if (
            location
            and location.bbox
            and (
                location.page is None
                or location.coordinate_space != "normalized"
                or not _valid_normalized_bbox(location.bbox)
            )
        ):
            return None
        return location
    bbox = getattr(item, "bbox", None)
    if bbox is not None and (
        not isinstance(item.page, int)
        or item.page < 1
        or item.coordinate_space != "normalized"
        or not _valid_normalized_bbox(bbox)
    ):
        bbox = None
    values = {
        "page": item.page,
        "bbox": bbox,
        "coordinate_space": item.coordinate_space if bbox is not None else None,
        "char_start": getattr(item, "char_start", None),
        "char_end": getattr(item, "char_end", None),
        "section_title": getattr(item, "section_title", None),
        "section_type": getattr(item, "section_type", None),
    }
    return SourceLocation(**values) if any(value is not None for value in values.values()) else None


def _valid_normalized_bbox(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int | float) for item in value)
        and 0 <= value[0] < value[2] <= 1
        and 0 <= value[1] < value[3] <= 1
    )


def _paper_ref(paper: Paper) -> PaperRef:
    return PaperRef(
        paper_id=paper.paper_id,
        title=paper.title,
        year=paper.year,
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
        venue=paper.venue,
        publication_type=paper.publication_type,
        language=paper.language,
        publication_date=paper.publication_date,
    )


def _asset_refs(db: Session, paper_id: str, ids: list[str] | None = None) -> list[AssetRef]:
    if ids == []:
        return []
    statement = select(ResourceAsset).where(ResourceAsset.paper_id == paper_id)
    if ids is not None:
        statement = statement.where(ResourceAsset.asset_id.in_(ids))
    return [
        AssetRef(
            asset_id=row.asset_id,
            asset_type=row.asset_type,
            mime_type=row.mime_type,
            caption=row.caption,
            location=_location(row),
        )
        for row in db.scalars(statement).all()
    ]


def _summary(db: Session, paper: Paper) -> PaperSummary:
    content_ready = db.get(PaperContent, paper.paper_id) is not None
    fulltext = (
        "content_ready"
        if content_ready
        else "parsed_artifacts"
        if paper.parse_status == "succeeded"
        else "local_pdf"
        if paper.pdf_storage_key
        else "missing"
    )
    counts = {
        kind: int(total)
        for kind, total in db.execute(
            select(ResourceAsset.asset_type, func.count())
            .where(ResourceAsset.paper_id == paper.paper_id)
            .group_by(ResourceAsset.asset_type)
        ).all()
    }
    asset_counts = AssetCounts(**{name: counts.get(name, 0) for name in AssetCounts.model_fields})
    pdf_id = db.scalar(
        select(ResourceAsset.asset_id)
        .where(ResourceAsset.paper_id == paper.paper_id, ResourceAsset.asset_type == "pdf")
        .limit(1)
    )
    processing = ProcessingStatus(
        fulltext=fulltext,
        parse=paper.parse_status,
        evidence=paper.evidence_status,
        embedding=paper.embedding_status,
        multimodal=paper.multimodal_status,
    )
    capabilities = Capabilities(
        has_fulltext=content_ready,
        can_read_content=content_ready,
        can_search_evidence=paper.evidence_status == "ready",
        can_download_pdf=pdf_id is not None,
        has_assets=any(
            getattr(asset_counts, key) > 0
            for key in ("figure", "table", "formula", "image", "attachment")
        ),
        ai_ready=paper.evidence_status == "ready" and paper.embedding_status == "ready",
    )
    return PaperSummary(
        **_paper_ref(paper).model_dump(),
        authors=paper.authors,
        abstract=paper.abstract,
        keywords=paper.keywords,
        source_provenance=paper.source_provenance,
        source_url=paper.source_url,
        license=paper.license,
        open_access=paper.open_access,
        parse_version=paper.parse_version,
        processing_status=processing,
        capabilities=capabilities,
        asset_counts=asset_counts,
        paper_quality=PaperQuality(
            score=paper.paper_quality_score, signals=paper.paper_quality_signals or {}
        ),
    )


def _import_batch_payload(db: Session, batch: ImportBatch) -> dict:
    current_pipeline = (batch.details or {}).get("pipeline_version") == PIPELINE_VERSION
    statement = select(ImportBatchItem).where(
        ImportBatchItem.batch_id == batch.batch_id
    )
    statement = statement.order_by(
        ImportBatchItem.item_id
        if current_pipeline
        else ImportBatchItem.created_at,
        ImportBatchItem.item_id,
    )
    items = db.scalars(statement.limit(101) if current_pipeline else statement).all()
    next_cursor = items[99].item_id if current_pipeline and len(items) > 100 else None
    if next_cursor:
        items = items[:100]
    return {
        "batch_id": batch.batch_id,
        "source": batch.source,
        "status": batch.status,
        "total_items": batch.total_items,
        "completed_items": batch.completed_items,
        "failed_items": batch.failed_items,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "next_cursor": next_cursor,
        "items": [
            _import_batch_item_payload(item)
            for item in items
        ],
    }


def _import_batch_item_payload(item: ImportBatchItem) -> dict:
    return {
        "item_id": item.item_id,
        "source_key": item.source_key,
        "paper_id": item.paper_id,
        "staged_artifact_id": item.staged_artifact_id,
        "task_id": item.pipeline_task_id or (item.details or {}).get("task_id"),
        "status": item.status,
        "current_stage": item.current_stage,
        "attempts": item.attempts,
        "error_message": item.error_message,
        "details": item.details or {},
    }


def _hit(
    db: Session, evidence: EvidenceUnit, rep: EvidenceRepresentation, score: float | None = None
) -> EvidenceHit:
    paper = db.get(Paper, evidence.paper_id)
    display = (
        rep.text
        if evidence.modality != "text" or evidence.raw_content.lstrip().startswith("{")
        else evidence.raw_content
    )
    return EvidenceHit(
        evidence_id=evidence.evidence_id,
        representation=RepresentationRef(
            representation_id=rep.representation_id, type=rep.repr_type
        ),
        paper=_paper_ref(paper),
        content={"snippet": display[:500], "quote": display, "quote_language": paper.language},
        location=_location(evidence),
        modality=evidence.modality,
        block_type=evidence.block_type,
        score=score,
        assets=_asset_refs(db, evidence.paper_id, evidence.asset_ids),
    )


def _citation(db: Session, evidence: EvidenceUnit) -> EvidenceCitation:
    paper = db.get(Paper, evidence.paper_id)
    return EvidenceCitation(
        evidence_id=evidence.evidence_id,
        paper=_paper_ref(paper),
        quote=evidence.raw_content,
        location=_location(evidence),
        assets=_asset_refs(db, evidence.paper_id, evidence.asset_ids),
    )


def _paper_statement(filters):
    statement = select(Paper)
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
    content_exists = exists(
        select(PaperContent.paper_id).where(PaperContent.paper_id == Paper.paper_id)
    )
    if filters.has_fulltext is True:
        statement = statement.where(content_exists)
    if filters.has_fulltext is False:
        statement = statement.where(~content_exists)
    if filters.ai_ready is True:
        statement = statement.where(
            Paper.evidence_status == "ready", Paper.embedding_status == "ready"
        )
    if filters.ai_ready is False:
        statement = statement.where(
            or_(Paper.evidence_status != "ready", Paper.embedding_status != "ready")
        )
    return statement


@router.get("/papers/meta-catalog", response_model=MetaCatalogResponse)
def meta_catalog(
    request: Request, include_sample_values: bool = False, db: Session = Depends(get_db)
):
    fields = []
    for name, type_, paper_filter, evidence_filter, sortable, operators in [
        ("year", "integer", True, True, True, ["eq", "gte", "lte"]),
        ("venue", "string", True, True, False, ["eq", "in", "match_phrase"]),
        ("doi", "string", True, True, False, ["eq"]),
        ("identifier", "object", True, False, False, ["eq"]),
        ("authors", "string", True, False, False, ["match_phrase"]),
        ("keywords", "string", True, False, False, ["match_phrase"]),
        ("source_provenance", "enum", True, False, False, ["eq", "in"]),
        ("language", "enum", True, True, False, ["eq", "in"]),
        ("publication_type", "enum", True, True, False, ["eq", "in"]),
        ("has_fulltext", "boolean", True, False, False, ["eq"]),
        ("ai_ready", "boolean", True, False, False, ["eq"]),
        ("indexed", "boolean", True, False, False, ["eq"]),
        ("modality", "enum", False, True, False, ["eq", "in"]),
        ("block_type", "enum", False, True, False, ["eq", "in"]),
        ("section_type", "enum", False, True, False, ["eq", "in"]),
        ("has_assets", "boolean", False, True, False, ["eq"]),
    ]:
        item = {
            "name": name,
            "type": type_,
            "description": {
                "has_fulltext": "Paper has readable full text.",
                "ai_ready": "Evidence and embedding statuses are both ready.",
                "has_assets": "Evidence has one or more linked assets.",
            }.get(name, f"{name} metadata field."),
            "paper_search_filterable": paper_filter,
            "evidence_search_filterable": evidence_filter,
            "sortable": sortable,
            "operators": operators,
        }
        if include_sample_values and name in {"venue", "language", "publication_type"}:
            column = getattr(Paper, name)
            item["sample_values"] = [
                value
                for value in db.scalars(
                    select(column).where(column.is_not(None)).distinct().limit(20)
                ).all()
                if value
            ]
        fields.append(item)
    return _json(
        {
            "fields": fields,
            "sort_fields": [
                "relevance",
                "year",
                "publication_date",
                "created_at",
                "updated_at",
                "paper_quality.score",
            ],
            "default_sort": {"field": "publication_date", "order": "desc"},
            "default_fields": [
                "paper_id",
                "title",
                "year",
                "doi",
                "venue",
                "publication_type",
                "language",
            ],
            "filter_semantics": "Values within one list filter are ORed; different filter fields are ANDed.",
        },
        request,
    )


@router.post("/papers/search", response_model=PaperSearchResponse)
def search_papers(body: PaperSearchRequest, request: Request, db: Session = Depends(get_db)):
    query = (body.query or "").strip()
    with trace_operation(
        "papers.search",
        request_id=_request_id(request),
        search_mode=body.search_mode,
        filter_fields=sorted(
            name for name, value in body.filters.model_dump().items() if value is not None
        ),
        page=body.page,
        page_size=body.page_size,
        query_length=len(query),
        query_sha256=sha256(query.encode()).hexdigest() if query else None,
    ) as trace:
        service = PaperSearchService(
            db,
            get_settings(),
            access_scope=_access_scope(request),
        )
        candidates, semantic_fallback = service.search(body)
        trace.record(
            "papers_search_candidates", {**service.metrics, "semantic_fallback": semantic_fallback}
        )
        total = len(candidates)
        selected = candidates[(body.page - 1) * body.page_size : body.page * body.page_size]
        items = []
        evidence_span = trace.start_span("best_evidence", candidates=len(selected))
        for candidate in selected:
            paper = _summary(db, candidate.paper).model_dump()
            evidence = service.best_evidence(candidate.paper.paper_id, query)
            highlights = []
            for field in candidate.matched_fields:
                value = getattr(candidate.paper, field, None)
                if value:
                    highlights.append({"field": field, "text": str(value)[:500]})
            paper.update(
                {
                    "score": candidate.score,
                    "matched_fields": candidate.matched_fields,
                    "highlights": highlights,
                    "best_evidence": {
                        "evidence_id": evidence.evidence_id,
                        "quote": evidence.raw_content[:1000],
                        "location": _location(evidence).model_dump()
                        if _location(evidence)
                        else None,
                    }
                    if evidence
                    else None,
                }
            )
            items.append(paper)
        trace.finish_span(evidence_span, returned=len(items))
        facets_span = trace.start_span("facets", requested=len(body.facets))
        facets = service.facets(body.filters, body.facets)
        trace.finish_span(facets_span, returned=len(facets))
        trace.record("papers_search_result", {"total_candidates": total, "returned": len(items)})
        return _json(
            {
                "items": items,
                "pagination": _page(body.page, body.page_size, total),
                "facets": facets,
                "search_info": {
                    "mode": body.search_mode,
                    "index_version": INDEX_VERSION,
                    "semantic_fallback": semantic_fallback,
                    "rerank_applied": bool(service.metrics.get("rerank_applied")),
                    "rerank_candidates": int(service.metrics.get("rerank_candidates", 0)),
                    "rerank_degraded_reason": service.metrics.get("rerank_degraded_reason"),
                },
            },
            request,
        )


@router.post("/literature/search", response_model=LiteratureSearchResponse)
def search_literature(
    body: LiteratureSearchRequest, request: Request, db: Session = Depends(get_db)
):
    try:
        result = LiteratureSearchService(db, get_settings()).search(body)
    except ValueError as exc:
        return _error(request, "INVALID_CURSOR", str(exc), 400)
    except SQLAlchemyError:
        db.rollback()
        return _error(
            request,
            "LOCAL_CATALOG_UNAVAILABLE",
            "The local paper catalog is unavailable.",
            503,
        )

    items = []
    for hit in result["items"]:
        local_summary = _summary(db, hit.local_paper) if hit.local_paper else None
        if hit.local_paper:
            availability = (
                "local_ready"
                if hit.local_paper.evidence_status == "ready"
                and hit.local_paper.embedding_status == "ready"
                else "local_processing"
            )
        elif hit.remote and hit.remote["downloadable"]:
            availability = "remote_downloadable"
        else:
            availability = "metadata_only"
        items.append(
            {
                "identity_key": hit.identity_key,
                "metadata": hit.metadata,
                "origins": hit.origins,
                "local_paper": local_summary,
                "remote": hit.remote,
                "remote_options": hit.remote_options,
                "availability": availability,
                "score": hit.score,
                "possible_duplicate": hit.possible_duplicate,
            }
        )
    return _json({**result, "items": items}, request)


@router.post(
    "/literature/imports",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ImportBatchResponse,
)
def import_literature(
    body: LiteratureImportRequest, request: Request, db: Session = Depends(get_db)
):
    if not get_settings().pipeline_enabled:
        return _error(request, "PIPELINE_DISABLED", "The import pipeline is disabled.", 503)
    if _pipeline_intake_paused():
        return _error(
            request,
            "PIPELINE_INTAKE_PAUSED",
            "The import pipeline is not accepting new imports.",
            503,
        )
    service = PipelineBatchService(db)
    batch = service.create_remote_batch(source="remote:auto")
    service.append_remote_items(batch, [item.model_dump() for item in body.items])
    batch.intake_closed = True
    batch.expansion_status = "completed"
    reconcile_import_batch(db, batch.batch_id)
    db.commit()
    return _json(_import_batch_payload(db, batch), request, status.HTTP_202_ACCEPTED)


@router.post(
    "/import-batches",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ImportBatchResponse,
)
def create_import_batch(
    body: ImportBatchCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.pipeline_enabled:
        return _error(
            request,
            "PIPELINE_DISABLED",
            "The import pipeline is disabled.",
            503,
        )
    if _pipeline_intake_paused():
        return _error(
            request,
            "PIPELINE_INTAKE_PAUSED",
            "The import pipeline is not accepting new import batches.",
            503,
        )
    batch = PipelineBatchService(db).create_remote_batch(
        source=body.source,
        priority=body.priority,
    )
    db.commit()
    return _json(
        _import_batch_payload(db, batch),
        request,
        status.HTTP_202_ACCEPTED,
    )


@router.post(
    "/import-batches/manifests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ImportBatchResponse,
)
def create_manifest_import_batch(
    request: Request,
    file: UploadFile = File(...),
    priority: int = Form(default=50, ge=0, le=100),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.pipeline_enabled:
        return _error(
            request,
            "PIPELINE_DISABLED",
            "The import pipeline is disabled.",
            503,
        )
    if _pipeline_intake_paused():
        return _error(
            request,
            "PIPELINE_INTAKE_PAUSED",
            "The import pipeline is not accepting new manifests.",
            503,
        )
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".jsonl", ".csv"}:
        return _error(
            request,
            "INVALID_MANIFEST",
            "Manifest must be a .jsonl or .csv file.",
            400,
        )
    manifest_id = new_id("manifest")
    storage_key = f"manifests/{manifest_id}{suffix}"
    path = (Path(settings.storage_root).resolve() / storage_key).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with path.open("wb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise ValueError(
                        "Manifest exceeds the configured upload limit."
                    )
                output.write(chunk)
        if not size:
            raise ValueError("Manifest is empty.")
        batch = PipelineBatchService(db).create_remote_batch(
            priority=priority,
            manifest_storage_key=storage_key,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        path.unlink(missing_ok=True)
        return _error(request, "INVALID_MANIFEST", str(exc), 400)
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return _json(
        _import_batch_payload(db, batch),
        request,
        status.HTTP_202_ACCEPTED,
    )


@router.post(
    "/import-batches/{batch_id}/items",
    response_model=ImportBatchAppendResponse,
)
def append_import_batch_items(
    batch_id: str,
    body: ImportBatchAppendRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    if not get_settings().pipeline_enabled:
        return _error(request, "PIPELINE_DISABLED", "The import pipeline is disabled.", 503)
    if _pipeline_intake_paused():
        return _error(
            request,
            "PIPELINE_INTAKE_PAUSED",
            "The import pipeline is not accepting new batch items.",
            503,
        )
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return _error(
            request,
            "IMPORT_BATCH_NOT_FOUND",
            "Import batch not found.",
            404,
            batch_id=batch_id,
        )
    if (batch.details or {}).get("pipeline_version") != PIPELINE_VERSION:
        return _error(
            request,
            "IMPORT_BATCH_NOT_APPENDABLE",
            "This import batch does not support chunked intake.",
            409,
        )
    if batch.intake_closed:
        return _error(
            request,
            "IMPORT_BATCH_INTAKE_CLOSED",
            "This import batch has already been finalized.",
            409,
        )
    task_ids = PipelineBatchService(db).append_remote_items(
        batch, [item.model_dump() for item in body.items]
    )
    db.commit()
    return _json(
        {
            "batch_id": batch.batch_id,
            "accepted_items": len(task_ids),
            "total_items": batch.total_items,
            "status": batch.status,
        },
        request,
    )


@router.post(
    "/import-batches/{batch_id}/finalize",
    response_model=ImportBatchResponse,
)
def finalize_import_batch(
    batch_id: str, request: Request, db: Session = Depends(get_db)
):
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return _error(
            request,
            "IMPORT_BATCH_NOT_FOUND",
            "Import batch not found.",
            404,
            batch_id=batch_id,
        )
    if (batch.details or {}).get("pipeline_version") != PIPELINE_VERSION:
        return _error(
            request,
            "IMPORT_BATCH_NOT_FINALIZABLE",
            "This historical import batch is read-only.",
            409,
        )
    batch.intake_closed = True
    batch.expansion_status = "completed"
    reconcile_import_batch(db, batch.batch_id)
    db.commit()
    return _json(_import_batch_payload(db, batch), request)


@router.get("/import-batches/{batch_id}", response_model=ImportBatchResponse)
def get_import_batch(batch_id: str, request: Request, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return _error(
            request, "IMPORT_BATCH_NOT_FOUND", "Import batch not found.", 404, batch_id=batch_id
        )
    return _json(_import_batch_payload(db, batch), request)


@router.get(
    "/import-batches/{batch_id}/items",
    response_model=ImportBatchItemsPage,
)
def get_import_batch_items(
    batch_id: str,
    request: Request,
    cursor: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    if not db.get(ImportBatch, batch_id):
        return _error(
            request,
            "IMPORT_BATCH_NOT_FOUND",
            "Import batch not found.",
            404,
            batch_id=batch_id,
        )
    page_size = min(max(limit, 1), 500)
    statement = (
        select(ImportBatchItem)
        .where(ImportBatchItem.batch_id == batch_id)
        .order_by(ImportBatchItem.item_id)
        .limit(page_size + 1)
    )
    if cursor:
        statement = statement.where(ImportBatchItem.item_id > cursor)
    items = db.scalars(statement).all()
    next_cursor = items[page_size - 1].item_id if len(items) > page_size else None
    if next_cursor:
        items = items[:page_size]
    return _json(
        {
            "items": [_import_batch_item_payload(item) for item in items],
            "next_cursor": next_cursor,
        },
        request,
    )


@router.post("/import-batches/{batch_id}/retry-failed", response_model=ImportBatchRetryResponse)
def retry_import_batch(batch_id: str, request: Request, db: Session = Depends(get_db)):
    if not get_settings().pipeline_enabled:
        return _error(request, "PIPELINE_DISABLED", "The import pipeline is disabled.", 503)
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return _error(
            request, "IMPORT_BATCH_NOT_FOUND", "Import batch not found.", 404, batch_id=batch_id
        )
    if (batch.details or {}).get("pipeline_version") != PIPELINE_VERSION:
        return _error(
            request,
            "IMPORT_BATCH_READ_ONLY",
            "This historical import batch is read-only.",
            409,
        )
    retried = PipelineBatchService(db).retry_failed_items(batch)
    db.commit()
    return _json(
        {"batch_id": batch.batch_id, "retried_items": retried, "status": batch.status}, request
    )


@router.post(
    "/papers",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestPaperResponse,
)
def ingest_paper(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.pipeline_enabled:
        return _error(request, "PIPELINE_DISABLED", "The import pipeline is disabled.", 503)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return _error(request, "INVALID_ARGUMENT", "Uploaded file must be a PDF.", 400)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as output:
            temporary_path = Path(output.name)
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise ValueError("Uploaded PDF exceeds the configured limit.")
                output.write(chunk)
        if not size:
            raise ValueError("Uploaded PDF file is empty.")
        metadata = {"title": title} if title else {}
        access_scope = _access_scope(request)
        if access_scope is not None:
            metadata["_access_scope"] = access_scope
        artifact = StagedArtifactService(db, settings).stage_local_file(
            temporary_path,
            source_type="user_upload",
            source_external_id=file.filename,
            metadata_payload=metadata,
        )
        pipeline = PipelineBatchService(db)
        batch = pipeline.create_staged_batch(source="user_upload", priority=100)
        pipeline.append_staged_artifacts(
            batch,
            [
                {
                    "artifact": artifact,
                    "source_key": file.filename,
                    "metadata_payload": metadata,
                }
            ],
        )
        batch.intake_closed = True
        batch.expansion_status = "completed"
        reconcile_import_batch(db, batch.batch_id)
        db.commit()
        item = db.scalar(
            select(ImportBatchItem).where(
                ImportBatchItem.batch_id == batch.batch_id
            )
        )
        task = db.get(PaperTask, item.pipeline_task_id) if item else None
        if not item or not task:
            raise RuntimeError("Upload Pipeline was not created.")
    except ValueError as exc:
        db.rollback()
        return _error(request, "INVALID_ARGUMENT", str(exc), 400)
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
    return _json(
        {
            "batch_id": batch.batch_id,
            "item_id": item.item_id,
            "task": _task(task).model_dump(),
        },
        request,
        status.HTTP_202_ACCEPTED,
    )


@router.get("/papers/{paper_id}", response_model=PaperSummary)
def get_paper(paper_id: str, request: Request, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    return (
        _json(_summary(db, paper).model_dump(), request)
        if paper and _paper_is_visible(db, request, paper_id)
        else _error(request, "PAPER_NOT_FOUND", "Paper not found.", 404, paper_id=paper_id)
    )


@router.get(
    "/papers/{paper_id}/pdf",
    responses={
        200: {
            "content": {
                "application/pdf": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
def download_paper_pdf(
    paper_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not db.get(Paper, paper_id) or not _paper_is_visible(db, request, paper_id):
        return _error(
            request,
            "PAPER_NOT_FOUND",
            "Paper not found.",
            404,
            paper_id=paper_id,
        )
    asset = db.scalar(
        select(ResourceAsset)
        .where(
            ResourceAsset.paper_id == paper_id,
            ResourceAsset.asset_type == "pdf",
            ResourceAsset.storage_key.is_not(None),
        )
        .order_by(ResourceAsset.created_at.asc(), ResourceAsset.asset_id.asc())
        .limit(1)
    )
    if asset is None or not asset.storage_key:
        return _error(
            request,
            "PDF_NOT_FOUND",
            "Paper PDF not found.",
            404,
            paper_id=paper_id,
        )
    root = Path(get_settings().storage_root).resolve()
    path = Path(asset.storage_key)
    path = path if path.is_absolute() else root / path
    try:
        path = path.resolve()
        path.relative_to(root)
    except ValueError:
        return _error(
            request,
            "PDF_NOT_FOUND",
            "Paper PDF not found.",
            404,
            paper_id=paper_id,
        )
    if not path.is_file():
        return _error(
            request,
            "PDF_NOT_FOUND",
            "Paper PDF not found.",
            404,
            paper_id=paper_id,
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        headers={"X-Request-ID": _request_id(request)},
    )


@router.get("/papers/{paper_id}/content", response_model=PaperContentResponse)
def get_paper_content(
    paper_id: str,
    request: Request,
    offset: int = 0,
    limit: int = 2000,
    db: Session = Depends(get_db),
):
    if offset < 0 or not 1 <= limit <= 10000:
        return _error(request, "INVALID_ARGUMENT", "offset or limit is out of range.", 400)
    paper = db.get(Paper, paper_id)
    if not paper or not _paper_is_visible(db, request, paper_id):
        return _error(request, "PAPER_NOT_FOUND", "Paper not found.", 404, paper_id=paper_id)
    content = db.get(PaperContent, paper_id)
    if not content:
        return _error(
            request,
            "CONTENT_NOT_READY",
            "Paper content is not ready.",
            409,
            paper_id=paper_id,
            fulltext_status=_summary(db, paper).processing_status.fulltext,
        )
    text = content.text[offset : offset + limit]
    end = offset + len(text)
    return _json(
        {
            "paper": _paper_ref(paper).model_dump(),
            "content": {
                "text": text,
                "char_start": offset,
                "char_end": end,
                "chars_returned": len(text),
                "next_offset": end if end < content.char_length else None,
                "more": end < content.char_length,
            },
        },
        request,
    )


@router.get("/papers/{paper_id}/evidence", response_model=EvidenceSearchResponse)
def browse_paper_evidence(
    paper_id: str,
    request: Request,
    modality: str | None = None,
    block_type: str | None = None,
    section_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    if page < 1 or not 1 <= page_size <= 100:
        return _error(request, "INVALID_ARGUMENT", "Invalid pagination.", 400)
    if not db.get(Paper, paper_id) or not _paper_is_visible(
        db, request, paper_id
    ):
        return _error(request, "PAPER_NOT_FOUND", "Paper not found.", 404, paper_id=paper_id)
    statement = (
        select(EvidenceUnit, EvidenceRepresentation)
        .join(EvidenceRepresentation)
        .where(EvidenceUnit.paper_id == paper_id, EvidenceRepresentation.is_active.is_(True))
    )
    if modality:
        statement = statement.where(EvidenceUnit.modality == modality)
    if block_type:
        statement = statement.where(EvidenceUnit.block_type == block_type)
    if section_type:
        statement = statement.where(EvidenceUnit.section_type == section_type)
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = db.execute(
        statement.order_by(EvidenceUnit.block_order, EvidenceUnit.evidence_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return _json(
        {
            "items": [_hit(db, evidence, rep).model_dump() for evidence, rep in rows],
            "pagination": _page(page, page_size, total),
        },
        request,
    )


@router.get("/papers/{paper_id}/assets", response_model=AssetPageResponse)
def browse_assets(
    paper_id: str,
    request: Request,
    asset_types: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    if page < 1 or not 1 <= page_size <= 100:
        return _error(request, "INVALID_ARGUMENT", "Invalid pagination.", 400)
    if not db.get(Paper, paper_id) or not _paper_is_visible(
        db, request, paper_id
    ):
        return _error(request, "PAPER_NOT_FOUND", "Paper not found.", 404, paper_id=paper_id)
    statement = select(ResourceAsset).where(ResourceAsset.paper_id == paper_id)
    if asset_types:
        statement = statement.where(
            ResourceAsset.asset_type.in_(
                [item.strip() for item in asset_types.split(",") if item.strip()]
            )
        )
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = db.scalars(
        statement.order_by(ResourceAsset.asset_id).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return _json(
        {
            "items": [
                AssetRef(
                    asset_id=item.asset_id,
                    asset_type=item.asset_type,
                    mime_type=item.mime_type,
                    caption=item.caption,
                    location=_location(item),
                ).model_dump()
                for item in rows
            ],
            "pagination": _page(page, page_size, total),
        },
        request,
    )


@router.post("/evidence/search", response_model=EvidenceSearchResponse)
def search_evidence(body: EvidenceSearchRequest, request: Request, db: Session = Depends(get_db)):
    with trace_operation(
        "evidence.search",
        request_id=_request_id(request),
        page=body.page,
        page_size=body.page_size,
        query_length=len(body.query),
        query_sha256=sha256(body.query.encode()).hexdigest(),
    ) as trace:
        try:
            service = RetrievalService(
                db,
                get_settings(),
                access_scope=_access_scope(request),
            )
            candidates = service.search(body)
        except RetrievalError as exc:
            return _error(
                request,
                getattr(exc, "code", "INVALID_ARGUMENT"),
                str(exc),
                503 if getattr(exc, "code", "") == "BACKEND_UNAVAILABLE" else 400,
            )
        total = len(candidates)
        selected = candidates[(body.page - 1) * body.page_size : body.page * body.page_size]
        trace.record(
            "evidence_search_result",
            {**service.metrics, "total_candidates": total, "returned": len(selected)},
        )
        return _json(
            {
                "items": [
                    _hit(db, item.evidence, item.representation, item.score).model_dump()
                    for item in selected
                ],
                "pagination": _page(body.page, body.page_size, total),
                "search_info": {
                    "mode": "hybrid",
                    "index_version": INDEX_VERSION,
                    "semantic_fallback": False,
                    "rerank_applied": bool(service.metrics.get("rerank_applied")),
                    "rerank_candidates": int(service.metrics.get("rerank_candidates", 0)),
                    "rerank_degraded_reason": service.metrics.get("rerank_degraded_reason"),
                },
            },
            request,
        )


@router.get("/evidence/{evidence_id}", response_model=EvidenceHit)
def get_evidence(evidence_id: str, request: Request, db: Session = Depends(get_db)):
    evidence = db.get(EvidenceUnit, evidence_id)
    if not _evidence_is_visible(db, request, evidence):
        return _error(
            request, "EVIDENCE_NOT_FOUND", "Evidence not found.", 404, evidence_id=evidence_id
        )
    rep = db.scalar(
        select(EvidenceRepresentation)
        .where(
            EvidenceRepresentation.evidence_id == evidence_id,
            EvidenceRepresentation.is_active.is_(True),
        )
        .limit(1)
    )
    if not rep:
        return _error(
            request,
            "CONTENT_NOT_READY",
            "Evidence representation is not ready.",
            409,
            evidence_id=evidence_id,
        )
    return _json(_hit(db, evidence, rep).model_dump(exclude={"score"}), request)


@router.get("/evidence/{evidence_id}/content", response_model=EvidenceContentResponse)
def get_evidence_content(
    evidence_id: str,
    request: Request,
    before: int = 1200,
    after: int = 2400,
    db: Session = Depends(get_db),
):
    if before < 0 or after < 0:
        return _error(request, "INVALID_ARGUMENT", "before and after must be non-negative.", 400)
    evidence = db.get(EvidenceUnit, evidence_id)
    if not _evidence_is_visible(db, request, evidence):
        return _error(
            request, "EVIDENCE_NOT_FOUND", "Evidence not found.", 404, evidence_id=evidence_id
        )
    content = db.get(PaperContent, evidence.paper_id)
    if not content or evidence.char_start is None or evidence.char_end is None:
        return _error(
            request,
            "CONTENT_NOT_READY",
            "Evidence content is not ready.",
            409,
            evidence_id=evidence_id,
        )
    start, end = (
        max(0, evidence.char_start - before),
        min(content.char_length, evidence.char_end + after),
    )
    paper = db.get(Paper, evidence.paper_id)
    return _json(
        {
            "evidence_id": evidence_id,
            "paper": _paper_ref(paper).model_dump(),
            "content": {
                "text": content.text[start:end],
                "evidence_char_start": evidence.char_start,
                "evidence_char_end": evidence.char_end,
                "window_char_start": start,
                "window_char_end": end,
                "more_before": start > 0,
                "more_after": end < content.char_length,
            },
            "location": _location(evidence).model_dump() if _location(evidence) else None,
            "assets": [
                item.model_dump() for item in _asset_refs(db, evidence.paper_id, evidence.asset_ids)
            ],
        },
        request,
    )


@router.get("/evidence/{evidence_id}/source", response_model=EvidenceSourceResponse)
def get_evidence_source(evidence_id: str, request: Request, db: Session = Depends(get_db)):
    evidence = db.get(EvidenceUnit, evidence_id)
    if not _evidence_is_visible(db, request, evidence):
        return _error(
            request, "EVIDENCE_NOT_FOUND", "Evidence not found.", 404, evidence_id=evidence_id
        )
    pdf_asset_id = db.scalar(
        select(ResourceAsset.asset_id)
        .where(ResourceAsset.paper_id == evidence.paper_id, ResourceAsset.asset_type == "pdf")
        .limit(1)
    )
    return _json(
        {"citation": _citation(db, evidence).model_dump(), "pdf_asset_id": pdf_asset_id}, request
    )


@router.get("/assets/{asset_id}", response_model=AssetDetailResponse)
def get_asset(asset_id: str, request: Request, db: Session = Depends(get_db)):
    asset = db.get(ResourceAsset, asset_id)
    if not asset or not _paper_is_visible(db, request, asset.paper_id):
        return _error(request, "ASSET_NOT_FOUND", "Asset not found.", 404, asset_id=asset_id)
    return _json(
        {
            "asset_id": asset.asset_id,
            "paper": _paper_ref(db.get(Paper, asset.paper_id)).model_dump(),
            "asset_type": asset.asset_type,
            "mime_type": asset.mime_type,
            "sha256": asset.sha256,
            "size_bytes": asset.size_bytes,
            "caption": asset.caption,
            "location": _location(asset).model_dump() if _location(asset) else None,
            "created_from": asset.created_from,
        },
        request,
    )


@router.get(
    "/assets/{asset_id}/download",
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
def download_asset(asset_id: str, request: Request, db: Session = Depends(get_db)):
    asset = db.get(ResourceAsset, asset_id)
    if (
        not asset
        or not asset.storage_key
        or not _paper_is_visible(db, request, asset.paper_id)
    ):
        return _error(request, "ASSET_NOT_FOUND", "Asset not found.", 404, asset_id=asset_id)
    root = Path(get_settings().storage_root).resolve()
    path = Path(asset.storage_key)
    path = path if path.is_absolute() else root / path
    try:
        path = path.resolve()
        path.relative_to(root)
    except ValueError:
        return _error(request, "ASSET_NOT_FOUND", "Asset not found.", 404, asset_id=asset_id)
    if not path.is_file():
        return _error(request, "ASSET_NOT_FOUND", "Asset not found.", 404, asset_id=asset_id)
    return FileResponse(
        path,
        media_type=asset.mime_type
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream",
        filename=path.name,
        headers={"X-Request-ID": _request_id(request)},
    )


@router.get("/papers/{paper_id}/relations", response_model=RelationPageResponse)
def list_relations(
    paper_id: str,
    relation: str,
    request: Request,
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_db),
):
    if relation not in {"references", "citations"}:
        return _error(request, "INVALID_ARGUMENT", "relation must be references or citations.", 400)
    if page < 1 or not 1 <= page_size <= 100:
        return _error(request, "INVALID_ARGUMENT", "Invalid pagination.", 400)
    if not db.get(Paper, paper_id):
        return _error(request, "PAPER_NOT_FOUND", "Paper not found.", 404, paper_id=paper_id)
    statement = select(PaperReference).where(
        PaperReference.paper_id == paper_id
        if relation == "references"
        else PaperReference.target_paper_id == paper_id
    )
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = db.scalars(
        statement.order_by(PaperReference.order_index, PaperReference.reference_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = []
    for row in rows:
        target = db.get(Paper, row.target_paper_id) if row.target_paper_id else None
        names = [
            item.get("name", str(item)) if isinstance(item, dict) else str(item)
            for item in (row.authors or [])
        ]
        items.append(
            {
                "relation": relation,
                "reference_id": row.reference_id,
                "paper_id": row.paper_id,
                "raw_text": row.raw_text,
                "parsed_title": row.title,
                "parsed_authors": names,
                "parsed_year": row.year,
                "doi": row.doi,
                "arxiv_id": row.arxiv_id,
                "venue": row.venue,
                "target_paper": _paper_ref(target).model_dump() if target else None,
                "order_index": row.order_index,
            }
        )
    return _json({"items": items, "pagination": _page(page, page_size, total)}, request)


@router.get("/papers/{paper_id}/citation-contexts", response_model=CitationContextPageResponse)
def list_citation_contexts(
    paper_id: str,
    request: Request,
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_db),
):
    if page < 1 or not 1 <= page_size <= 100:
        return _error(request, "INVALID_ARGUMENT", "Invalid pagination.", 400)
    if not db.get(Paper, paper_id):
        return _error(request, "PAPER_NOT_FOUND", "Paper not found.", 404, paper_id=paper_id)
    statement = select(CitationContext).where(CitationContext.paper_id == paper_id)
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = db.scalars(
        statement.order_by(CitationContext.context_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = []
    for row in rows:
        ref = db.get(PaperReference, row.reference_id) if row.reference_id else None
        target = db.get(Paper, ref.target_paper_id) if ref and ref.target_paper_id else None
        items.append(
            {
                "citation_context_id": row.context_id,
                "paper": _paper_ref(db.get(Paper, row.paper_id)).model_dump(),
                "reference": {
                    "reference_id": ref.reference_id,
                    "parsed_title": ref.title,
                    "parsed_year": ref.year,
                    "doi": ref.doi,
                    "target_paper": _paper_ref(target).model_dump() if target else None,
                }
                if ref
                else None,
                "context": {
                    "quote": row.quote,
                    "location": _location(row).model_dump() if _location(row) else None,
                },
            }
        )
    return _json({"items": items, "pagination": _page(page, page_size, total)}, request)


@router.post("/bibliography/format", response_model=BibliographyFormatResponse)
def format_bibliography(
    body: BibliographyFormatRequest, request: Request, db: Session = Depends(get_db)
):
    if body.style not in {"gbt7714", "apa", "ieee"}:
        return _error(request, "INVALID_ARGUMENT", "Unsupported bibliography style.", 400)
    result = []
    for kind, ids, model, code in [
        ("paper", body.paper_ids, Paper, "PAPER_NOT_FOUND"),
        ("reference", body.reference_ids, PaperReference, "REFERENCE_NOT_FOUND"),
    ]:
        for id_ in ids:
            row = db.get(model, id_)
            if not row:
                return _error(
                    request, code, f"{kind.title()} not found.", 404, **{f"{kind}_id": id_}
                )
            names = (
                ", ".join(
                    str(item.get("name"))
                    for item in (row.authors or [])
                    if isinstance(item, dict) and item.get("name")
                )
                or "Unknown author"
            )
            title = row.title or "Untitled"
            year = row.year or "n.d."
            venue = f". {row.venue}" if row.venue else ""
            doi = f". DOI: {row.doi}" if row.doi else ""
            index = len(result) + 1
            formatted = (
                f"{names} ({year}). {title}{venue}{doi}."
                if body.style == "apa"
                else f'[{index}] {names}, "{title}"{venue}, {year}{doi}.'
                if body.style == "ieee"
                else f"[{index}] {names}. {title}{venue}, {year}{doi}."
            )
            result.append({"source_type": kind, f"{kind}_id": id_, "formatted": formatted})
    return _json({"items": result, "style": body.style}, request)


def _task(task: PaperTask) -> TaskResponse:
    details = task.details or {}
    safe_details = {
        key: details[key]
        for key in ("stage", "provider", "result", "queue")
        if key in details
    }
    if task.pipeline_version == PIPELINE_VERSION:
        safe_details["pipeline"] = {
            "root_task_id": task.root_task_id,
            "stage": task.stage,
            "queue_name": task.queue_name,
            "delivery_status": task.delivery_status,
            "next_action": task.next_action,
            "attempt": task.attempt_count,
            "max_attempts": task.max_attempts,
            "next_retry_at": task.next_retry_at,
        }
    return TaskResponse(
        task_id=task.task_id,
        paper_id=task.paper_id,
        operation=task.operation,
        status=task.status,
        progress=task.progress,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        details=safe_details,
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    task = db.get(PaperTask, task_id)
    return (
        _json(_task(task).model_dump(), request)
        if task
        else _error(request, "TASK_NOT_FOUND", "Task not found.", 404, task_id=task_id)
    )


@router.get("/tasks/{task_id}/stages", response_model=list[TaskResponse])
def get_task_stages(
    task_id: str, request: Request, db: Session = Depends(get_db)
):
    task = db.get(PaperTask, task_id)
    if not task:
        return _error(
            request, "TASK_NOT_FOUND", "Task not found.", 404, task_id=task_id
        )
    root_id = task.task_id if task.operation == "pipeline" else task.root_task_id
    if not root_id:
        return _json([], request)
    rows = db.scalars(
        select(PaperTask)
        .where(
            PaperTask.root_task_id == root_id,
            PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
        )
        .order_by(PaperTask.created_at)
    ).all()
    order = {
        value: index
        for index, value in enumerate(PIPELINE_STAGE_ORDER)
    }
    rows.sort(key=lambda row: order[row.operation])
    return _json([_task(row).model_dump() for row in rows], request)


@router.post("/tasks/{task_id}/refresh", response_model=TaskResponse)
def refresh_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    return get_task(task_id, request, db)


def _workflow(run: WorkflowRun) -> dict:
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "status": run.status,
        "progress": run.progress,
        "scope_binding": workflow_scope_projection(run).model_dump(mode="json"),
        "request": run.request_payload,
        "result": run.result_payload,
        "details": run.details,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _submit_workflow(workflow: str, payload: dict, request: Request, db: Session):
    access_scope = _access_scope(request)
    try:
        scope_binding = validate_workflow_request_scope(
            db,
            access_scope,
            payload.get("scope"),
        )
    except WorkflowScopeViolation:
        return _error(
            request,
            "WORKFLOW_SCOPE_NOT_FOUND",
            "Workflow scope contains an unavailable resource.",
            404,
        )
    run_id = new_id("run")
    run = WorkflowRun(
        run_id=run_id,
        workflow=workflow,
        status="queued",
        progress=0.0,
        access_scope=access_scope,
        scope_digest=scope_binding.scope_digest,
        request_payload=payload,
        details={
            "stage": "queued",
            "queue": {
                "attempt": 0,
                "max_attempts": 4,
                "job_id": run_id,
                "delivery_status": "pending",
                "enqueue_attempt": 0,
            },
        },
    )
    db.add(run)
    enqueue_submitted_workflow(db, run)
    return _json(
        {
            "run_id": run.run_id,
            "workflow": workflow,
            "status": run.status,
            "scope_binding": scope_binding.model_dump(mode="json"),
        },
        request,
        202,
    )


@router.post(
    "/evidence/packs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkflowSubmissionResponse,
)
def build_pack(body: EvidencePackRequest, request: Request, db: Session = Depends(get_db)):
    return _submit_workflow("evidence_pack", body.model_dump(), request, db)


@router.post(
    "/citations/ground",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkflowSubmissionResponse,
)
def ground_citations(body: CitationGroundRequest, request: Request, db: Session = Depends(get_db)):
    return _submit_workflow("citation_grounding", body.model_dump(), request, db)


@router.get("/workflows/{run_id}", response_model=WorkflowRunResponse)
def get_workflow(run_id: str, request: Request, db: Session = Depends(get_db)):
    run = db.get(WorkflowRun, run_id)
    if run is None:
        return _error(request, "WORKFLOW_NOT_FOUND", "Workflow run not found.", 404, run_id=run_id)
    try:
        validate_workflow_caller_scope(run, _access_scope(request))
        return _json(_workflow(run), request)
    except WorkflowScopeViolation:
        return _error(request, "WORKFLOW_NOT_FOUND", "Workflow run not found.", 404, run_id=run_id)
