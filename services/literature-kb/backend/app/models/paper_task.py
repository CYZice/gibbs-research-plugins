from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PaperTask(Base):
    __tablename__ = "paper_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_id: Mapped[str | None] = mapped_column(ForeignKey("papers.paper_id"), index=True)
    operation: Mapped[str] = mapped_column(String(50), index=True)
    provider: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage: Mapped[str | None] = mapped_column(String(50), index=True)
    last_completed_stage: Mapped[str | None] = mapped_column(String(50))
    error_code: Mapped[str | None] = mapped_column(String(64), index=True)
    error_domain: Mapped[str | None] = mapped_column(String(20), index=True)
    attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(default=4, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    root_task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_batches.batch_id"), index=True
    )
    batch_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_batch_items.item_id"), index=True
    )
    queue_name: Mapped[str | None] = mapped_column(String(100))
    delivery_status: Mapped[str | None] = mapped_column(String(20), index=True)
    delivery_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivery_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action: Mapped[str | None] = mapped_column(String(20))
    run_token: Mapped[str | None] = mapped_column(String(64), index=True)
    run_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(200))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pipeline_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    pipeline_generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    output_fingerprint: Mapped[str | None] = mapped_column(String(64))

    paper = relationship("Paper", back_populates="tasks")
