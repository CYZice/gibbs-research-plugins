from pydantic import BaseModel


class ReferenceFormatRequest(BaseModel):
    evidence_ids: list[str]
    style: str = "gbt7714"


class ReferenceFormatResponse(BaseModel):
    references: list[str]
