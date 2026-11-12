from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Paper(Base):
    __tablename__ = "papers"

    paper_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    year: Mapped[int | None] = mapped_column(Integer)
    publication_date: Mapped[date | None] = mapped_column(Date)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(100), index=True)
    venue: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    source_provenance: Mapped[str | None] = mapped_column(String(50), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    pdf_storage_key: Mapped[str | None] = mapped_column(Text)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    publication_type: Mapped[str | None] = mapped_column(String(50), index=True)
    language: Mapped[str | None] = mapped_column(String(20), index=True)
    keywords: Mapped[list[str] | None] = mapped_column(JSONB)
    license: Mapped[str | None] = mapped_column(String(255))
    open_access: Mapped[bool | None] = mapped_column(Boolean)
    parse_version: Mapped[str | None] = mapped_column(String(100))
    parse_status: Mapped[str] = mapped_column(String(50), default="not_started", index=True)
    evidence_status: Mapped[str] = mapped_column(String(50), default="not_started", index=True)
    embedding_status: Mapped[str] = mapped_column(String(50), default="not_started", index=True)
    multimodal_status: Mapped[str] = mapped_column(String(50), default="not_started", index=True)
    index_status: Mapped[str] = mapped_column(String(50), default="not_started", index=True)
    paper_quality_score: Mapped[float | None] = mapped_column(Float)
    paper_quality_signals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metadata_provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    evidence_units = relationship("EvidenceUnit", back_populates="paper")
    tasks = relationship("PaperTask", back_populates="paper")
