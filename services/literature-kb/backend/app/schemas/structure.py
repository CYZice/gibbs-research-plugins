from pydantic import BaseModel, Field


class PaperStructureEnrichResponse(BaseModel):
    paper_id: str
    tei_path: str
    references_count: int
    citation_contexts_count: int


class PaperReferenceResponse(BaseModel):
    reference_id: str
    paper_id: str
    ordinal: int
    raw_text: str | None
    title: str | None
    authors: list[dict] | None
    year: int | None
    venue: str | None
    doi: str | None
    url: str | None
    confidence: float | None
    metadata: dict | None


class PaperReferenceListResponse(BaseModel):
    paper_id: str
    references: list[PaperReferenceResponse]


class CitationContextResponse(BaseModel):
    context_id: str
    paper_id: str
    reference_id: str | None
    evidence_id: str | None
    page: int | None
    section: str | None
    marker: str | None
    context_text: str
    metadata: dict | None


class CitationContextListResponse(BaseModel):
    paper_id: str
    citation_contexts: list[CitationContextResponse]


class MultimodalEnrichResponse(BaseModel):
    paper_id: str
    enriched_count: int
    reembedded: bool = False
    processed_count: int = 0
    changed_representation_ids: list[str] = Field(default_factory=list)
    remaining_count: int = 0
    failed_count: int = 0
