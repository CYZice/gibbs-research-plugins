from pydantic import BaseModel


class PaperIngestResponse(BaseModel):
    paper_id: str
    task_id: str
    status: str


class PaperDetailResponse(BaseModel):
    paper_id: str
    title: str | None
    authors: list[dict] | None
    year: int | None
    doi: str | None
    arxiv_id: str | None
    abstract: str | None
    pdf_path: str | None
    evidence_count: int


class PaperParseResponse(BaseModel):
    paper_id: str
    task_id: str
    status: str
    stage: str | None
    mineru_batch_id: str | None = None
    mineru_state: str | None = None
    parsed_dir: str | None = None
    error_message: str | None = None
