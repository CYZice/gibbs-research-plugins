import re
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence_representation import EvidenceRepresentation
from app.models.evidence_unit import EvidenceUnit
from app.models.paper import Paper

FIGURE_PATTERNS = [
    re.compile(r"\bFig\.?\s*(\d+[a-zA-Z]?(?:\([a-z]\))?)", re.I),
    re.compile(r"\bFigure\.?\s*(\d+[a-zA-Z]?(?:\([a-z]\))?)", re.I),
    re.compile(r"图\s*(\d+[a-zA-Z]?(?:\([a-z]\))?)", re.I),
]
TABLE_PATTERNS = [
    re.compile(r"\bTable\s*([IVXLC]+|\d+)", re.I),
    re.compile(r"\bTab\.?\s*(\d+)", re.I),
    re.compile(r"表\s*(\d+)", re.I),
]
EQUATION_PATTERNS = [
    re.compile(r"\bEq\.?\s*\(?(\d+)\)?", re.I),
    re.compile(r"\bEquation\s*\(?(\d+)\)?", re.I),
    re.compile(r"公式\s*\(?(\d+)\)?", re.I),
    re.compile(r"式\s*\(?(\d+)\)?", re.I),
    re.compile(r"\((\d+)\)"),
]
CONTEXT_BLOCK_TYPES = {
    "title",
    "section_heading",
    "paragraph",
    "list",
    "table",
    "equation_interline",
}


class EvidenceContextBuilder:
    def __init__(self, db: Session) -> None:
        self.db = db

    def build_for_evidence(
        self, evidence_id: str, before_k: int = 2, after_k: int = 2
    ) -> dict[str, Any]:
        evidence = self.db.get(EvidenceUnit, evidence_id)
        if evidence is None:
            raise ValueError("Evidence not found.")
        paper = self.db.get(Paper, evidence.paper_id)
        all_evidence = self.db.scalars(
            select(EvidenceUnit)
            .where(EvidenceUnit.paper_id == evidence.paper_id)
            .order_by(EvidenceUnit.block_order, EvidenceUnit.evidence_id)
        ).all()
        representations = self.db.scalars(
            select(EvidenceRepresentation).where(
                EvidenceRepresentation.evidence_id.in_(
                    [item.evidence_id for item in all_evidence]
                )
            )
        ).all()
        active_text = {
            item.evidence_id: item.text
            for item in representations
            if item.is_active and item.status == "success"
        }
        weak_text = {
            item.evidence_id: item.text
            for item in representations
            if item.repr_type == "weak" and item.status == "success"
        }

        caption = weak_text.get(evidence.evidence_id) or evidence.raw_content
        label = self.extract_label(evidence.modality, caption)
        before_blocks, after_blocks = self.neighbor_blocks(
            evidence, all_evidence, active_text, before_k=before_k, after_k=after_k
        )
        reference_sentences = self.reference_sentences(
            evidence, label, all_evidence, active_text
        )
        spatial_neighbors = self.spatial_neighbors(evidence, all_evidence, active_text)
        context_text = self.context_text(
            evidence=evidence,
            paper=paper,
            caption=caption,
            before_blocks=before_blocks,
            after_blocks=after_blocks,
            reference_sentences=reference_sentences,
            spatial_neighbors=spatial_neighbors,
        )

        return {
            "evidence_id": evidence.evidence_id,
            "paper_id": evidence.paper_id,
            "paper_title": paper.title if paper is not None else None,
            "section": evidence.section_title,
            "label": label,
            "caption": caption,
            "before_blocks": before_blocks,
            "after_blocks": after_blocks,
            "reference_sentences": reference_sentences,
            "spatial_neighbors": spatial_neighbors,
            "context_text": context_text,
            "context_hash": sha256(context_text.encode("utf-8")).hexdigest(),
        }

    def build_for_paper(self, paper_id: str) -> dict[str, Any]:
        evidence_ids = self.db.scalars(
            select(EvidenceUnit.evidence_id).where(
                EvidenceUnit.paper_id == paper_id,
                EvidenceUnit.modality.in_(["figure", "table", "formula"]),
            )
        ).all()
        contexts = [self.build_for_evidence(evidence_id) for evidence_id in evidence_ids]
        return {
            "paper_id": paper_id,
            "processed": len(contexts),
            "contexts": contexts,
        }

    def extract_label(self, modality: str, text: str | None) -> str | None:
        source = text or ""
        if modality == "figure":
            for pattern in FIGURE_PATTERNS:
                match = pattern.search(source)
                if match:
                    return f"Figure {match.group(1)}"
        if modality == "table":
            for pattern in TABLE_PATTERNS:
                match = pattern.search(source)
                if match:
                    value = match.group(1)
                    if value.isalpha():
                        value = str(self.roman_to_int(value) or value)
                    return f"Table {value}"
        if modality == "formula":
            for pattern in EQUATION_PATTERNS:
                match = pattern.search(source)
                if match:
                    return f"Equation {match.group(1)}"
        return None

    def neighbor_blocks(
        self,
        evidence: EvidenceUnit,
        all_evidence: list[EvidenceUnit],
        active_text: dict[str, str],
        before_k: int = 2,
        after_k: int = 2,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if evidence.block_order is None:
            return [], []
        candidates = [
            item
            for item in all_evidence
            if item.evidence_id != evidence.evidence_id
            and not item.exclude_from_retrieval
            and item.block_type in CONTEXT_BLOCK_TYPES
            and item.block_order is not None
            and (not evidence.section_title or not item.section_title or item.section_title == evidence.section_title)
        ]
        before = sorted(
            [item for item in candidates if item.block_order < evidence.block_order],
            key=lambda item: item.block_order or 0,
            reverse=True,
        )[:before_k]
        after = sorted(
            [item for item in candidates if item.block_order > evidence.block_order],
            key=lambda item: item.block_order or 0,
        )[:after_k]
        return (
            [
                self.block_payload(item, active_text, evidence.block_order)
                for item in sorted(before, key=lambda item: item.block_order or 0)
            ],
            [self.block_payload(item, active_text, evidence.block_order) for item in after],
        )

    def reference_sentences(
        self,
        evidence: EvidenceUnit,
        label: str | None,
        all_evidence: list[EvidenceUnit],
        active_text: dict[str, str],
    ) -> list[dict[str, Any]]:
        if not label:
            return []
        variants = self.label_variants(label)
        matches = []
        for item in all_evidence:
            if item.evidence_id == evidence.evidence_id or item.exclude_from_retrieval:
                continue
            if item.block_type not in CONTEXT_BLOCK_TYPES:
                continue
            text = active_text.get(item.evidence_id) or item.raw_content or ""
            sentences = re.split(r"(?<=[。！？.!?])\s+", text)
            for sentence in [*sentences, text]:
                lowered = sentence.lower()
                if any(variant in lowered for variant in variants):
                    matches.append(
                        {
                            "evidence_id": item.evidence_id,
                            "sentence": sentence.strip()[:500],
                            "matched_label": label,
                        }
                    )
                    break
        return matches[:5]

    def spatial_neighbors(
        self,
        evidence: EvidenceUnit,
        all_evidence: list[EvidenceUnit],
        active_text: dict[str, str],
    ) -> list[dict[str, Any]]:
        if evidence.page is None or not self.valid_bbox(evidence.bbox):
            return []
        above = []
        below = []
        for item in all_evidence:
            if (
                item.evidence_id == evidence.evidence_id
                or item.page != evidence.page
                or item.exclude_from_retrieval
                or item.block_type not in CONTEXT_BLOCK_TYPES
                or not self.valid_bbox(item.bbox)
                or self.horizontal_overlap(evidence.bbox, item.bbox) < 0.2
            ):
                continue
            if item.bbox[3] <= evidence.bbox[1]:
                above.append((evidence.bbox[1] - item.bbox[3], item))
            elif item.bbox[1] >= evidence.bbox[3]:
                below.append((item.bbox[1] - evidence.bbox[3], item))
        rows = []
        for distance, item in sorted(above, key=lambda row: row[0])[:2]:
            rows.append(self.spatial_payload(item, active_text, "above", distance))
        for distance, item in sorted(below, key=lambda row: row[0])[:2]:
            rows.append(self.spatial_payload(item, active_text, "below", distance))
        return rows

    def context_text(
        self,
        evidence: EvidenceUnit,
        paper: Paper | None,
        caption: str,
        before_blocks: list[dict[str, Any]],
        after_blocks: list[dict[str, Any]],
        reference_sentences: list[dict[str, Any]],
        spatial_neighbors: list[dict[str, Any]],
    ) -> str:
        limit = 1000 if evidence.modality == "formula" else 1500
        parts = [
            f"Paper title: {paper.title}" if paper is not None and paper.title else "",
            f"Section: {evidence.section_title}" if evidence.section_title else "",
            f"Caption/content: {caption}" if caption else "",
        ]
        parts.extend(f"Reference sentence: {item['sentence']}" for item in reference_sentences)
        parts.extend(f"Before: {item['text']}" for item in before_blocks)
        parts.extend(f"After: {item['text']}" for item in after_blocks)
        parts.extend(
            f"Spatial {item['relation']}: {item['text']}" for item in spatial_neighbors
        )

        seen = set()
        output = []
        for part in parts:
            text = re.sub(r"\s+", " ", part.strip())
            if not text or text in seen:
                continue
            seen.add(text)
            if sum(len(item) for item in output) + len(text) + 1 > limit:
                remaining = limit - sum(len(item) for item in output) - len(output)
                if remaining > 40:
                    output.append(text[:remaining].rstrip())
                break
            output.append(text)
        return "\n".join(output)

    def block_payload(
        self,
        item: EvidenceUnit,
        active_text: dict[str, str],
        origin_order: int,
    ) -> dict[str, Any]:
        text = (active_text.get(item.evidence_id) or item.raw_content or "").strip()
        return {
            "evidence_id": item.evidence_id,
            "text": re.sub(r"\s+", " ", text)[:800],
            "distance": abs((item.block_order or origin_order) - origin_order),
        }

    def spatial_payload(
        self,
        item: EvidenceUnit,
        active_text: dict[str, str],
        relation: str,
        distance: float,
    ) -> dict[str, Any]:
        text = (active_text.get(item.evidence_id) or item.raw_content or "").strip()
        return {
            "evidence_id": item.evidence_id,
            "text": re.sub(r"\s+", " ", text)[:800],
            "relation": relation,
            "distance": distance,
        }

    def label_variants(self, label: str) -> list[str]:
        kind, value = label.split(" ", 1)
        value = value.strip()
        if kind == "Figure":
            return [f"fig. {value}", f"fig {value}", f"figure {value}", f"图{value}", f"图 {value}"]
        if kind == "Table":
            return [f"table {value}", f"tab. {value}", f"tab {value}", f"表{value}", f"表 {value}"]
        return [
            f"eq. {value}",
            f"eq ({value})",
            f"equation {value}",
            f"equation ({value})",
            f"公式{value}",
            f"公式 ({value})",
            f"式{value}",
            f"({value})",
        ]

    def horizontal_overlap(self, left: list[float], right: list[float]) -> float:
        overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
        width = max(1.0, min(left[2] - left[0], right[2] - right[0]))
        return overlap / width

    def valid_bbox(self, bbox: Any) -> bool:
        return (
            isinstance(bbox, list)
            and len(bbox) == 4
            and all(isinstance(value, int | float) for value in bbox)
            and bbox[0] < bbox[2]
            and bbox[1] < bbox[3]
        )

    def roman_to_int(self, value: str) -> int | None:
        total = 0
        previous = 0
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
        for char in reversed(value.upper()):
            current = values.get(char)
            if current is None:
                return None
            total += -current if current < previous else current
            previous = max(previous, current)
        return total
