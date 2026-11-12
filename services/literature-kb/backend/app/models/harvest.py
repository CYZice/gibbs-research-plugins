from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class HarvestJob(Base):
    __tablename__ = "harvest_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="openalex")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="enumerating", index=True
    )
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    taxonomy_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    billing_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="free_credit_only"
    )
    ingest_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="stage_only"
    )
    next_cursor: Mapped[str | None] = mapped_column(Text, default="*")
    enumeration_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    estimated_items: Mapped[int | None] = mapped_column(Integer)
    enumerated_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    staged_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_source_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    registered_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CorpusDocument(Base):
    __tablename__ = "corpus_documents"

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="openalex")
    external_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    publication_type: Mapped[str | None] = mapped_column(String(50), index=True)
    language: Mapped[str | None] = mapped_column(String(20), index=True)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    candidate_urls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    content_url: Mapped[str | None] = mapped_column(Text)
    pdf_storage_key: Mapped[str | None] = mapped_column(Text)
    metadata_storage_key: Mapped[str | None] = mapped_column(Text)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    pdf_size_bytes: Mapped[int | None] = mapped_column(Integer)
    download_source: Mapped[str | None] = mapped_column(String(50))
    source_url: Mapped[str | None] = mapped_column(Text)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_paper_id: Mapped[str | None] = mapped_column(
        ForeignKey("papers.paper_id"), index=True
    )
    staged_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("staged_artifacts.artifact_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HarvestJobItem(Base):
    __tablename__ = "harvest_job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "document_id", name="uq_harvest_job_document"),
    )

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("harvest_jobs.job_id"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("corpus_documents.document_id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_source: Mapped[str | None] = mapped_column(String(50))
    credential_id: Mapped[str | None] = mapped_column(String(64), index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    import_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_batches.batch_id"), index=True
    )
    import_batch_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_batch_items.item_id"), index=True
    )
    registered_paper_id: Mapped[str | None] = mapped_column(
        ForeignKey("papers.paper_id"), index=True
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HarvestCredential(Base):
    __tablename__ = "harvest_credentials"
    __table_args__ = (
        UniqueConstraint("provider", "alias", name="uq_harvest_credential_alias"),
    )

    credential_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="openalex")
    alias: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    key_suffix: Mapped[str] = mapped_column(String(8), nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", index=True
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    daily_budget_usd: Mapped[float | None] = mapped_column(Float)
    daily_used_usd: Mapped[float | None] = mapped_column(Float)
    daily_remaining_usd: Mapped[float | None] = mapped_column(Float)
    prepaid_remaining_usd: Mapped[float | None] = mapped_column(Float)
    content_cost_usd: Mapped[float | None] = mapped_column(Float)
    resets_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
