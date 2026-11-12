from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class EvidenceEmbedding(Base):
    __tablename__ = "evidence_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "representation_id",
            "embedding_model",
            name="uq_evidence_embeddings_representation_model",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    representation_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_representations.representation_id"), index=True
    )
    embedding_model: Mapped[str] = mapped_column(String(100))
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding_dim: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), default="success")
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    representation = relationship("EvidenceRepresentation", back_populates="embeddings")
