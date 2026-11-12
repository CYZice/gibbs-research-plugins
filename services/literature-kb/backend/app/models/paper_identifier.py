from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaperIdentifier(Base):
    __tablename__ = "paper_identifiers"
    __table_args__ = (UniqueConstraint("identifier_type", "normalized_value", name="uq_paper_identifiers_type_value"),)

    identifier_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.paper_id"), index=True)
    identifier_type: Mapped[str] = mapped_column(String(32))
    normalized_value: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
