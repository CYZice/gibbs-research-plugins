from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.core.ids import new_id
from app.models.import_batch import ImportBatch, ImportBatchItem
from app.models.paper_task import PaperTask
from app.models.staged_artifact import StagedArtifact
from app.schemas.v1 import LiteratureImportItem
from app.services.pipeline_service import (
    PIPELINE_STAGE_ORDER,
    PIPELINE_VERSION,
    new_pipeline_tasks,
    reconcile_import_batch,
    retry_pipeline_from_stage,
)


@dataclass(slots=True)
class PreparedRemoteItem:
    item_id: str
    source_key: str
    idempotency_key: str
    payload: dict
    root: PaperTask
    first_stage: PaperTask


def remote_item_key(payload: dict) -> tuple[str, str]:
    source_key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return source_key, sha256(source_key.encode("utf-8")).hexdigest()


class PipelineBatchService:
    def __init__(self, db) -> None:
        self.db = db

    def create_remote_batch(
        self,
        *,
        source: str = "remote:auto",
        priority: int = 50,
        manifest_storage_key: str | None = None,
    ) -> ImportBatch:
        batch = ImportBatch(
            batch_id=new_id("batch"),
            source=source,
            status="expanding" if manifest_storage_key else "draft",
            total_items=0,
            completed_items=0,
            failed_items=0,
            priority=priority,
            intake_closed=False,
            manifest_storage_key=manifest_storage_key,
            expanded_items=0,
            expansion_status="pending" if manifest_storage_key else None,
            details={"kind": "remote_fetch", "pipeline_version": PIPELINE_VERSION},
        )
        self.db.add(batch)
        return batch

    def append_remote_items(
        self, batch: ImportBatch, payloads: list[dict]
    ) -> list[str]:
        prepared = self._prepare(batch, payloads)
        if not prepared:
            return []
        # The batch may have been created in this transaction. Flush it before
        # the Core INSERT below so PostgreSQL can satisfy the item foreign key.
        self.db.flush()
        rows = [
            {
                "item_id": item.item_id,
                "batch_id": batch.batch_id,
                "source_key": item.source_key,
                "paper_id": None,
                "status": "pending",
                "attempts": 0,
                "metadata_payload": item.payload,
                "details": {"stage": "remote_fetch"},
                "pipeline_task_id": item.root.task_id,
                "current_stage": "remote_fetch",
                "pipeline_version": PIPELINE_VERSION,
                "idempotency_key": item.idempotency_key,
            }
            for item in prepared
        ]
        inserted_ids = set(
            self.db.scalars(
                insert(ImportBatchItem)
                .values(rows)
                .on_conflict_do_nothing(
                    constraint="uq_import_batch_items_source"
                )
                .returning(ImportBatchItem.item_id)
            ).all()
        )
        tasks = [
            task
            for item in prepared
            if item.item_id in inserted_ids
            for task in (item.root, item.first_stage)
        ]
        self.db.add_all(tasks)
        inserted_count = len(inserted_ids)
        batch.total_items += inserted_count
        batch.expanded_items += inserted_count
        if inserted_count:
            batch.status = "running"
            batch.expansion_status = "running"
        return [item.root.task_id for item in prepared if item.item_id in inserted_ids]

    def expand_next_manifest(self) -> ImportBatch | None:
        batch = self.db.scalar(
            select(ImportBatch)
            .where(
                ImportBatch.manifest_storage_key.is_not(None),
                ImportBatch.intake_closed.is_(False),
                ImportBatch.expansion_status.in_(("pending", "running")),
            )
            .order_by(ImportBatch.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not batch:
            return None
        settings = get_settings()
        path = (
            Path(settings.storage_root).resolve() / batch.manifest_storage_key
        ).resolve()
        root = Path(settings.storage_root).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            batch.status = "failed"
            batch.expansion_status = "failed"
            batch.expansion_error = "Manifest file is missing."
            batch.intake_closed = True
            return batch

        cursor = int(batch.expansion_cursor or 0)
        try:
            rows, done = self._manifest_chunk(
                path, cursor, settings.pipeline_expand_chunk_size
            )
        except Exception as exc:
            batch.status = "failed"
            batch.expansion_status = "failed"
            batch.expansion_error = (
                f"Manifest expansion failed: {type(exc).__name__}"
            )
            batch.intake_closed = True
            return batch
        valid = []
        invalid = []
        for row_number, raw in rows:
            if isinstance(raw, Exception):
                invalid.append(
                    (row_number, type(raw).__name__, "Manifest item is invalid.")
                )
                continue
            try:
                valid.append(
                    LiteratureImportItem.model_validate(raw).model_dump()
                )
            except Exception as exc:
                invalid.append(
                    (row_number, type(exc).__name__, "Manifest item is invalid.")
                )
        self.append_remote_items(batch, valid)
        self._insert_invalid_rows(batch, invalid)
        batch.expansion_cursor = str(cursor + len(rows))
        batch.expansion_status = "completed" if done else "running"
        if done:
            batch.intake_closed = True
            reconcile_import_batch(self.db, batch.batch_id)
        return batch

    def create_staged_batch(
        self,
        *,
        source: str,
        priority: int = 50,
    ) -> ImportBatch:
        batch = ImportBatch(
            batch_id=new_id("batch"),
            source=source,
            status="draft",
            total_items=0,
            completed_items=0,
            failed_items=0,
            priority=priority,
            intake_closed=False,
            expanded_items=0,
            details={"kind": "staged", "pipeline_version": PIPELINE_VERSION},
        )
        self.db.add(batch)
        return batch

    def append_staged_artifacts(
        self,
        batch: ImportBatch,
        entries: list[dict],
    ) -> list[str]:
        if not entries:
            return []
        self.db.flush()
        accepted = []
        seen = set()
        for entry in entries:
            artifact: StagedArtifact = entry["artifact"]
            source_key = str(entry.get("source_key") or artifact.storage_key)
            idempotency_key = sha256(
                f"staged:{artifact.artifact_id}".encode("utf-8")
            ).hexdigest()
            if source_key in seen:
                continue
            seen.add(source_key)
            existing = self.db.scalar(
                select(ImportBatchItem).where(
                    ImportBatchItem.batch_id == batch.batch_id,
                    ImportBatchItem.source_key == source_key,
                )
            )
            if existing:
                continue
            item_id = new_id("batchitem")
            root, first_stage = new_pipeline_tasks(
                paper_id=artifact.paper_id,
                initial_stage="register",
                provider=artifact.source_type,
                batch_id=batch.batch_id,
                batch_item_id=item_id,
                priority=batch.priority,
                details={"staged_artifact_id": artifact.artifact_id},
            )
            item = ImportBatchItem(
                item_id=item_id,
                batch_id=batch.batch_id,
                source_key=source_key,
                paper_id=artifact.paper_id,
                staged_artifact_id=artifact.artifact_id,
                status="pending",
                metadata_payload=(
                    entry.get("metadata_payload")
                    or artifact.metadata_payload
                    or {}
                ),
                details={
                    "stage": "register",
                    "staged_artifact_id": artifact.artifact_id,
                },
                pipeline_task_id=root.task_id,
                current_stage="register",
                pipeline_version=PIPELINE_VERSION,
                idempotency_key=idempotency_key,
            )
            self.db.add_all((item, root, first_stage))
            accepted.append(root.task_id)
        batch.total_items += len(accepted)
        batch.expanded_items += len(accepted)
        if accepted:
            batch.status = "running"
            batch.expansion_status = "running"
        return accepted

    def retry_failed_items(self, batch: ImportBatch) -> int:
        rows = self.db.scalars(
            select(ImportBatchItem)
            .where(
                ImportBatchItem.batch_id == batch.batch_id,
                ImportBatchItem.status == "failed",
                ImportBatchItem.pipeline_version == PIPELINE_VERSION,
            )
            .order_by(ImportBatchItem.created_at)
            .with_for_update(skip_locked=True)
        ).all()
        retried = 0
        for item in rows:
            root = (
                self.db.get(PaperTask, item.pipeline_task_id)
                if item.pipeline_task_id
                else None
            )
            if not root:
                continue
            failed_stage = self.db.scalar(
                select(PaperTask)
                .where(
                    PaperTask.root_task_id == root.task_id,
                    PaperTask.pipeline_version == PIPELINE_VERSION,
                    PaperTask.pipeline_generation == root.pipeline_generation,
                    PaperTask.operation.in_(PIPELINE_STAGE_ORDER),
                    PaperTask.status == "failed",
                )
                .order_by(PaperTask.finished_at.desc().nullslast())
                .limit(1)
            )
            if not failed_stage:
                continue
            retry_pipeline_from_stage(
                self.db, task_id=failed_stage.task_id, priority=batch.priority
            )
            retried += 1
        if retried:
            batch.status = "running"
            reconcile_import_batch(self.db, batch.batch_id)
        return retried

    @staticmethod
    def _manifest_chunk(
        path: Path, cursor: int, limit: int
    ) -> tuple[list[tuple[int, dict | Exception]], bool]:
        suffix = path.suffix.lower()
        if suffix not in {".jsonl", ".csv"}:
            raise ValueError("Manifest must be JSONL or CSV.")
        rows: list[tuple[int, dict | Exception]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            processed = 0
            values = (
                (
                    row_number,
                    line,
                )
                for row_number, line in enumerate(stream, start=1)
                if line.strip()
            ) if suffix == ".jsonl" else enumerate(
                csv.DictReader(stream), start=1
            )
            for row_number, value in values:
                if processed < cursor:
                    processed += 1
                    continue
                if len(rows) >= limit:
                    return rows, False
                processed += 1
                try:
                    raw = (
                        json.loads(value)
                        if suffix == ".jsonl"
                        else PipelineBatchService._csv_payload(value)
                    )
                except (csv.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
                    raw = exc
                rows.append((row_number, raw))
        return rows, True

    @staticmethod
    def _csv_payload(row: dict[str, str | None]) -> dict:
        payload = {
            key: value.strip()
            for key, value in row.items()
            if key and isinstance(value, str) and value.strip()
        }
        if "options" in payload:
            payload["options"] = json.loads(payload["options"])
        if "license_acknowledged" in payload:
            payload["license_acknowledged"] = payload[
                "license_acknowledged"
            ].lower() in {"1", "true", "yes"}
        return payload

    def _insert_invalid_rows(
        self,
        batch: ImportBatch,
        invalid: list[tuple[int, str, str]],
    ) -> None:
        if not invalid:
            return
        rows = []
        for row_number, error_code, error_message in invalid:
            source_key = f"manifest-row:{row_number}"
            rows.append(
                {
                    "item_id": new_id("batchitem"),
                    "batch_id": batch.batch_id,
                    "source_key": source_key,
                    "status": "failed",
                    "attempts": 0,
                    "details": {
                        "stage": "validation",
                        "error_code": error_code,
                    },
                    "error_message": error_message,
                    "current_stage": "validation",
                    "pipeline_version": PIPELINE_VERSION,
                    "finished_at": datetime.now(UTC),
                    "idempotency_key": sha256(
                        source_key.encode("utf-8")
                    ).hexdigest(),
                }
            )
        inserted = len(
            self.db.scalars(
                insert(ImportBatchItem)
                .values(rows)
                .on_conflict_do_nothing(
                    constraint="uq_import_batch_items_source"
                )
                .returning(ImportBatchItem.item_id)
            ).all()
        )
        batch.total_items += inserted
        batch.expanded_items += inserted
        batch.failed_items += inserted

    def _prepare(
        self, batch: ImportBatch, payloads: list[dict]
    ) -> list[PreparedRemoteItem]:
        prepared = []
        seen = set()
        for payload in payloads:
            source_key, idempotency_key = remote_item_key(payload)
            if idempotency_key in seen:
                continue
            seen.add(idempotency_key)
            item_id = new_id("batchitem")
            provider = str(
                payload.get("preferred_source")
                or payload.get("source")
                or "auto"
            )
            root, first_stage = new_pipeline_tasks(
                paper_id=None,
                initial_stage="remote_fetch",
                provider=provider,
                batch_id=batch.batch_id,
                batch_item_id=item_id,
                priority=batch.priority,
                details={"remote": payload},
            )
            prepared.append(
                PreparedRemoteItem(
                    item_id=item_id,
                    source_key=source_key,
                    idempotency_key=idempotency_key,
                    payload=payload,
                    root=root,
                    first_stage=first_stage,
                )
            )
        return prepared
