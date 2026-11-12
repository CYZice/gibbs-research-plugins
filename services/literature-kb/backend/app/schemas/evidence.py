from typing import Literal

from pydantic import BaseModel, Field


class EvidenceBuildResponse(BaseModel):
    paper_id: str
    evidence_count: int
    source_file: str


class EvidenceEmbeddingBuildResponse(BaseModel):
    paper_id: str
    embedding_model: str
    embedding_dimension: int
    embedded_count: int
    skipped_count: int
    skipped_unchanged_count: int = 0
    skipped_not_indexable_count: int = 0
    deleted_stale_count: int = 0


class EvidenceFilters(BaseModel):
    paper_ids: list[str] | None = None
    modality: list[str] | None = None
    section: list[str] | None = None


SearchScope = Literal[
    "scientific",
    "references",
    "author_contributions",
    "funding",
    "metadata",
    "supplementary",
    "all",
]
SearchIntent = Literal[
    "auto",
    "general",
    "main_result",
    "method",
    "dataset",
    "metric",
    "limitation",
    "future_work",
    "reference_query",
    "author_contribution_query",
    "funding_query",
    "figure_query",
    "table_query",
    "formula_query",
]


class EvidenceSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)
    filters: EvidenceFilters | None = None
    scope: SearchScope | None = None
    intent: SearchIntent = "auto"


class EvidenceSearchItem(BaseModel):
    evidence_id: str
    representation_id: str
    paper_id: str
    page: int | None
    section: str | None
    modality: str
    block_type: str | None
    content: str
    representation_text: str
    repr_type: str
    score: float
    representation_metadata: dict | None = None
    retrieval_policy: str | None = None
    retrieval_scope: str | None = None
    semantic_role: str | None = None
    score_breakdown: dict | None = None


class EvidenceSearchResponse(BaseModel):
    query: str
    embedding_model: str
    resolved_scope: str | None = None
    resolved_intent: str = "general"
    retrieval_mode: str = "dense"
    evidence: list[EvidenceSearchItem]
    warnings: list[str] = Field(default_factory=list)


class ContextPackRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)
    filters: EvidenceFilters | None = None
    scope: SearchScope | None = None
    intent: SearchIntent = "auto"
    need_summary: bool = True
    need_references: bool = True
    reference_mode: Literal["evidence", "paper"] = "evidence"


class ContextPackItem(BaseModel):
    evidence_id: str
    representation_id: str
    repr_type: str
    citation_key: str
    paper_id: str
    paper_title: str | None
    page: int | None
    section: str | None
    modality: str
    quote: str
    usable_summary: str
    support_type: str
    score: float


class ContextPackReference(BaseModel):
    citation_key: str | None = None
    evidence_id: str | None = None
    citation_keys: list[str] | None = None
    evidence_ids: list[str] | None = None
    paper_id: str
    title: str | None
    authors: list[dict] | None
    year: int | None
    doi: str | None
    venue: str | None = None
    raw_text: str | None = None
    url: str | None = None


class ContextPackResponse(BaseModel):
    query: str
    embedding_model: str
    context_pack: list[ContextPackItem]
    references: list[ContextPackReference]


class EvidenceRepresentationResponse(BaseModel):
    representation_id: str
    evidence_id: str
    repr_type: str
    text: str
    model_name: str | None
    prompt_version: str | None
    status: str
    is_active: bool
    indexable: bool
    error_message: str | None
    metadata: dict | None


class EvidenceUnitResponse(BaseModel):
    evidence_id: str
    paper_id: str
    page: int | None
    section: str | None
    modality: str
    block_type: str | None
    content: str
    block_order: int | None
    exclude_from_retrieval: bool
    render_policy: str
    bbox: list[float] | None
    asset_path: str | None
    parser: str | None
    confidence: float | None
    metadata: dict | None
    active_representation: EvidenceRepresentationResponse | None = None
    representations: list[EvidenceRepresentationResponse] = Field(default_factory=list)


class PaperEvidenceListResponse(BaseModel):
    paper_id: str
    total: int
    limit: int
    offset: int
    evidence: list[EvidenceUnitResponse]


class EvidenceNeighborBlock(BaseModel):
    evidence_id: str
    text: str
    distance: float | int


class EvidenceReferenceSentence(BaseModel):
    evidence_id: str
    sentence: str
    matched_label: str


class EvidenceSpatialNeighbor(BaseModel):
    evidence_id: str
    text: str
    relation: str
    distance: float


class EvidenceContextResponse(BaseModel):
    evidence_id: str
    paper_id: str
    paper_title: str | None = None
    section: str | None = None
    label: str | None = None
    caption: str | None = None
    before_blocks: list[EvidenceNeighborBlock] = Field(default_factory=list)
    after_blocks: list[EvidenceNeighborBlock] = Field(default_factory=list)
    reference_sentences: list[EvidenceReferenceSentence] = Field(default_factory=list)
    spatial_neighbors: list[EvidenceSpatialNeighbor] = Field(default_factory=list)
    context_text: str
    context_hash: str
