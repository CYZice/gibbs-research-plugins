from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CitationContext(Base):
    __tablename__ = "citation_contexts"

    context_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.paper_id"), index=True)
    reference_id: Mapped[str | None] = mapped_column(ForeignKey("paper_references.reference_id"), index=True)
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("evidence_units.evidence_id"))
    page: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(Text)
    section_type: Mapped[str | None] = mapped_column(String(50))
    marker: Mapped[str | None] = mapped_column(String(100))
    quote: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
