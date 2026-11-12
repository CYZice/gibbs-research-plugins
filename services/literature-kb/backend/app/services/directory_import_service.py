from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.ids import new_id
from app.models.import_batch import ImportBatch, ImportBatchItem
from app.services.pipeline_batch_service import PipelineBatchService
from app.services.pipeline_service import PIPELINE_VERSION, reconcile_import_batch
from app.services.staged_artifact_service import StagedArtifactService


class DirectoryImportService:
    """Stages a local PDF directory and submits it to the current Pipeline."""

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def import_directory(
        self, source: str, manifest_path: str | None = None
    ) -> ImportBatch:
        if "://" in source:
            raise ValueError(
                "Remote URIs are not configured. Use a local or mounted directory."
            )
        root = Path(source).resolve()
        if not root.is_dir():
            raise ValueError("Batch source must be a readable directory.")
        manifest = self._manifest(Path(manifest_path).resolve()) if manifest_path else {}
        pipeline = PipelineBatchService(self.db)
        batch = pipeline.create_staged_batch(source=str(root), priority=50)
        self.db.flush()
        entries = []
        for path in sorted(root.rglob("*.pdf")):
            if not path.is_file():
                continue
            source_key = path.relative_to(root).as_posix()
            metadata = manifest.get(source_key) or {}
            try:
                artifact = StagedArtifactService(
                    self.db, self.settings
                ).stage_local_file(
                    path,
                    source_type="directory_import",
                    source_external_id=source_key,
                    metadata_payload=metadata,
                )
                entries.append(
                    {
                        "artifact": artifact,
                        "source_key": source_key,
                        "metadata_payload": metadata,
                    }
                )
            except Exception as exc:
                self.db.add(
                    ImportBatchItem(
                        item_id=new_id("batchitem"),
                        batch_id=batch.batch_id,
                        source_key=source_key,
                        status="failed",
                        attempts=0,
                        metadata_payload=metadata,
                        details={
                            "stage": "staging",
                            "error_code": type(exc).__name__,
                        },
                        error_message=str(exc),
                        current_stage="staging",
                        pipeline_version=PIPELINE_VERSION,
                        finished_at=datetime.now(UTC),
                    )
                )
        pipeline.append_staged_artifacts(batch, entries)
        batch.intake_closed = True
        batch.expansion_status = "completed"
        reconcile_import_batch(self.db, batch.batch_id)
        self.db.commit()
        return batch

    @staticmethod
    def _manifest(path: Path) -> dict[str, dict]:
        if not path.is_file():
            raise ValueError("Manifest must be a readable JSONL file.")
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            key = value.pop("path", None)
            if not isinstance(key, str) or not key:
                raise ValueError("Every manifest row requires a path.")
            values[key.replace("\\", "/")] = value
        return values
