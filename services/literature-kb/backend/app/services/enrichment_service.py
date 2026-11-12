from __future__ import annotations

import json
from hashlib import sha256

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.evidence_embedding import EvidenceEmbedding
from app.models.evidence_representation import EvidenceRepresentation
from app.models.evidence_unit import EvidenceUnit
from app.models.paper import Paper
from app.schemas.structure import MultimodalEnrichResponse
from app.services.mineru_representation import mineru_text, multimodal_text
from app.services.model_gateway import ModelGateway


class EnrichmentService:
    """Build deterministic resource context from canonical MinerU evidence."""

    version = "v3-mineru-resource-context"

    def __init__(
        self, db: Session, settings: Settings, model_gateway: ModelGateway | None = None
    ) -> None:
        self.db = db
        self.settings = settings

    def enrich_paper(
        self, paper: Paper, task=None, force: bool = False
    ) -> MultimodalEnrichResponse:
        paper.multimodal_status = "running"
        multimodal_evidence = self.db.scalars(
            select(EvidenceUnit)
            .where(
                EvidenceUnit.paper_id == paper.paper_id,
                EvidenceUnit.modality.in_(("figure", "table", "formula")),
            )
            .order_by(EvidenceUnit.block_order, EvidenceUnit.evidence_id)
        ).all()
        context_pool = self.db.scalars(
            select(EvidenceUnit)
            .where(
                EvidenceUnit.paper_id == paper.paper_id,
                EvidenceUnit.modality == "text",
                EvidenceUnit.exclude_from_retrieval.is_(False),
            )
            .order_by(EvidenceUnit.block_order, EvidenceUnit.evidence_id)
        ).all()
        changed: list[str] = []
        failed = 0
        processed = 0
        for item in multimodal_evidence:
            processed += 1
            try:
                text, metadata, repr_type = self._representation_input(item, context_pool)
                input_hash = self._hash(
                    json.dumps(metadata["input"], ensure_ascii=False, sort_keys=True)
                )
                metadata["input_hash"] = input_hash
                representation_id = f"{item.evidence_id}_{repr_type}"
                current = self.db.get(EvidenceRepresentation, representation_id)
                text_hash = self._hash(text)
                if (
                    current
                    and not force
                    and current.text_hash == text_hash
                    and (current.representation_metadata or {}).get("input_hash") == input_hash
                    and current.status == "success"
                ):
                    continue
                if current is None:
                    current = EvidenceRepresentation(
                        representation_id=representation_id,
                        evidence_id=item.evidence_id,
                        repr_type=repr_type,
                        text=text,
                        model_name=metadata.get("model"),
                        prompt_version=self.version,
                        text_hash=text_hash,
                        status="success",
                        is_active=True,
                        indexable=not item.exclude_from_retrieval,
                        error_message=None,
                        representation_metadata=metadata,
                    )
                    self.db.add(current)
                else:
                    self.db.execute(
                        delete(EvidenceEmbedding).where(
                            EvidenceEmbedding.representation_id == current.representation_id
                        )
                    )
                    current.text = text
                    current.model_name = metadata.get("model")
                    current.prompt_version = self.version
                    current.text_hash = text_hash
                    current.status = "success"
                    current.indexable = not item.exclude_from_retrieval
                    current.error_message = None
                    current.representation_metadata = metadata
                    current.is_active = True
                self.db.execute(
                    EvidenceRepresentation.__table__.update()
                    .where(
                        EvidenceRepresentation.evidence_id == item.evidence_id,
                        EvidenceRepresentation.representation_id != representation_id,
                    )
                    .values(is_active=False)
                )
                changed.append(representation_id)
            except (ValueError, TypeError) as exc:
                failed += 1
                self._record_failure(item, str(exc))
        if changed:
            paper.embedding_status = "not_started"
        paper.multimodal_status = "ready" if not failed else "partial"
        self.db.commit()
        result = MultimodalEnrichResponse(
            paper_id=paper.paper_id,
            enriched_count=len(changed),
            processed_count=processed,
            changed_representation_ids=changed,
            remaining_count=failed,
            failed_count=failed,
        )
        if task:
            details = dict(task.details or {})
            details["provider"] = {
                "role": None,
                "provider": None,
                "model": None,
                "model_called": False,
                "vision_called": False,
            }
            details["result"] = result.model_dump()
            task.details, task.progress = details, 1.0
        return result

    def _representation_input(
        self, item: EvidenceUnit, context_pool: list[EvidenceUnit]
    ) -> tuple[str, dict, str]:
        context = self._nearby_text(item, context_pool)
        metadata = item.evidence_metadata or {}
        caption = str(metadata.get("caption") or "").strip()
        label = str(metadata.get("figure_label") or metadata.get("table_label") or "").strip()
        normalized_raw = mineru_text(item.raw_content)
        base = {
            "source": "mineru",
            "version": self.version,
            "input": {
                "modality": item.modality,
                "content": normalized_raw,
                "caption": caption,
                "label": label,
                "section": item.section_title,
                "context": context,
                "asset_ids": item.asset_ids or [],
            },
        }
        base.update(
            {
                "method": "mineru_formula_context" if item.modality == "formula" else "mineru_caption_context",
                "model_called": False,
                "vision_called": False,
            }
        )
        return (
            multimodal_text(
                item.modality,
                normalized_raw,
                caption=caption,
                label=label,
                section=item.section_title or "",
                context=context,
            ),
            base,
            f"{item.modality}_caption_context",
        )

    def _nearby_text(self, item: EvidenceUnit, context_pool: list[EvidenceUnit]) -> str:
        candidates = [row for row in context_pool if row.page == item.page and row.raw_content]
        candidates.sort(key=lambda row: abs((row.block_order or 0) - (item.block_order or 0)))
        return "\n".join(mineru_text(row.raw_content) for row in candidates[:2])[:2400]

    def _record_failure(self, item: EvidenceUnit, message: str) -> None:
        representation_id = f"{item.evidence_id}_{item.modality}_caption_context"
        row = self.db.get(EvidenceRepresentation, representation_id)
        if row is None:
            self.db.add(
                EvidenceRepresentation(
                    representation_id=representation_id,
                    evidence_id=item.evidence_id,
                    repr_type=f"{item.modality}_caption_context",
                    text="",
                    model_name=None,
                    prompt_version=self.version,
                    text_hash=self._hash(""),
                    status="failed",
                    is_active=False,
                    indexable=False,
                    error_message=message,
                    representation_metadata={"source": "mineru", "model_called": False, "vision_called": False},
                )
            )
        else:
            row.status, row.is_active, row.indexable, row.error_message = (
                "failed",
                False,
                False,
                message,
            )

    @staticmethod
    def _hash(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()
