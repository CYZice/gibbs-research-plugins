from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StagedArtifact(Base):
    __tablename__ = "staged_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="local"
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    metadata_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="application/pdf"
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="available", index=True
    )
    paper_id: Mapped[str | None] = mapped_column(
        ForeignKey("papers.paper_id"), index=True
    )
    metadata_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
