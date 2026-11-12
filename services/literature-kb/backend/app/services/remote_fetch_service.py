from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.import_batch import ImportBatchItem
from app.models.paper import Paper
from app.models.paper_task import PaperTask
from app.models.staged_artifact import StagedArtifact
from app.services.discovery_service import DiscoveryError, DiscoveryService
from app.services.ingestion_service import IngestionService
from app.services.pdf_download_service import PdfDownloadError, PdfDownloadService
from app.services.staged_artifact_service import StagedArtifactService


@dataclass(slots=True)
class RemoteFetchResult:
    artifact: StagedArtifact | None
    paper: Paper | None
    duplicate: bool
    selected_source: str


class RemoteFetchService:
    """Resolve a remote work and produce either a staged PDF or an existing Paper."""

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def run(self, task: PaperTask) -> RemoteFetchResult:
        item = self.db.get(ImportBatchItem, task.batch_item_id)
        if not item:
            raise ValueError("Remote fetch task is missing its batch item.")
        if item.paper_id:
            paper = self.db.get(Paper, item.paper_id)
            if not paper:
                raise ValueError("Remote fetch Paper was not found.")
            return RemoteFetchResult(None, paper, True, task.provider or "existing")
        if item.staged_artifact_id:
            artifact = self.db.get(StagedArtifact, item.staged_artifact_id)
            if artifact and artifact.status in {"available", "registered"}:
                return RemoteFetchResult(
                    artifact,
                    self.db.get(Paper, artifact.paper_id) if artifact.paper_id else None,
                    bool(artifact.paper_id),
                    artifact.source_type,
                )

        remote = dict((task.details or {}).get("remote") or {})
        references = list(remote.get("options") or [])
        source = remote.get("source")
        external_id = remote.get("external_id")
        preferred_source = remote.get("preferred_source")
        if external_id:
            references = [
                {"source": source or "openalex", "external_id": external_id}
            ]
            preferred_source = source or "openalex"
        if preferred_source:
            references = [
                row for row in references if row.get("source") == preferred_source
            ]
        if not references:
            raise DiscoveryError(
                "INVALID_IDENTIFIER", "No remote import source was provided.", 400
            )

        discovery = DiscoveryService(self.settings)
        attempts = []
        last_error = None
        grouped: dict[str, list[dict]] = {}
        for reference in references:
            grouped.setdefault(str(reference.get("source") or ""), []).append(
                reference
            )
        resolved_sources = []
        for source_name, source_references in grouped.items():
            resolved = []
            source_error = None
            seen = set()
            for reference in source_references:
                reference_id = str(reference.get("external_id") or "")
                if reference_id in seen:
                    continue
                seen.add(reference_id)
                try:
                    paper = discovery.resolve(
                        source=source_name, external_id=reference_id
                    )
                    candidate = self.best_candidate(discovery, paper)
                    resolved.append(
                        (
                            discovery.source_candidate_rank(candidate, source_name),
                            paper,
                        )
                    )
                except (DiscoveryError, PdfDownloadError) as exc:
                    source_error = last_error = exc
            if resolved:
                resolved.sort(key=lambda row: row[0])
                resolved_sources.append((resolved[0][0], source_name, resolved))
            else:
                attempts.append(
                    {
                        "source": source_name,
                        "status": "failed",
                        "error_code": (
                            source_error.code
                            if source_error
                            else "EXTERNAL_WORK_NOT_FOUND"
                        ),
                    }
                )
                if preferred_source and source_error:
                    raise source_error

        for _, source_name, resolved in sorted(resolved_sources, key=lambda row: row[0]):
            for _, paper in resolved:
                try:
                    result = self.fetch_resolved(item, discovery, paper, source_name)
                    attempts.append({"source": source_name, "status": "succeeded"})
                    task.details = {
                        **dict(task.details or {}),
                        "source_attempts": attempts,
                        "selected_source": source_name,
                    }
                    return result
                except (DiscoveryError, PdfDownloadError) as exc:
                    last_error = exc
            attempts.append(
                {
                    "source": source_name,
                    "status": "failed",
                    "error_code": getattr(last_error, "code", "REMOTE_FETCH_FAILED"),
                }
            )
            if preferred_source and last_error:
                raise last_error
        task.details = {**dict(task.details or {}), "source_attempts": attempts}
        if last_error:
            raise last_error
        raise DiscoveryError(
            "NO_DOWNLOAD_CANDIDATE", "No remote source could be fetched.", 404
        )

    def fetch_resolved(
        self,
        item: ImportBatchItem,
        discovery: DiscoveryService,
        paper,
        source: str,
    ) -> RemoteFetchResult:
        metadata = self.metadata(paper)
        ingestion = IngestionService(
            self.db, self.settings.storage_root, self.settings.max_upload_bytes
        )
        existing = ingestion.find_existing(metadata)
        if existing:
            ingestion.merge_existing(existing, metadata, source)
            item.paper_id = existing.paper_id
            item.duplicate_of_paper_id = existing.paper_id
            item.metadata_payload = metadata
            return RemoteFetchResult(None, existing, True, source)

        last_error = None
        downloaded = None
        for candidate in discovery.import_candidates(paper):
            try:
                downloaded = PdfDownloadService(self.settings).download(
                    paper.external_id,
                    candidate,
                    doi=paper.doi,
                    title=paper.title,
                )
                break
            except PdfDownloadError as exc:
                last_error = exc
        if downloaded is None:
            if last_error:
                raise last_error
            raise DiscoveryError(
                "NO_DOWNLOAD_CANDIDATE",
                "No remote PDF candidate could be downloaded.",
                404,
            )
        artifact = StagedArtifactService(self.db, self.settings).from_download(
            downloaded,
            source_type=source,
            source_external_id=paper.external_id,
            metadata_payload=metadata,
        )
        item.staged_artifact_id = artifact.artifact_id
        item.metadata_payload = metadata
        item.details = {
            **dict(item.details or {}),
            "staged_artifact_id": artifact.artifact_id,
            "selected_source": source,
        }
        return RemoteFetchResult(artifact, None, False, source)

    @staticmethod
    def best_candidate(discovery: DiscoveryService, paper):
        if not paper.download_candidates:
            return None
        return sorted(
            paper.download_candidates, key=discovery.import_candidate_rank
        )[0]

    @staticmethod
    def metadata(paper) -> dict:
        return {
            "title": paper.title,
            "authors": paper.authors,
            "year": paper.year,
            "publication_date": (
                paper.publication_date.isoformat()
                if paper.publication_date
                else None
            ),
            "doi": paper.doi,
            "arxiv_id": paper.arxiv_id,
            "pmid": paper.pmid,
            "pmcid": paper.pmcid,
            f"{paper.source}_id": paper.external_id,
            "venue": paper.venue,
            "abstract": paper.abstract,
            "publication_type": paper.publication_type,
            "language": paper.language,
            "license": next(
                (
                    item.license
                    for item in paper.download_candidates
                    if item.license
                ),
                None,
            ),
            "open_access": paper.open_access,
            "source_url": RemoteFetchService.clean_url(paper.landing_page_url),
        }

    @staticmethod
    def clean_url(value: str | None) -> str | None:
        if not value:
            return None
        from urllib.parse import urlsplit, urlunsplit

        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
