from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ResourceAsset(Base):
    __tablename__ = "resource_assets"
    __table_args__ = (UniqueConstraint("paper_id", "evidence_id", "asset_type", "variant", name="uq_resource_assets_evidence_variant"),)

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.paper_id"), index=True)
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("evidence_units.evidence_id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(50), index=True)
    variant: Mapped[str] = mapped_column(String(50), index=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None]
    caption: Mapped[str | None] = mapped_column(Text)
    location: Mapped[dict | None] = mapped_column(JSONB)
    created_from: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
