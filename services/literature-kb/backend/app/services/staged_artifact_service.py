from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.core.config import Settings
from app.core.ids import new_id
from app.models.import_batch import ImportBatchItem
from app.models.paper import Paper
from app.models.staged_artifact import StagedArtifact
from app.services.discovery_download_storage import DiscoveryDownloadStorage
from app.services.ingestion_service import IngestionService
from app.services.paper_access_service import grant_paper_access


@dataclass(slots=True)
class RegistrationResult:
    paper: Paper
    duplicate: bool
    completed_stages: set[str]


class StagedArtifactService:
    def __init__(self, db, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def from_download(
        self,
        result,
        *,
        source_type: str,
        source_external_id: str | None,
        metadata_payload: dict | None,
    ) -> StagedArtifact:
        artifact = self.db.scalar(
            select(StagedArtifact).where(
                StagedArtifact.storage_key == result.storage_key
            )
        )
        if artifact:
            artifact.metadata_payload = metadata_payload or artifact.metadata_payload
            artifact.status = "available"
            return artifact
        artifact = StagedArtifact(
            artifact_id=new_id("artifact"),
            storage_provider=self.settings.discovery_storage_provider,
            storage_key=result.storage_key,
            metadata_key=result.metadata_key,
            sha256=result.sha256,
            size_bytes=result.size_bytes,
            source_type=source_type,
            source_external_id=source_external_id,
            status="available",
            metadata_payload=metadata_payload,
        )
        self.db.add(artifact)
        self.db.flush()
        return artifact

    def stage_local_file(
        self,
        path: Path,
        *,
        source_type: str,
        source_external_id: str | None = None,
        metadata_payload: dict | None = None,
    ) -> StagedArtifact:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError(f"{path.name} is not a valid PDF.")
            stream.seek(0)
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_upload_bytes:
                    raise ValueError(
                        f"{path.name} exceeds the configured upload limit."
                    )
                digest.update(chunk)
        checksum = digest.hexdigest()
        existing = self.db.scalar(
            select(StagedArtifact)
            .where(StagedArtifact.sha256 == checksum)
            .order_by(StagedArtifact.created_at)
            .limit(1)
        )
        if existing:
            return existing
        storage_key = f"staged/{checksum}.pdf"
        metadata_key = f"staged/{checksum}.json"
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as output:
                temporary_path = Path(output.name)
                with path.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            DiscoveryDownloadStorage(self.settings).store(
                temporary_path,
                storage_key,
                metadata_key,
                {
                    "path": path.name,
                    "source_type": source_type,
                    "source_external_id": source_external_id,
                    "sha256": checksum,
                    "size_bytes": size,
                    "metadata": metadata_payload or {},
                },
            )
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
        artifact = StagedArtifact(
            artifact_id=new_id("artifact"),
            storage_provider=self.settings.discovery_storage_provider,
            storage_key=storage_key,
            metadata_key=metadata_key,
            sha256=checksum,
            size_bytes=size,
            source_type=source_type,
            source_external_id=source_external_id,
            status="available",
            metadata_payload=metadata_payload or {},
        )
        self.db.add(artifact)
        self.db.flush()
        return artifact


class ImportRegistrationService:
    def __init__(self, db, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def register(self, batch_item_id: str) -> RegistrationResult:
        item = self.db.get(ImportBatchItem, batch_item_id)
        if not item:
            raise ValueError("Import batch item was not found.")
        if item.paper_id:
            paper = self.db.get(Paper, item.paper_id)
            if not paper:
                raise ValueError("Registered Paper was not found.")
            grant_paper_access(
                self.db,
                paper.paper_id,
                self.access_scope(item, None),
            )
            return RegistrationResult(
                paper, True, self.completed_paper_stages(paper)
            )
        artifact = (
            self.db.get(StagedArtifact, item.staged_artifact_id)
            if item.staged_artifact_id
            else None
        )
        if not artifact or artifact.status not in {"available", "registered"}:
            raise ValueError("A valid staged PDF artifact is required.")
        if artifact.paper_id:
            paper = self.db.get(Paper, artifact.paper_id)
            if not paper:
                raise ValueError("Artifact Paper was not found.")
            item.paper_id = paper.paper_id
            item.duplicate_of_paper_id = paper.paper_id
            grant_paper_access(
                self.db,
                paper.paper_id,
                self.access_scope(item, artifact),
            )
            return RegistrationResult(
                paper, True, self.completed_paper_stages(paper)
            )
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as output:
                temporary_path = Path(output.name)
            DiscoveryDownloadStorage(
                self.settings,
                provider=artifact.storage_provider,
            ).download_to_path(artifact.storage_key, temporary_path)
            with temporary_path.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError("The staged artifact is not a valid PDF.")
            metadata = self.normalized_metadata(
                artifact.source_type,
                artifact.metadata_payload or item.metadata_payload or {},
            )
            with temporary_path.open("rb") as stream:
                result = IngestionService(
                    self.db,
                    self.settings.storage_root,
                    self.settings.max_upload_bytes,
                ).ingest_pdf_stream(
                    stream,
                    f"{artifact.source_external_id or artifact.artifact_id}.pdf",
                    title=metadata.get("title"),
                    metadata=metadata,
                    source_provenance=artifact.source_type,
                )
            paper = self.db.get(Paper, result.paper_id)
            if not paper:
                raise RuntimeError("Artifact registration did not create a Paper.")
            duplicate = result.status == "duplicate"
            artifact.paper_id = paper.paper_id
            artifact.status = "registered"
            item.paper_id = paper.paper_id
            item.duplicate_of_paper_id = paper.paper_id if duplicate else None
            item.details = {
                **dict(item.details or {}),
                "duplicate": duplicate,
                "staged_artifact_id": artifact.artifact_id,
            }
            grant_paper_access(
                self.db,
                paper.paper_id,
                self.access_scope(item, artifact),
            )
            return RegistrationResult(
                paper, duplicate, self.completed_paper_stages(paper)
            )
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def access_scope(
        item: ImportBatchItem,
        artifact: StagedArtifact | None,
    ) -> str | None:
        for payload in (
            artifact.metadata_payload if artifact else None,
            item.metadata_payload,
            item.details,
        ):
            if isinstance(payload, dict) and payload.get("_access_scope"):
                return str(payload["_access_scope"])
        return None

    @staticmethod
    def normalized_metadata(source_type: str, payload: dict) -> dict:
        if source_type != "openalex_harvest":
            return {
                key: value
                for key, value in dict(payload).items()
                if not key.startswith("_")
            }
        work = payload
        doi = str(work.get("doi") or "") or None
        if doi:
            doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
        openalex_id = str(work.get("id") or "") or None
        if openalex_id:
            openalex_id = openalex_id.rstrip("/").rsplit("/", 1)[-1]
        return {
            "title": work.get("title"),
            "doi": doi,
            "openalex_id": openalex_id,
            "year": work.get("publication_year"),
            "publication_date": work.get("publication_date"),
            "publication_type": work.get("type"),
            "language": work.get("language"),
            "authors": [
                {
                    "name": ((row.get("author") or {}).get("display_name")),
                    "orcid": ((row.get("author") or {}).get("orcid")),
                }
                for row in work.get("authorships") or []
            ],
            "license": ((work.get("best_oa_location") or {}).get("license")),
            "open_access": ((work.get("open_access") or {}).get("is_oa")),
            "source_url": (
                (work.get("primary_location") or {}).get("landing_page_url")
            ),
        }

    @staticmethod
    def completed_paper_stages(paper: Paper) -> set[str]:
        completed: set[str] = set()
        if paper.parse_status != "succeeded":
            return completed
        completed.add("parse")
        if paper.evidence_status != "ready":
            return completed
        completed.add("evidence")
        if paper.multimodal_status != "ready":
            return completed
        completed.add("resource_context")
        if paper.embedding_status != "ready":
            return completed
        completed.add("vectorize")
        if paper.index_status == "ready":
            completed.add("index")
        return completed
