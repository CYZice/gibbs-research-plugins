from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.citation_context import CitationContext
from app.models.evidence_unit import EvidenceUnit
from app.models.paper import Paper
from app.models.paper_reference import PaperReference
from app.schemas.references import ReferenceFormatRequest, ReferenceFormatResponse


class ReferenceFormatError(Exception):
    pass


class ReferenceFormatService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def format(self, request: ReferenceFormatRequest) -> ReferenceFormatResponse:
        style = request.style.lower()
        if style not in {"gbt7714", "apa", "ieee"}:
            raise ReferenceFormatError("style must be one of: gbt7714, apa, ieee.")

        evidence = self.db.scalars(
            select(EvidenceUnit).where(EvidenceUnit.evidence_id.in_(request.evidence_ids))
        ).all()
        items = []
        seen = set()
        for item in evidence:
            reference = self._structured_reference(item.evidence_id)
            key = ("reference", reference.reference_id) if reference else ("paper", item.paper_id)
            if key in seen:
                continue
            seen.add(key)
            if reference is not None:
                items.append(reference)
                continue
            paper = self.db.get(Paper, item.paper_id)
            if paper is not None:
                items.append(paper)

        rows = [self._format_item(item, style, index) for index, item in enumerate(items, 1)]
        return ReferenceFormatResponse(references=rows)

    def structured_references(self, paper_id: str) -> list[PaperReference]:
        return self.db.scalars(
            select(PaperReference)
            .where(PaperReference.paper_id == paper_id)
            .order_by(PaperReference.ordinal)
        ).all()

    def _structured_reference(self, evidence_id: str) -> PaperReference | None:
        try:
            context = self.db.scalar(
                select(CitationContext)
                .where(CitationContext.evidence_id == evidence_id)
                .limit(1)
            )
            if context is None or context.reference_id is None:
                return None
            return self.db.get(PaperReference, context.reference_id)
        except Exception:
            return None

    def _format_item(self, item: Paper | PaperReference, style: str, index: int) -> str:
        raw_text = item.raw_text if isinstance(item, PaperReference) else None
        authors = self._authors(item.authors)
        year = str(item.year) if item.year else None
        title = item.title
        if raw_text and not (authors and year and title):
            return f"[{index}] {raw_text}" if style in {"gbt7714", "ieee"} else raw_text
        year = year or "n.d."
        title = title or "Untitled"
        venue = f". {item.venue}" if item.venue else ""
        doi = f". DOI: {item.doi}" if item.doi else ""
        if style == "apa":
            return f"{authors or 'Unknown author'} ({year}). {title}{venue}{doi}."
        if style == "ieee":
            return f"[{index}] {authors or 'Unknown author'}, \"{title}\"{venue}, {year}{doi}."
        return f"[{index}] {authors or '佚名'}. {title}{venue}, {year}{doi}."

    def _authors(self, authors: list[dict] | None) -> str:
        names = [str(item.get("name")) for item in authors or [] if item.get("name")]
        return ", ".join(names)
