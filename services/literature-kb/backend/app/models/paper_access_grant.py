from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


SHARED_SCOPE = "shared"


class PaperAccessGrant(Base):
    __tablename__ = "paper_access_grants"
    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "scope_key",
            name="uq_paper_access_grants_paper_scope",
        ),
    )

    grant_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("papers.paper_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = ["PaperAccessGrant", "SHARED_SCOPE"]
