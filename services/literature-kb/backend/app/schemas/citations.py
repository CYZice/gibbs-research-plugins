from pydantic import BaseModel


class CitationResolveResponse(BaseModel):
    evidence_id: str
    paper_id: str
    pdf_path: str | None
    pdf_url: str | None
    page: int | None
    page_index: int | None
    bbox: list[float] | None
    bbox_format: str | None
    bbox_normalized: list[float] | None
    page_size: list[float] | None
    quote: str
