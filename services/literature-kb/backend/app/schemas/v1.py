from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pagination(BaseModel):
    """Pagination over the endpoint's candidate/result set, not a global relevance count."""

    page: int = Field(description="One-based page number.")
    page_size: int = Field(description="Number of items returned per page.")
    total: int = Field(description="Size of the filtered candidate/result set.")
    total_pages: int
    has_next: bool
    has_prev: bool


class PaperRef(BaseModel):
    paper_id: str
    title: str | None = None
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    venue: str | None = None
    publication_type: str | None = None
    language: str | None = None
    publication_date: date | None = None


class SourceLocation(BaseModel):
    page: int | None = None
    bbox: list[float] | None = None
    coordinate_space: Literal["normalized"] | None = None
    char_start: int | None = None
    char_end: int | None = None
    section_title: str | None = None
    section_type: str | None = None


class AssetRef(BaseModel):
    asset_id: str
    asset_type: str
    mime_type: str | None = None
    caption: str | None = None
    location: SourceLocation | None = None


class ProcessingStatus(BaseModel):
    fulltext: Literal["missing", "local_pdf", "parsed_artifacts", "content_ready"]
    parse: Literal["not_started", "queued", "running", "succeeded", "failed"]
    evidence: Literal["not_started", "building", "ready", "failed"]
    embedding: Literal["not_started", "queued", "building", "ready", "failed"]
    multimodal: Literal["not_started", "partial", "ready", "failed"]


class Capabilities(BaseModel):
    has_fulltext: bool
    can_read_content: bool
    can_search_evidence: bool
    can_download_pdf: bool
    has_assets: bool
    ai_ready: bool


class AssetCounts(BaseModel):
    pdf: int = 0
    figure: int = 0
    table: int = 0
    formula: int = 0
    image: int = 0
    attachment: int = 0


class PaperQuality(BaseModel):
    score: float | None = None
    signals: dict[str, float] = Field(default_factory=dict)


class PaperSummary(PaperRef):
    authors: list[dict] | None = None
    abstract: str | None = None
    keywords: list[str] | None = None
    source_provenance: str | None = None
    source_url: str | None = None
    license: str | None = None
    open_access: bool | None = None
    parse_version: str | None = None
    processing_status: ProcessingStatus
    capabilities: Capabilities
    asset_counts: AssetCounts
    paper_quality: PaperQuality


class SearchRange(StrictModel):
    gte: int | None = None
    lte: int | None = None


class PaperFilters(StrictModel):
    paper_ids: list[str] = Field(
        default_factory=list,
        max_length=5000,
        description="Restrict paper candidates before lexical or semantic retrieval.",
    )
    year: SearchRange | None = None
    venue: list[str] = Field(default_factory=list)
    doi: str | None = None
    language: list[str] = Field(default_factory=list)
    publication_type: list[str] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    source_provenance: list[str] = Field(default_factory=list)
    identifier_type: str | None = None
    identifier_value: str | None = None
    parse_status: list[str] = Field(default_factory=list)
    indexed: bool | None = None
    has_fulltext: bool | None = None
    ai_ready: bool | None = None


class SortSpec(StrictModel):
    field: Literal["relevance", "year", "publication_date", "created_at", "updated_at", "paper_quality.score"] | None = None
    order: Literal["asc", "desc"] = "desc"


class PaperSearchRequest(StrictModel):
    query: str | None = None
    search_mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    filters: PaperFilters = Field(default_factory=PaperFilters)
    sort: SortSpec = Field(default_factory=SortSpec)
    facets: list[Literal["year", "venue", "language", "publication_type", "source_provenance", "has_fulltext", "ai_ready"]] = Field(default_factory=list)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class EvidenceScope(StrictModel):
    """Optional retrieval boundary applied before candidate generation.

    An empty scope searches locally retrievable evidence. When both lists are supplied,
    evidence IDs must belong to the listed papers; otherwise the request is invalid.
    """

    paper_ids: list[str] = Field(
        default_factory=list,
        max_length=5000,
        description="Restrict candidates to these papers before retrieval.",
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        max_length=2000,
        description="Restrict candidates to these known evidence records before retrieval.",
    )


class EvidenceFilters(PaperFilters):
    modality: list[str] = Field(default_factory=list)
    block_type: list[str] = Field(default_factory=list)
    section_type: list[str] = Field(default_factory=list)
    has_assets: bool | None = None


class EvidenceSearchRequest(StrictModel):
    """Search evidence and then page the bounded candidate pool.

    `top_k` bounds candidates after scope and filters; `page` and `page_size` only
    select a page from that pool. Values within one list filter are ORed, while
    different filter fields are ANDed.
    """

    query: str = Field(min_length=1, description="Natural-language retrieval query.")
    scope: EvidenceScope = Field(
        default_factory=EvidenceScope,
        description=(
            "Optional pre-retrieval boundary; omit or leave empty for local ready evidence."
        ),
    )
    filters: EvidenceFilters = Field(default_factory=EvidenceFilters)
    top_k: int = Field(
        default=50, ge=1, le=100, description="Maximum filtered candidate pool size."
    )
    page: int = Field(default=1, ge=1, description="One-based page within the candidate pool.")
    page_size: int = Field(
        default=10, ge=1, le=50, description="Items returned from the candidate pool."
    )


class RepresentationRef(BaseModel):
    representation_id: str
    type: str


class EvidenceContent(BaseModel):
    snippet: str
    quote: str
    quote_language: str | None = None


class EvidenceHit(BaseModel):
    evidence_id: str
    representation: RepresentationRef
    paper: PaperRef
    content: EvidenceContent
    location: SourceLocation | None = None
    modality: str
    block_type: str | None = None
    score: float | None = None
    assets: list[AssetRef] = Field(default_factory=list)


class EvidenceCitation(BaseModel):
    evidence_id: str
    paper: PaperRef
    quote: str
    location: SourceLocation | None = None
    assets: list[AssetRef] = Field(default_factory=list)


class WorkflowClaim(StrictModel):
    claim_id: str | None = None
    text: str = Field(min_length=1, max_length=2000)


class EvidencePackRequest(StrictModel):
    claims: list[WorkflowClaim] = Field(min_length=1, max_length=20)
    scope: EvidenceScope = Field(default_factory=EvidenceScope)
    filters: EvidenceFilters = Field(default_factory=EvidenceFilters)
    top_k_per_claim: int = Field(default=5, ge=1, le=10)


class CitationGroundRequest(StrictModel):
    draft: str = Field(min_length=1, max_length=20000)
    scope: EvidenceScope = Field(default_factory=EvidenceScope)
    filters: EvidenceFilters = Field(default_factory=EvidenceFilters)
    top_k_per_claim: int = Field(default=5, ge=1, le=10)


class OperationRequest(StrictModel):
    force: bool = False
    options: dict = Field(default_factory=dict)


class BibliographyFormatRequest(StrictModel):
    style: str = "gbt7714"
    paper_ids: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_source(self):
        if not self.paper_ids and not self.reference_ids:
            raise ValueError("paper_ids or reference_ids is required")
        return self


class TaskResponse(BaseModel):
    task_id: str
    paper_id: str | None = None
    operation: str
    status: str
    progress: float
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    details: dict = Field(default_factory=dict)


class ApiError(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable explanation of the failure.")
    details: dict = Field(
        default_factory=dict, description="Endpoint-specific, non-sensitive error details."
    )
    request_id: str = Field(
        description="Request correlation ID; include it when reporting failures."
    )


class ErrorResponse(BaseModel):
    error: ApiError


class MetaCatalogField(BaseModel):
    name: str
    type: str
    description: str | None = None
    paper_search_filterable: bool
    evidence_search_filterable: bool
    sortable: bool
    operators: list[str]
    sample_values: list[str | int] | None = None


class MetaCatalogResponse(BaseModel):
    fields: list[MetaCatalogField]
    sort_fields: list[str]
    default_sort: SortSpec
    default_fields: list[str]
    filter_semantics: str


class SearchHighlight(BaseModel):
    field: str
    text: str


class BestEvidenceRef(BaseModel):
    evidence_id: str
    quote: str
    location: SourceLocation | None = None


class PaperSearchHit(PaperSummary):
    score: float | None = None
    matched_fields: list[str] = Field(default_factory=list)
    highlights: list[SearchHighlight] = Field(default_factory=list)
    best_evidence: BestEvidenceRef | None = None


class FacetBucket(BaseModel):
    value: str | int | bool
    count: int


class SearchInfo(BaseModel):
    mode: Literal["lexical", "semantic", "hybrid"]
    index_version: str = "v1"
    semantic_fallback: bool = False
    rerank_applied: bool = False
    rerank_candidates: int = 0
    rerank_degraded_reason: str | None = None


class PaperSearchResponse(BaseModel):
    items: list[PaperSearchHit]
    pagination: Pagination
    facets: dict[str, list[FacetBucket]] = Field(default_factory=dict)
    search_info: SearchInfo | None = None


class LiteratureSearchFilters(StrictModel):
    year_from: int | None = Field(default=None, ge=1800, le=2200)
    year_to: int | None = Field(default=None, ge=1800, le=2200)
    publication_types: list[str] = Field(default_factory=list, max_length=10)
    language: list[str] = Field(default_factory=list, max_length=10)
    open_access_only: bool = False

    @model_validator(mode="after")
    def validate_years(self):
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must be less than or equal to year_to")
        return self


class LiteratureSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    search_mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    sources: list[str] = Field(
        default_factory=lambda: ["local", "openalex"], min_length=1, max_length=10
    )
    filters: LiteratureSearchFilters = Field(default_factory=LiteratureSearchFilters)
    page_size: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None


class LiteratureOrigin(BaseModel):
    source: str
    id: str


class LiteratureMetadata(BaseModel):
    title: str | None = None
    authors: list[dict] = Field(default_factory=list)
    year: int | None = None
    publication_date: date | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    venue: str | None = None
    abstract: str | None = None
    publication_type: str | None = None
    language: str | None = None
    open_access: bool | None = None


class LiteratureRemoteInfo(BaseModel):
    source: str
    external_id: str
    downloadable: bool
    license: str | None = None
    license_policy: Literal["allowed", "confirmation_required", "blocked"]
    version: str | None = None
    priority: int = 0
    landing_page_url: str | None = None


class LiteratureSearchHit(BaseModel):
    identity_key: str
    metadata: LiteratureMetadata
    origins: list[LiteratureOrigin]
    local_paper: PaperSummary | None = None
    remote: LiteratureRemoteInfo | None = None
    remote_options: list[LiteratureRemoteInfo] = Field(default_factory=list)
    availability: Literal[
        "local_ready", "local_processing", "remote_downloadable", "metadata_only"
    ]
    score: float
    possible_duplicate: bool = False


class LiteratureSourceStatus(BaseModel):
    source: str
    status: Literal[
        "ok",
        "cached",
        "stale_cache",
        "timeout",
        "rate_limited",
        "unavailable",
        "unconfigured",
        "unsupported_filter",
    ]
    latency_ms: int
    cache: Literal["hit", "miss", "stale", "unavailable"] = "miss"
    error_code: str | None = None


class LiteratureSearchResponse(BaseModel):
    search_id: str
    items: list[LiteratureSearchHit]
    next_cursor: str | None = None
    partial: bool = False
    source_status: list[LiteratureSourceStatus]


class LiteratureImportOption(StrictModel):
    source: str = Field(min_length=1, max_length=50)
    external_id: str = Field(min_length=1, max_length=255)


class LiteratureImportItem(StrictModel):
    source: str | None = None
    external_id: str | None = Field(default=None, min_length=1, max_length=255)
    options: list[LiteratureImportOption] = Field(default_factory=list, max_length=10)
    preferred_source: str | None = Field(default=None, min_length=1, max_length=50)
    license_acknowledged: bool = Field(
        default=False,
        deprecated=True,
        description="Retained for compatibility; license metadata does not gate downloads.",
    )

    @model_validator(mode="after")
    def validate_sources(self):
        if self.external_id:
            self.source = self.source or "openalex"
            if self.options:
                raise ValueError("Use either source/external_id or options, not both")
            return self
        if not self.options:
            raise ValueError("source/external_id or options is required")
        sources = {item.source for item in self.options}
        if self.preferred_source and self.preferred_source not in sources:
            raise ValueError("preferred_source must identify one of the options")
        return self


class LiteratureImportRequest(StrictModel):
    items: list[LiteratureImportItem] = Field(min_length=1, max_length=50)


class IngestPaperResponse(BaseModel):
    batch_id: str
    item_id: str
    task: TaskResponse


class PaperContentWindow(BaseModel):
    text: str
    char_start: int
    char_end: int
    chars_returned: int
    next_offset: int | None = None
    more: bool


class PaperContentResponse(BaseModel):
    paper: PaperRef
    content: PaperContentWindow


class EvidenceSearchResponse(BaseModel):
    items: list[EvidenceHit]
    pagination: Pagination
    search_info: SearchInfo | None = None


class EvidenceContentWindow(BaseModel):
    text: str
    evidence_char_start: int
    evidence_char_end: int
    window_char_start: int
    window_char_end: int
    more_before: bool
    more_after: bool


class EvidenceContentResponse(BaseModel):
    evidence_id: str
    paper: PaperRef
    content: EvidenceContentWindow
    location: SourceLocation | None = None
    assets: list[AssetRef] = Field(default_factory=list)


class EvidenceSourceResponse(BaseModel):
    citation: EvidenceCitation
    pdf_asset_id: str | None = None


class AssetPageResponse(BaseModel):
    items: list[AssetRef]
    pagination: Pagination


class AssetDetailResponse(BaseModel):
    asset_id: str
    paper: PaperRef
    asset_type: str
    mime_type: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    caption: str | None = None
    location: SourceLocation | None = None
    created_from: str | None = None


class RelationItem(BaseModel):
    relation: Literal["references", "citations"]
    reference_id: str
    paper_id: str
    raw_text: str | None = None
    parsed_title: str | None = None
    parsed_authors: list[str] = Field(default_factory=list)
    parsed_year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    venue: str | None = None
    target_paper: PaperRef | None = None
    order_index: int | None = None


class RelationPageResponse(BaseModel):
    items: list[RelationItem]
    pagination: Pagination


class CitationReference(BaseModel):
    reference_id: str
    parsed_title: str | None = None
    parsed_year: int | None = None
    doi: str | None = None
    target_paper: PaperRef | None = None


class CitationContextContent(BaseModel):
    quote: str
    location: SourceLocation | None = None


class CitationContextItem(BaseModel):
    citation_context_id: str
    paper: PaperRef
    reference: CitationReference | None = None
    context: CitationContextContent


class CitationContextPageResponse(BaseModel):
    items: list[CitationContextItem]
    pagination: Pagination


class BibliographyItem(BaseModel):
    source_type: Literal["paper", "reference"]
    paper_id: str | None = None
    reference_id: str | None = None
    formatted: str


class BibliographyFormatResponse(BaseModel):
    items: list[BibliographyItem]
    style: str


class WorkflowScopeBinding(StrictModel):
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    paper_ids: list[str] = Field(default_factory=list, max_length=5000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=2000)
    scope_validated: bool
    result_validated: bool


class WorkflowSubmissionResponse(BaseModel):
    run_id: str
    workflow: Literal["evidence_pack", "citation_grounding"]
    status: Literal["queued", "retrying"]
    scope_binding: WorkflowScopeBinding


class WorkflowRunResponse(BaseModel):
    run_id: str
    workflow: Literal["evidence_pack", "citation_grounding"]
    status: Literal["queued", "retrying", "running", "succeeded", "failed"]
    progress: float
    scope_binding: WorkflowScopeBinding
    request: dict
    result: dict | None = None
    details: dict | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class HealthResponse(BaseModel):
    status: str


class ImportBatchResponse(BaseModel):
    batch_id: str
    source: str
    status: str
    total_items: int
    completed_items: int
    failed_items: int
    items: list["ImportBatchItemResponse"] = Field(default_factory=list)
    next_cursor: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ImportBatchItemResponse(BaseModel):
    item_id: str
    source_key: str
    paper_id: str | None = None
    staged_artifact_id: str | None = None
    task_id: str | None = None
    status: str
    current_stage: str | None = None
    attempts: int
    error_message: str | None = None
    details: dict = Field(default_factory=dict)


class ImportBatchCreateRequest(StrictModel):
    source: str = Field(default="remote:auto", min_length=1, max_length=100)
    priority: int = Field(default=50, ge=0, le=100)


class ImportBatchAppendRequest(StrictModel):
    items: list[LiteratureImportItem] = Field(min_length=1, max_length=5000)


class ImportBatchAppendResponse(BaseModel):
    batch_id: str
    accepted_items: int
    total_items: int
    status: str


class ImportBatchItemsPage(BaseModel):
    items: list[ImportBatchItemResponse]
    next_cursor: str | None = None


class ImportBatchRetryResponse(BaseModel):
    batch_id: str
    retried_items: int
    status: str


class SystemHealthComponent(BaseModel):
    name: str
    status: str
    latency_ms: int
    detail: str | int | bool | dict[str, Any] | None = None


class SystemHealthResponse(BaseModel):
    status: str
    checked_at: str
    components: list[SystemHealthComponent]
    capabilities: list[str]
