from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models.paper_access_grant import PaperAccessGrant, SHARED_SCOPE


def normalize_scope(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) > 255 or any(character.isspace() for character in normalized):
        raise ValueError("Literature access scope is invalid.")
    return normalized


def visible_paper_clause(paper_id, scope_key: str | None):
    if scope_key is None:
        return True
    return exists(
        select(PaperAccessGrant.grant_id).where(
            PaperAccessGrant.paper_id == paper_id,
            PaperAccessGrant.scope_key.in_((SHARED_SCOPE, scope_key)),
        )
    )


def can_access_paper(db: Session, paper_id: str, scope_key: str | None) -> bool:
    if scope_key is None:
        return True
    return bool(
        db.scalar(
            select(PaperAccessGrant.grant_id)
            .where(
                PaperAccessGrant.paper_id == paper_id,
                PaperAccessGrant.scope_key.in_((SHARED_SCOPE, scope_key)),
            )
            .limit(1)
        )
    )


def grant_paper_access(db: Session, paper_id: str, scope_key: str | None) -> None:
    normalized = normalize_scope(scope_key) or SHARED_SCOPE
    existing = db.scalar(
        select(PaperAccessGrant.grant_id)
        .where(
            PaperAccessGrant.paper_id == paper_id,
            PaperAccessGrant.scope_key == normalized,
        )
        .limit(1)
    )
    if existing is None:
        db.add(PaperAccessGrant(paper_id=paper_id, scope_key=normalized))
        db.flush()
