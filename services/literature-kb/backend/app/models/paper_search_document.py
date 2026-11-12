from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaperSearchDocument(Base):
    __tablename__ = "paper_search_documents"

    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.paper_id"), primary_key=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[object] = mapped_column(TSVECTOR, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    search_index_version: Mapped[str] = mapped_column(Text, nullable=False)
    search_tier: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
