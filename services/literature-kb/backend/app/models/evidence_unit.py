from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class EvidenceUnit(Base):
    __tablename__ = "evidence_units"

    evidence_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.paper_id"), index=True)
    page: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(Text)
    section_type: Mapped[str | None] = mapped_column(String(50), index=True)
    modality: Mapped[str] = mapped_column(String(50), index=True)
    block_type: Mapped[str | None] = mapped_column(String(50), index=True)
    block_order: Mapped[int | None] = mapped_column(Integer)
    exclude_from_retrieval: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_content: Mapped[str] = mapped_column(Text)
    bbox: Mapped[list[float] | None] = mapped_column(JSONB)
    coordinate_space: Mapped[str | None] = mapped_column(String(32), default="normalized")
    asset_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    parse_source: Mapped[str | None] = mapped_column(String(50))
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    paper = relationship("Paper", back_populates="evidence_units")
    representations = relationship("EvidenceRepresentation", back_populates="evidence")
