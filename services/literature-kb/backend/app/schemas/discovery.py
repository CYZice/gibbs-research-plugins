from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AccessClass = Literal[
    "commercial_allowed",
    "noncommercial_only",
    "open_license_other",
    "unknown",
    "restricted",
]


class StrictDiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoverySourceInfo(BaseModel):
    source: str
    role: Literal["search_and_resolve", "doi_resolver"]
    configured: bool
    requires_key: bool
    notes: str


class DiscoverySourcesResponse(BaseModel):
    items: list[DiscoverySourceInfo]


class DiscoveryDownloadCandidate(BaseModel):
    provider: str
    url: str
    landing_page_url: str | None = None
    version: str | None = None
    license: str | None = None
    access: AccessClass
    is_oa: bool
    host_type: str | None = None
    source_name: str | None = None


class DiscoveryPaper(BaseModel):
    source: str = "openalex"
    external_id: str
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    title: str | None = None
    authors: list[dict[str, str | None]] = Field(default_factory=list)
    year: int | None = None
    publication_date: date | None = None
    publication_type: str | None = None
    language: str | None = None
    venue: str | None = None
    abstract: str | None = None
    cited_by_count: int = 0
    open_access: bool | None = None
    oa_status: str | None = None
    landing_page_url: str | None = None
    download_candidates: list[DiscoveryDownloadCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DiscoverySearchRequest(StrictDiscoveryModel):
    query: str = Field(min_length=1, max_length=500)
    source: str = "openalex"
    year_from: int | None = Field(default=None, ge=1800, le=2200)
    year_to: int | None = Field(default=None, ge=1800, le=2200)
    open_access_only: bool = True
    publication_types: list[str] = Field(default_factory=lambda: ["article"], max_length=10)
    page_size: int = Field(default=25, ge=1, le=100)
    cursor: str = "*"

    @model_validator(mode="after")
    def validate_years(self):
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must be less than or equal to year_to")
        return self


class DiscoverySearchResponse(BaseModel):
    source: str = "openalex"
    items: list[DiscoveryPaper]
    total: int
    next_cursor: str | None = None


class DiscoveryResolveRequest(StrictDiscoveryModel):
    source: str = "openalex"
    external_id: str | None = Field(default=None, min_length=1, max_length=255)
    openalex_id: str | None = Field(default=None, pattern=r"^W\d+$")
    doi: str | None = Field(default=None, min_length=3, max_length=255)

    @model_validator(mode="after")
    def require_one_identifier(self):
        if sum(bool(value) for value in (self.external_id, self.openalex_id, self.doi)) != 1:
            raise ValueError("Provide exactly one of external_id, openalex_id or doi")
        if self.openalex_id and self.source != "openalex":
            raise ValueError("openalex_id requires source=openalex")
        return self


class DiscoveryDownloadRequest(DiscoveryResolveRequest):
    allow_unknown_license: bool = Field(
        default=False,
        deprecated=True,
        description="Retained for compatibility; license metadata does not gate downloads.",
    )


class DiscoveryDownloadResponse(BaseModel):
    external_id: str
    doi: str | None = None
    storage_key: str
    metadata_key: str
    sha256: str
    size_bytes: int
    source_provider: str
    source_url: str
    license: str | None = None
    access: AccessClass
    duplicate: bool = False


# Compatibility alias for clients importing the pre-federation schema name.
LocalPdfDownloadResponse = DiscoveryDownloadResponse
