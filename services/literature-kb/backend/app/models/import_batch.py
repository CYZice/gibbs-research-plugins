from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
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


class ImportBatch(Base):
    __tablename__ = "import_batches"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    intake_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manifest_storage_key: Mapped[str | None] = mapped_column(Text)
    expansion_cursor: Mapped[str | None] = mapped_column(Text)
    expanded_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expansion_status: Mapped[str | None] = mapped_column(String(32))
    expansion_error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ImportBatchItem(Base):
    __tablename__ = "import_batch_items"
    __table_args__ = (UniqueConstraint("batch_id", "source_key", name="uq_import_batch_items_source"),)

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.batch_id"), index=True)
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    paper_id: Mapped[str | None] = mapped_column(ForeignKey("papers.paper_id"), index=True)
    staged_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("staged_artifacts.artifact_id"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pipeline_task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    current_stage: Mapped[str | None] = mapped_column(String(50), index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duplicate_of_paper_id: Mapped[str | None] = mapped_column(
        ForeignKey("papers.paper_id")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
