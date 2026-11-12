from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class EvidenceRepresentation(Base):
    __tablename__ = "evidence_representations"

    representation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence_units.evidence_id"), index=True)
    repr_type: Mapped[str] = mapped_column(String(50), index=True)
    text: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[object] = mapped_column(TSVECTOR, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(100))
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(50), default="success", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    indexable: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    representation_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    evidence = relationship("EvidenceUnit", back_populates="representations")
    embeddings = relationship("EvidenceEmbedding", back_populates="representation")
