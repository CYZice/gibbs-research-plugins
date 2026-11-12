"""Future MinerU-native structure enhancement contracts.

These types deliberately carry anchors instead of mutating canonical parser output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Protocol

from app.services.document_structure_fusion import DocumentIR


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    source_block_id: str
    char_start: int
    char_end: int
    page: int | None
    bbox: list[float] | None
    confidence: float
    provenance: str = "mineru"


@dataclass(frozen=True, slots=True)
class PaperMetadataCandidate:
    field: str
    value: str | list[dict]
    anchor: SourceAnchor


@dataclass(frozen=True, slots=True)
class PaperReference:
    raw_text: str
    anchor: SourceAnchor
    title: str | None = None
    doi: str | None = None


@dataclass(frozen=True, slots=True)
class CitationContext:
    marker: str
    quote: str
    anchor: SourceAnchor
    reference_index: int | None = None


@dataclass(frozen=True, slots=True)
class SectionSpan:
    title: str
    section_type: str
    anchor: SourceAnchor


@dataclass(frozen=True, slots=True)
class PaperCallout:
    kind: str
    label: str | None
    anchor: SourceAnchor
    asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalWorkCandidate:
    title: str | None
    doi: str | None
    anchor: SourceAnchor


@dataclass(frozen=True, slots=True)
class PaperRelation:
    relation: str
    source_anchor: SourceAnchor
    target_reference_index: int | None = None
    target_external_work: ExternalWorkCandidate | None = None


@dataclass(frozen=True, slots=True)
class StructureEnhancementInput:
    paper_id: str
    text: str
    content_map: dict
    mineru_artifact_keys: dict[str, str]
    asset_ids: tuple[str, ...]


@dataclass(slots=True)
class StructureEnhancementResult:
    metadata: list[PaperMetadataCandidate] = field(default_factory=list)
    references: list[PaperReference] = field(default_factory=list)
    citation_contexts: list[CitationContext] = field(default_factory=list)
    sections: list[SectionSpan] = field(default_factory=list)
    callouts: list[PaperCallout] = field(default_factory=list)
    external_works: list[ExternalWorkCandidate] = field(default_factory=list)
    relations: list[PaperRelation] = field(default_factory=list)


class MinerUStructureEnhancer(Protocol):
    def enhance(self, source: StructureEnhancementInput) -> StructureEnhancementResult: ...


class NoopMinerUStructureEnhancer:
    def enhance(self, source: StructureEnhancementInput) -> StructureEnhancementResult:
        for block in source.content_map.get("blocks", []):
            start, end = block.get("char_start"), block.get("char_end")
            if not isinstance(start, int) or not isinstance(end, int) or source.text[start:end] != block.get("content"):
                raise ValueError("content_map does not preserve canonical MinerU anchors.")
        return StructureEnhancementResult()


class MinerUNativeStructureEnhancer:
    """Deterministic, MinerU-only structural annotations.

    This deliberately avoids a second PDF-to-text parser. It is conservative: it
    emits only headings, reference-like blocks, citation markers and visual
    callouts that can be anchored in the canonical content map.
    """

    version = "mineru-native-v1"
    _citation = re.compile(r"\[(\d+(?:\s*[,\-–]\s*\d+)*)\]|\(([A-Z][A-Za-z'\-]+(?:\s+et al\.)?,?\s*\d{4}[a-z]?)\)")
    _callout = re.compile(r"\b(?P<kind>fig(?:ure)?|table|eq(?:uation)?|图|表|公式)\.?\s*\(?\s*(?P<label>\d+[a-zA-Z]?)", re.I)

    def enhance(self, source: StructureEnhancementInput) -> StructureEnhancementResult:
        NoopMinerUStructureEnhancer().enhance(source)
        result = StructureEnhancementResult()
        blocks = source.content_map.get("blocks", [])
        in_references = False
        reference_index = 0
        for block in blocks:
            content = str(block.get("content") or "")
            anchor = self._anchor(source, block, content)
            if anchor is None:
                continue
            raw_type = str(block.get("type") or "")
            if raw_type == "title":
                section_type = self._section_type(content)
                result.sections.append(SectionSpan(content, section_type, anchor))
                in_references = section_type == "references"
                continue
            if in_references and raw_type in {"paragraph", "text", "list"} and len(content) >= 12:
                reference_index += 1
                result.references.append(PaperReference(content, anchor, doi=self._doi(content)))
                continue
            if raw_type in {"paragraph", "text", "list"}:
                for match in self._citation.finditer(content):
                    result.citation_contexts.append(CitationContext(match.group(0), content, self._span_anchor(anchor, match.start(), match.end()), self._reference_index(match.group(1))))
                for match in self._callout.finditer(content):
                    result.callouts.append(PaperCallout(match.group("kind").lower(), match.group("label"), self._span_anchor(anchor, match.start(), match.end())))
        return result

    def _anchor(self, source: StructureEnhancementInput, block: dict, content: str) -> SourceAnchor | None:
        start, end = block.get("char_start"), block.get("char_end")
        if not isinstance(start, int) or not isinstance(end, int) or source.text[start:end] != content:
            return None
        bbox = block.get("normalized_bbox")
        return SourceAnchor(str(block.get("source_block_id")), start, end, block.get("page") if isinstance(block.get("page"), int) else None, bbox if isinstance(bbox, list) else None, 1.0, "mineru_native")

    def _span_anchor(self, parent: SourceAnchor, start: int, end: int) -> SourceAnchor:
        return SourceAnchor(parent.source_block_id, parent.char_start + start, parent.char_start + end, parent.page, parent.bbox, 0.9, parent.provenance)

    def _section_type(self, title: str) -> str:
        text = title.lower()
        for kind, aliases in {"abstract": ("abstract", "摘要"), "introduction": ("introduction", "引言"), "method": ("method", "experimental", "方法"), "result": ("result", "结果"), "discussion": ("discussion", "讨论"), "conclusion": ("conclusion", "结论"), "references": ("reference", "bibliography", "参考文献")}.items():
            if any(alias in text for alias in aliases):
                return kind
        return "body"

    @staticmethod
    def _reference_index(value: str | None) -> int | None:
        if value and value.strip().isdigit():
            return int(value.strip())
        return None

    @staticmethod
    def _doi(text: str) -> str | None:
        match = re.search(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", text, re.I)
        return match.group(0).rstrip(".,;") if match else None


class StructureEnhancerFusionHook:
    """Applies future section/callout annotations without moving DocumentIR blocks."""

    def __init__(self, result: StructureEnhancementResult) -> None:
        self.result = result

    def apply(self, document: DocumentIR) -> DocumentIR:
        by_source = {item.anchor.source_block_id: item for item in self.result.sections}
        for block in document.blocks:
            source_id = block.metadata.get("source_block_id")
            section = by_source.get(source_id)
            if section:
                block.metadata.update({"section_title": section.title, "section_type": section.section_type, "structure_source": section.anchor.provenance})
        return document
