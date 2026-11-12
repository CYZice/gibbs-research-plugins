from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.models.paper import Paper
from app.models.paper_identifier import PaperIdentifier
from app.models.paper_task import PaperTask
from app.models.resource_asset import ResourceAsset
from app.schemas.papers import PaperIngestResponse
from app.services.paper_search_service import normalized_identifier


class IngestionService:
    """Store a PDF and register only current v1 models."""

    def __init__(self, db: Session, storage_root: str, max_upload_bytes: int = 50 * 1024 * 1024) -> None:
        self.db, self.root, self.max_upload_bytes = db, Path(storage_root).resolve(), max_upload_bytes

    def ingest_uploaded_pdf(
        self,
        file: UploadFile,
        title: str | None = None,
        metadata: dict | None = None,
        source_provenance: str = "user_upload",
    ) -> PaperIngestResponse:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise ValueError("Uploaded file must have a .pdf filename.")
        return self.ingest_pdf_stream(
            file.file,
            file.filename,
            title=title,
            metadata=metadata,
            source_provenance=source_provenance,
        )

    def ingest_pdf_stream(
        self,
        stream: BinaryIO,
        filename: str,
        *,
        title: str | None = None,
        metadata: dict | None = None,
        source_provenance: str = "user_upload",
    ) -> PaperIngestResponse:
        if not filename.lower().endswith(".pdf"):
            raise ValueError("Uploaded file must have a .pdf filename.")
        metadata = metadata or {}
        paper_id, task_id = new_id("paper"), new_id("task")
        key = f"pdfs/{paper_id}.pdf"
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        digest, size = sha256(), 0
        try:
            existing = self.find_existing(metadata)
            if existing:
                self.merge_existing(existing, metadata, source_provenance)
                return self._duplicate(existing, task_id, filename, source_provenance)
            with path.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise ValueError(f"Uploaded PDF exceeds the {self.max_upload_bytes} byte limit.")
                    digest.update(chunk)
                    output.write(chunk)
            if not size:
                raise ValueError("Uploaded PDF file is empty.")
            pdf_hash = digest.hexdigest()
            existing = self.db.scalar(select(Paper).where(Paper.pdf_sha256 == pdf_hash).limit(1))
            if existing:
                path.unlink(missing_ok=True)
                self.merge_existing(existing, metadata, source_provenance)
                return self._duplicate(existing, task_id, filename, source_provenance)
            supplied_title = title or metadata.get("title")
            paper = Paper(paper_id=paper_id, title=supplied_title or Path(filename).stem, source_provenance=source_provenance, pdf_storage_key=key, pdf_sha256=pdf_hash, parse_status="not_started", evidence_status="not_started", embedding_status="not_started", multimodal_status="not_started", metadata_provenance={"title": source_provenance if supplied_title else "filename"})
            paper.title = supplied_title or Path(filename).stem
            paper.metadata_provenance = {"title": source_provenance if supplied_title else "filename"}
            self._apply_metadata(paper, metadata, source_provenance)
            asset = ResourceAsset(asset_id=new_id("asset"), paper_id=paper_id, evidence_id=None, asset_type="pdf", variant="original", mime_type="application/pdf", storage_key=key, sha256=pdf_hash, size_bytes=size, caption=None, location=None, created_from=source_provenance)
            task = PaperTask(task_id=task_id, paper_id=paper_id, operation="ingest", provider=source_provenance, status="succeeded", progress=1.0, details={"result": {"original_filename": filename, "storage_key": key}})
            self.db.add_all([paper, asset, task])
            self._register_identifiers(paper, metadata)
            try:
                self.db.commit()
            except IntegrityError:
                # A concurrent upload may have committed the same SHA after our lookup.
                self.db.rollback()
                path.unlink(missing_ok=True)
                existing = self.db.scalar(select(Paper).where(Paper.pdf_sha256 == pdf_hash).limit(1)) or self.find_existing(metadata)
                if not existing:
                    raise
                self.merge_existing(existing, metadata, source_provenance)
                return self._duplicate(existing, task_id, filename, source_provenance)
            return PaperIngestResponse(paper_id=paper_id, task_id=task_id, status="succeeded")
        except Exception:
            self.db.rollback()
            path.unlink(missing_ok=True)
            raise

    def merge_existing(
        self, paper: Paper, metadata: dict, source_provenance: str
    ) -> Paper:
        """Fill missing canonical metadata and attach newly observed strong identifiers."""
        self._apply_metadata(paper, metadata, source_provenance)
        self._register_identifiers(paper, metadata)
        return paper

    def find_existing(self, metadata: dict) -> Paper | None:
        for kind, value in self._identifiers(metadata):
            identifier = self.db.scalar(
                select(PaperIdentifier).where(
                    PaperIdentifier.identifier_type == kind,
                    PaperIdentifier.normalized_value == normalized_identifier(value),
                )
            )
            if identifier:
                return self.db.get(Paper, identifier.paper_id)
        return None

    def _identifiers(self, metadata: dict):
        return [
            (kind, str(value))
            for kind, value in (
                ("doi", metadata.get("doi")),
                ("arxiv", metadata.get("arxiv_id")),
                ("openalex", metadata.get("openalex_id")),
                ("core", metadata.get("core_id")),
                ("pmid", metadata.get("pmid")),
                ("pmcid", metadata.get("pmcid")),
            )
            if value
        ]

    def _register_identifiers(self, paper: Paper, metadata: dict) -> None:
        for kind, value in self._identifiers(metadata):
            normalized = normalized_identifier(value)
            existing = self.db.scalar(
                select(PaperIdentifier).where(
                    PaperIdentifier.identifier_type == kind,
                    PaperIdentifier.normalized_value == normalized,
                )
            )
            if existing is None:
                self.db.add(
                    PaperIdentifier(
                        paper_id=paper.paper_id,
                        identifier_type=kind,
                        normalized_value=normalized,
                    )
                )

    def _duplicate(
        self, paper: Paper, task_id: str, filename: str, source_provenance: str
    ) -> PaperIngestResponse:
        task = PaperTask(
            task_id=task_id,
            paper_id=paper.paper_id,
            operation="ingest",
            provider=source_provenance,
            status="succeeded",
            progress=1.0,
            details={"result": {"duplicate": True, "original_filename": filename}},
        )
        self.db.add(task)
        self.db.commit()
        return PaperIngestResponse(paper_id=paper.paper_id, task_id=task_id, status="duplicate")

    def _apply_metadata(
        self, paper: Paper, metadata: dict, source_provenance: str = "manifest"
    ) -> None:
        provenance = dict(paper.metadata_provenance or {})
        for field in (
            "title", "doi", "arxiv_id", "venue", "abstract", "publication_type",
            "language", "license", "open_access", "source_url",
        ):
            current = getattr(paper, field)
            empty = current is None or (isinstance(current, str) and not current.strip())
            if metadata.get(field) is not None and empty:
                setattr(paper, field, metadata[field])
                provenance[field] = source_provenance
        if metadata.get("year") is not None and paper.year is None:
            paper.year = int(metadata["year"])
            provenance["year"] = source_provenance
        if metadata.get("publication_date") and paper.publication_date is None:
            paper.publication_date = date.fromisoformat(metadata["publication_date"])
            provenance["publication_date"] = source_provenance
        if metadata.get("authors") and not paper.authors:
            paper.authors = metadata["authors"]
            provenance["authors"] = source_provenance
        if metadata.get("keywords") and not paper.keywords:
            paper.keywords = metadata["keywords"]
            provenance["keywords"] = source_provenance
        paper.metadata_provenance = provenance
