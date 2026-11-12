from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import ModelEndpointConfig, Settings
from app.models.evidence_embedding import EvidenceEmbedding
from app.models.evidence_representation import EvidenceRepresentation
from app.models.paper import Paper
from app.models.paper_task import PaperTask
from app.services.model_gateway import ModelGateway, get_model_gateway, runtime_model_name

VECTOR_DIMENSION = 1024


class EmbeddingIndexError(Exception):
    pass


@dataclass(slots=True)
class EmbeddingBuildResult:
    paper_id: str
    embedded_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class _EmbeddingTarget:
    representation_id: str
    text: str
    text_hash: str


class EmbeddingIndexService:
    def __init__(self, db: Session, settings: Settings, model_gateway: ModelGateway | None = None) -> None:
        self.db, self.settings = db, settings
        self.model_gateway = model_gateway or get_model_gateway(settings)

    def build_for_paper(
        self, paper: Paper, force_rebuild: bool = False, *, should_cancel=None, on_progress=None
    ) -> EmbeddingBuildResult:
        model = runtime_model_name(self.model_gateway, "embedding", self.settings.embedding_model)
        rebuild = self.db.scalar(
            select(PaperTask).where(PaperTask.operation == "reembed_all")
            .order_by(PaperTask.created_at.desc()).limit(1)
        )
        if rebuild and (rebuild.details or {}).get("target_model") == model and rebuild.status != "succeeded":
            raise EmbeddingIndexError("Embedding rebuild is in progress.")
        paper.embedding_status = "building"
        self.db.commit()
        try:
            reps = self.db.scalars(select(EvidenceRepresentation).where(EvidenceRepresentation.evidence.has(paper_id=paper.paper_id), EvidenceRepresentation.status == "success", EvidenceRepresentation.is_active.is_(True), EvidenceRepresentation.indexable.is_(True))).all()
            result = self._embed(
                reps, paper.paper_id, force_rebuild,
                should_cancel=should_cancel, on_progress=on_progress,
            )
            paper.embedding_status = "ready"
            self.db.commit()
            return result
        except Exception as exc:
            self.db.rollback()
            error = EmbeddingIndexError(str(exc))
            for name in ("role", "provider", "credential_id", "http_status", "error_code", "retryable", "retry_after"):
                if hasattr(exc, name):
                    setattr(error, name, getattr(exc, name))
            raise error from exc

    def build_for_representation_ids(
        self, representation_ids: list[str], *, model: str | None = None, should_cancel=None
    ) -> EmbeddingBuildResult:
        reps = self.db.scalars(select(EvidenceRepresentation).where(
            EvidenceRepresentation.representation_id.in_(list(dict.fromkeys(representation_ids))),
            EvidenceRepresentation.status == "success",
            EvidenceRepresentation.is_active.is_(True),
            EvidenceRepresentation.indexable.is_(True),
        )).all()
        return self._embed(reps, "", False, should_cancel, model)

    def _embed(
        self, reps: list[EvidenceRepresentation], paper_id: str, force: bool,
        should_cancel=None, model_override: str | None = None, on_progress=None,
    ) -> EmbeddingBuildResult:
        if not reps: return EmbeddingBuildResult(paper_id, 0, 0)
        resolver = getattr(self.model_gateway, "resolve_embedding_endpoint", None)
        endpoint = resolver(self.settings.embedding_model) if callable(resolver) else ModelEndpointConfig(
            role="embedding", model=runtime_model_name(self.model_gateway, "embedding", self.settings.embedding_model)
        )
        if model_override:
            endpoint = endpoint.model_copy(update={"model": model_override})
        model = endpoint.model
        current = {item.representation_id: item for item in self.db.scalars(select(EvidenceEmbedding).where(EvidenceEmbedding.representation_id.in_([rep.representation_id for rep in reps]), EvidenceEmbedding.embedding_model == model)).all()}
        targets = []
        metadata_changed = False
        for rep in reps:
            metadata = dict(getattr(rep, "representation_metadata", None) or {})
            embedding_metadata = dict(metadata.get("embedding") or {})
            if len(rep.text) > self.settings.embedding_max_input_chars:
                skipped_metadata = {
                    "status": "skipped",
                    "reason": "input_too_large",
                    "input_chars": len(rep.text),
                    "max_input_chars": self.settings.embedding_max_input_chars,
                }
                if embedding_metadata != skipped_metadata:
                    metadata["embedding"] = skipped_metadata
                    rep.representation_metadata = metadata
                    metadata_changed = True
                if existing_embedding := current.get(rep.representation_id):
                    self.db.delete(existing_embedding)
                    metadata_changed = True
                continue
            if embedding_metadata.get("reason") == "input_too_large":
                metadata.pop("embedding", None)
                rep.representation_metadata = metadata
                metadata_changed = True
            if rep.text.strip() and (
                force or not self._fresh(current.get(rep.representation_id), rep)
            ):
                targets.append(
                    _EmbeddingTarget(
                        representation_id=rep.representation_id,
                        text=rep.text,
                        text_hash=rep.text_hash,
                    )
                )
        if not targets:
            if metadata_changed:
                self.db.commit()
            return EmbeddingBuildResult(paper_id, 0, len(reps))
        batches = self._embedding_batches(targets)
        total_batches = len(batches)
        self.db.commit()
        embedded_count = 0
        for batch_number, batch in enumerate(batches, start=1):
            if should_cancel:
                should_cancel()
                self.db.commit()
            caller = getattr(self.model_gateway, "embed_texts_for_endpoint", None)
            vectors = caller([rep.text for rep in batch], endpoint) if callable(caller) else self.model_gateway.embed_texts([rep.text for rep in batch])
            if len(vectors) != len(batch) or any(len(vector) != VECTOR_DIMENSION for vector in vectors):
                raise EmbeddingIndexError("Embedding backend returned invalid vectors.")
            if should_cancel:
                should_cancel()
            for rep, vector in zip(batch, vectors, strict=True):
                values = {
                    "representation_id": rep.representation_id,
                    "embedding_model": model,
                    "text_hash": rep.text_hash,
                    "embedding_dim": VECTOR_DIMENSION,
                    "status": "success",
                    "embedding": vector,
                    "error_message": None,
                }
                self.db.execute(
                    insert(EvidenceEmbedding)
                    .values(**values)
                    .on_conflict_do_update(
                        constraint="uq_evidence_embeddings_representation_model",
                        set_={
                            key: value
                            for key, value in values.items()
                            if key not in {"representation_id", "embedding_model"}
                        },
                    )
                )
            self.db.commit()
            embedded_count += len(batch)
            if on_progress:
                on_progress(
                    batch_number,
                    total_batches,
                    embedded_count,
                    len(targets),
                )
        return EmbeddingBuildResult(paper_id, len(targets), len(reps) - len(targets))

    def _embedding_batches(
        self, targets: list[_EmbeddingTarget]
    ) -> list[list[_EmbeddingTarget]]:
        batches: list[list[_EmbeddingTarget]] = []
        current: list[_EmbeddingTarget] = []
        current_chars = 0
        for target in targets:
            if current and (
                len(current) >= self.settings.embedding_batch_size
                or current_chars + len(target.text)
                > self.settings.embedding_batch_max_chars
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(target)
            current_chars += len(target.text)
        if current:
            batches.append(current)
        return batches

    def _fresh(self, item: EvidenceEmbedding | None, rep: EvidenceRepresentation) -> bool:
        return bool(item and item.status == "success" and item.embedding_dim == VECTOR_DIMENSION and item.text_hash == rep.text_hash)
