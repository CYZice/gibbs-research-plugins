from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictHarvestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


HarvestLevel = Literal["domain", "field", "subfield", "topic"]


class HarvestScope(StrictHarvestModel):
    level: HarvestLevel
    openalex_id: str = Field(min_length=1, max_length=100)
    include_descendants: bool = True
    match_mode: Literal["topics"] = "topics"


class HarvestFilters(StrictHarvestModel):
    year_from: int | None = Field(default=None, ge=1800, le=2200)
    year_to: int | None = Field(default=None, ge=1800, le=2200)
    types: list[str] = Field(default_factory=lambda: ["article", "review"], max_length=20)
    languages: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_years(self):
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must be less than or equal to year_to")
        return self


class HarvestEstimateRequest(StrictHarvestModel):
    scope: HarvestScope
    filters: HarvestFilters = Field(default_factory=HarvestFilters)


class HarvestCreateRequest(HarvestEstimateRequest):
    name: str = Field(min_length=1, max_length=200)
    billing_mode: Literal["free_credit_only"] = "free_credit_only"
    ingest_mode: Literal["metadata_only", "stage_only"] = "stage_only"


class HarvestImportRequest(StrictHarvestModel):
    selection: Literal["all_staged", "items"] = "all_staged"
    item_ids: list[str] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.selection == "items" and not self.item_ids:
            raise ValueError("item_ids is required when selection=items")
        return self


class HarvestContentRequest(StrictHarvestModel):
    selection: Literal["all_eligible", "items"] = "all_eligible"
    item_ids: list[str] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.selection == "items" and not self.item_ids:
            raise ValueError("item_ids is required when selection=items")
        return self


class HarvestContentExecuteRequest(HarvestContentRequest):
    confirm: Literal[True]


class HarvestFreeDownloadRequest(StrictHarvestModel):
    selection: Literal["all_free_candidates", "items"] = "all_free_candidates"
    item_ids: list[str] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.selection == "items" and not self.item_ids:
            raise ValueError("item_ids is required when selection=items")
        return self


class HarvestFreeDownloadExecuteRequest(HarvestFreeDownloadRequest):
    confirm: Literal[True]


class HarvestCredentialCreate(StrictHarvestModel):
    alias: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=8, max_length=500)
    weight: float = Field(default=1.0, gt=0, le=100)


class HarvestTaxonomyItem(BaseModel):
    level: HarvestLevel
    id: str
    display_name: str
    description: str | None = None
    works_count: int = 0
    domain: dict | None = None
    field: dict | None = None
    subfield: dict | None = None


class HarvestJobSummary(BaseModel):
    job_id: str
    name: str
    status: str
    scope: dict
    filters: dict
    billing_mode: str
    ingest_mode: str
    estimated_items: int | None = None
    enumerated_items: int
    staged_items: int
    no_source_items: int
    failed_items: int
    registered_items: int
    enumeration_complete: bool
    next_retry_at: datetime | None = None
    next_poll_after_seconds: int
    created_at: datetime
    updated_at: datetime
