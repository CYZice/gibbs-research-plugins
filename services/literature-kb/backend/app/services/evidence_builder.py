from __future__ import annotations

import json
import mimetypes
from hashlib import sha256
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.ids import new_id
from app.models.citation_context import CitationContext
from app.models.evidence_embedding import EvidenceEmbedding
from app.models.evidence_representation import EvidenceRepresentation
from app.models.evidence_unit import EvidenceUnit
from app.models.paper import Paper
from app.models.paper_content import PaperContent
from app.models.paper_reference import PaperReference
from app.models.paper_task import PaperTask
from app.models.resource_asset import ResourceAsset
from app.schemas.evidence import EvidenceBuildResponse
from app.services.mineru_representation import mineru_text, multimodal_text


class EvidenceBuildError(Exception):
    pass


class EvidenceBuilderService:
    builder_version = "v3-normalized-representations"
    excluded_types = {"title", "page_header", "page_footer", "page_number", "page_footnote"}

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db, self.root = db, Path(settings.storage_root).resolve()

    def build_for_paper(
        self,
        paper: Paper,
        task: PaperTask | None = None,
        force: bool = False,
        full_rebuild: bool = False,
    ) -> EvidenceBuildResponse:
        content = self.db.get(PaperContent, paper.paper_id)
        if not content:
            raise EvidenceBuildError("PaperContent is required before evidence can be built.")
        map_key = (
            ((task.details or {}).get("artifacts") or {}).get("content_map_key") if task else None
        )
        path = self._path(map_key or f"parsed/{paper.paper_id}/content_map.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 2:
            raise EvidenceBuildError("content-map v2 is required; run repair_content_map first.")
        map_hash = sha256(path.read_bytes()).hexdigest()
        previous = (task.details or {}).get("input") if task else None
        if not force and previous == {
            "content_sha256": content.content_sha256,
            "content_map_sha256": map_hash,
            "builder_version": self.builder_version,
        }:
            self._finish(
                task,
                paper,
                {
                    "created": 0,
                    "updated": 0,
                    "deleted": 0,
                    "unchanged": self.count_for_paper(paper.paper_id),
                    "skipped": True,
                },
                content,
                map_hash,
                False,
            )
            return EvidenceBuildResponse(
                paper_id=paper.paper_id,
                evidence_count=self.count_for_paper(paper.paper_id),
                source_file=str(path),
            )

        if full_rebuild:
            self._clear_structure(paper.paper_id)
            self.db.flush()
            self._delete_all(paper.paper_id)
            self.db.flush()
        else:
            self._clear_structure(paper.paper_id)
        existing = {
            str((row.evidence_metadata or {}).get("source_block_id")): row
            for row in self.db.scalars(
                select(EvidenceUnit).where(EvidenceUnit.paper_id == paper.paper_id)
            ).all()
        }
        units = self._units(paper.paper_id, content.text, payload["blocks"])
        seen, result, representation_changed = (
            set(),
            {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "skipped": False},
            False,
        )
        for unit, representation_text, asset_spec in units:
            source_id = unit.evidence_metadata["source_block_id"]
            seen.add(source_id)
            current = existing.get(source_id)
            if current is None:
                self.db.add(unit)
                self.db.flush()
                unit.asset_ids = self._sync_asset(paper, unit, asset_spec)
                self.db.add(self._representation(unit, representation_text))
                result["created"] += 1
                representation_changed = True
                continue
            old_fingerprint = (current.evidence_metadata or {}).get("fingerprint")
            old_rep = self.db.scalar(
                select(EvidenceRepresentation).where(
                    EvidenceRepresentation.evidence_id == current.evidence_id,
                    EvidenceRepresentation.is_active.is_(True),
                )
            )
            if old_fingerprint == unit.evidence_metadata["fingerprint"]:
                result["unchanged"] += 1
                continue
            for field in (
                "page",
                "char_start",
                "char_end",
                "section_title",
                "section_type",
                "modality",
                "block_type",
                "block_order",
                "exclude_from_retrieval",
                "raw_content",
                "bbox",
                "coordinate_space",
                "parse_source",
                "confidence",
                "evidence_metadata",
            ):
                setattr(current, field, getattr(unit, field))
            current.asset_ids = self._sync_asset(paper, current, asset_spec)
            if not old_rep or old_rep.text_hash != self._hash(representation_text):
                if old_rep:
                    self.db.execute(
                        delete(EvidenceEmbedding).where(
                            EvidenceEmbedding.representation_id == old_rep.representation_id
                        )
                    )
                    old_rep.text, old_rep.text_hash, old_rep.indexable = (
                        representation_text,
                        self._hash(representation_text),
                        not current.exclude_from_retrieval,
                    )
                else:
                    self.db.add(self._representation(current, representation_text))
                representation_changed = True
            result["updated"] += 1
        for source_id, row in existing.items():
            if source_id in seen:
                continue
            self._delete_evidence(row)
            result["deleted"] += 1
            representation_changed = True
        source_to_evidence = {
            str((row.evidence_metadata or {}).get("source_block_id")): row.evidence_id
            for row in self.db.scalars(
                select(EvidenceUnit).where(EvidenceUnit.paper_id == paper.paper_id)
            ).all()
        }
        self._sync_structure(paper.paper_id, payload.get("structure") or {}, source_to_evidence)
        self._finish(task, paper, result, content, map_hash, representation_changed)
        return EvidenceBuildResponse(
            paper_id=paper.paper_id, evidence_count=len(units), source_file=str(path)
        )

    def count_for_paper(self, paper_id: str) -> int:
        return len(
            self.db.scalars(
                select(EvidenceUnit.evidence_id).where(EvidenceUnit.paper_id == paper_id)
            ).all()
        )

    def _units(
        self, paper_id: str, text: str, blocks: list[dict]
    ) -> list[tuple[EvidenceUnit, str, dict | None]]:
        rows, section_title, section_type = [], None, "body"
        for order, block in enumerate(blocks):
            raw = str(block.get("content") or "").strip()
            start, end = block.get("char_start"), block.get("char_end")
            if (
                not raw
                or not isinstance(start, int)
                or not isinstance(end, int)
                or text[start:end] != raw
            ):
                continue
            raw_type = str(block.get("type") or "paragraph")
            if raw_type == "title":
                section_title, section_type = raw, self._section_type(raw)
            modality = {
                "image": "figure",
                "chart": "figure",
                "table": "table",
                "equation_interline": "formula",
                "interline_equation": "formula",
            }.get(raw_type, "text")
            normalized = (
                block.get("normalized_bbox")
                if self._valid_normalized(block.get("normalized_bbox"))
                else None
            )
            metadata = dict(block.get("metadata") or {})
            metadata.update(
                {
                    "source_block_id": str(block.get("source_block_id") or f"p0_b{order}"),
                    "source_bbox": {
                        "provider": "mineru",
                        "raw": block.get("bbox"),
                        "raw_coordinate_space": block.get("raw_coordinate_space"),
                    }
                    if block.get("bbox")
                    else None,
                    "builder_version": self.builder_version,
                }
            )
            caption = str(metadata.get("caption") or "").strip()
            asset_spec = self._asset_spec(paper_id, raw_type, block, normalized, caption)
            fingerprint_data = {
                "text": raw,
                "page": block.get("page"),
                "bbox": normalized,
                "type": raw_type,
                "section": section_type,
                "caption": caption,
                "asset": asset_spec.get("storage_key") if asset_spec else None,
                "version": self.builder_version,
            }
            metadata["fingerprint"] = self._hash(
                json.dumps(fingerprint_data, ensure_ascii=False, sort_keys=True)
            )
            evidence_id = (
                f"ev_{paper_id}_{sha256(metadata['source_block_id'].encode()).hexdigest()[:20]}"
            )
            exclude = raw_type in self.excluded_types
            unit = EvidenceUnit(
                evidence_id=evidence_id,
                paper_id=paper_id,
                page=block.get("page")
                if isinstance(block.get("page"), int) and block.get("page") >= 1
                else None,
                char_start=start,
                char_end=end,
                section_title=section_title,
                section_type=section_type,
                modality=modality,
                block_type=raw_type,
                block_order=order,
                exclude_from_retrieval=exclude,
                raw_content=raw,
                bbox=normalized,
                coordinate_space="normalized" if normalized else None,
                asset_ids=[],
                parse_source="mineru",
                confidence=1.0,
                evidence_metadata=metadata,
            )
            representation = self._representation_text(unit, caption)
            rows.append((unit, representation, asset_spec))
        return rows

    def _asset_spec(
        self, paper_id: str, raw_type: str, block: dict, bbox: list[float] | None, caption: str
    ) -> dict | None:
        kind = {
            "image": "figure",
            "chart": "figure",
            "table": "table",
            "equation_interline": "formula",
            "interline_equation": "formula",
        }.get(raw_type)
        if not kind:
            return None
        location = (
            {"page": block.get("page"), "bbox": bbox, "coordinate_space": "normalized"}
            if bbox and isinstance(block.get("page"), int)
            else None
        )
        path = block.get("asset_path")
        if isinstance(path, str) and path:
            absolute = (self.root / "parsed" / paper_id / path).resolve()
            if absolute.is_file() and absolute.is_relative_to(self.root):
                return {
                    "asset_type": kind,
                    "variant": "source",
                    "mime_type": mimetypes.guess_type(absolute.name)[0]
                    or "application/octet-stream",
                    "storage_key": str(absolute.relative_to(self.root)).replace("\\", "/"),
                    "caption": caption or None,
                    "location": location,
                }
        return {
            "asset_type": kind,
            "variant": "derived",
            "mime_type": "text/plain",
            "storage_key": None,
            "caption": caption or None,
            "location": location,
            "text": str(block.get("content") or ""),
        }

    def _sync_asset(self, paper: Paper, evidence: EvidenceUnit, spec: dict | None) -> list[str]:
        if not spec:
            self.db.execute(
                delete(ResourceAsset).where(
                    ResourceAsset.paper_id == paper.paper_id,
                    ResourceAsset.evidence_id == evidence.evidence_id,
                )
            )
            return []
        asset = self.db.scalar(
            select(ResourceAsset).where(
                ResourceAsset.paper_id == paper.paper_id,
                ResourceAsset.evidence_id == evidence.evidence_id,
                ResourceAsset.asset_type == spec["asset_type"],
                ResourceAsset.variant == spec["variant"],
            )
        )
        if not asset:
            asset = ResourceAsset(
                asset_id=new_id("asset"),
                paper_id=paper.paper_id,
                evidence_id=evidence.evidence_id,
                asset_type=spec["asset_type"],
                variant=spec["variant"],
                mime_type=spec["mime_type"],
                storage_key=spec.get("storage_key"),
                sha256=None,
                size_bytes=None,
                caption=spec.get("caption"),
                location=spec.get("location"),
                created_from="mineru",
            )
            self.db.add(asset)
        else:
            for field in ("mime_type", "storage_key", "caption", "location"):
                setattr(asset, field, spec.get(field))
        return [asset.asset_id]

    def _representation_text(self, unit: EvidenceUnit, caption: str) -> str:
        if unit.modality in {"figure", "formula", "table"}:
            metadata = unit.evidence_metadata or {}
            return multimodal_text(
                unit.modality,
                unit.raw_content,
                caption=caption,
                label=str(metadata.get("figure_label") or metadata.get("table_label") or ""),
                section=unit.section_title or "",
            )
        return mineru_text(unit.raw_content)

    def _representation(self, unit: EvidenceUnit, text: str) -> EvidenceRepresentation:
        return EvidenceRepresentation(
            representation_id=f"{unit.evidence_id}_weak",
            evidence_id=unit.evidence_id,
            repr_type="weak",
            text=text,
            model_name=None,
            prompt_version=None,
            text_hash=self._hash(text),
            status="success",
            is_active=True,
            indexable=bool(text.strip()) and not unit.exclude_from_retrieval,
            error_message=None,
            representation_metadata={
                "source": "mineru_content_map",
                "builder_version": self.builder_version,
            },
        )

    def _section_type(self, title: str) -> str:
        text = title.lower()
        for key, aliases in {
            "abstract": ("abstract", "摘要"),
            "introduction": ("introduction", "intro", "引言"),
            "method": ("method", "materials", "experimental", "方法"),
            "result": ("result", "results", "结果"),
            "discussion": ("discussion", "讨论"),
            "conclusion": ("conclusion", "结论"),
            "references": ("reference", "bibliography", "参考文献"),
        }.items():
            if any(alias in text for alias in aliases):
                return key
        return "body"

    def _finish(
        self,
        task: PaperTask | None,
        paper: Paper,
        result: dict,
        content: PaperContent,
        map_hash: str,
        representation_changed: bool,
    ) -> None:
        paper.evidence_status = "ready"
        if representation_changed:
            paper.embedding_status = "not_started"
        if task:
            details = dict(task.details or {})
            details["input"] = {
                "content_sha256": content.content_sha256,
                "content_map_sha256": map_hash,
                "builder_version": self.builder_version,
            }
            details["result"] = result
            task.details, task.progress = details, 1.0
        self.db.commit()

    def _delete_all(self, paper_id: str) -> None:
        for row in self.db.scalars(
            select(EvidenceUnit).where(EvidenceUnit.paper_id == paper_id)
        ).all():
            self._delete_evidence(row)

    def _delete_evidence(self, row: EvidenceUnit) -> None:
        self.db.execute(
            delete(CitationContext).where(CitationContext.evidence_id == row.evidence_id)
        )
        ids = self.db.scalars(
            select(EvidenceRepresentation.representation_id).where(
                EvidenceRepresentation.evidence_id == row.evidence_id
            )
        ).all()
        if ids:
            self.db.execute(
                delete(EvidenceEmbedding).where(EvidenceEmbedding.representation_id.in_(ids))
            )
        self.db.execute(
            delete(EvidenceRepresentation).where(
                EvidenceRepresentation.evidence_id == row.evidence_id
            )
        )
        self.db.execute(delete(ResourceAsset).where(ResourceAsset.evidence_id == row.evidence_id))
        self.db.delete(row)

    def _sync_structure(
        self, paper_id: str, structure: dict, source_to_evidence: dict[str, str]
    ) -> None:
        """Persist MinerU-native reference/citation candidates without touching anchors."""
        references: list[PaperReference] = []
        for index, item in enumerate(structure.get("references") or [], start=1):
            raw = str(item.get("raw_text") or "").strip()
            if not raw:
                continue
            source_id = str(item.get("source_block_id") or "")
            reference_id = f"ref_{paper_id}_{sha256(f'{source_id}:{item.get("char_start")}:{item.get("char_end")}'.encode()).hexdigest()[:20]}"
            references.append(
                PaperReference(
                    reference_id=reference_id,
                    paper_id=paper_id,
                    target_paper_id=None,
                    order_index=index,
                    raw_text=raw,
                    title=None,
                    authors=None,
                    year=None,
                    venue=None,
                    doi=item.get("doi"),
                    arxiv_id=None,
                    url=None,
                    confidence=item.get("confidence"),
                )
            )
        self.db.add_all(references)
        self.db.flush()
        by_order = {item.order_index: item.reference_id for item in references}
        for item in structure.get("citation_contexts") or []:
            source_id = str(item.get("source_block_id") or "")
            quote = str(item.get("quote") or "").strip()
            if not quote:
                continue
            context_id = f"ctx_{paper_id}_{sha256(f'{source_id}:{item.get("char_start")}:{item.get("char_end")}:{item.get("marker")}'.encode()).hexdigest()[:20]}"
            self.db.add(
                CitationContext(
                    context_id=context_id,
                    paper_id=paper_id,
                    reference_id=by_order.get(item.get("reference_index")),
                    evidence_id=source_to_evidence.get(source_id),
                    page=item.get("page") if isinstance(item.get("page"), int) else None,
                    char_start=item.get("char_start")
                    if isinstance(item.get("char_start"), int)
                    else None,
                    char_end=item.get("char_end")
                    if isinstance(item.get("char_end"), int)
                    else None,
                    section_title=None,
                    section_type=None,
                    marker=item.get("marker"),
                    quote=quote,
                )
            )

    def _clear_structure(self, paper_id: str) -> None:
        self.db.execute(delete(CitationContext).where(CitationContext.paper_id == paper_id))
        self.db.execute(delete(PaperReference).where(PaperReference.paper_id == paper_id))

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise EvidenceBuildError("Invalid storage key.")
        return path

    def _valid_normalized(self, value) -> bool:
        return bool(
            isinstance(value, list)
            and len(value) == 4
            and all(isinstance(item, int | float) for item in value)
            and 0 <= value[0] < value[2] <= 1
            and 0 <= value[1] < value[3] <= 1
        )

    def _hash(self, value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()
