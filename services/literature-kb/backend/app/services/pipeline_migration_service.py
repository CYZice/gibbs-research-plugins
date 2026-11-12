from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.import_batch import ImportBatch, ImportBatchItem
from app.models.paper import Paper
from app.models.paper_task import PaperTask
from app.services.pipeline_service import (
    ACTIVE_ROOT_STATUSES,
    PIPELINE_ROOT_OPERATION,
    PIPELINE_VERSION,
    new_pipeline_tasks,
)


def first_incomplete_stage(paper: Paper) -> str | None:
    if paper.index_status == "ready":
        return None
    if paper.embedding_status == "ready":
        return "index"
    if paper.multimodal_status == "ready":
        return "vectorize"
    if paper.evidence_status == "ready":
        return "resource_context"
    if paper.parse_status == "succeeded":
        return "evidence"
    return "parse"


def migrate_v1_process_tasks(
    db,
    *,
    apply: bool = False,
    interrupt_running: bool = False,
    limit: int = 1000,
) -> dict:
    statuses = ["queued", "retrying"]
    if interrupt_running:
        statuses.append("running")
    statement = (
        select(PaperTask)
        .where(
            PaperTask.operation == "process",
            PaperTask.pipeline_version == 1,
            PaperTask.status.in_(statuses),
        )
        .order_by(PaperTask.created_at.desc())
        .limit(max(1, min(limit, 10000)))
    )
    if apply:
        statement = statement.with_for_update(skip_locked=True)
    candidates = db.scalars(statement).all()
    seen_papers = set()
    items = []
    migrated = 0
    for legacy in candidates:
        if not legacy.paper_id or legacy.paper_id in seen_papers:
            continue
        seen_papers.add(legacy.paper_id)
        paper = db.get(Paper, legacy.paper_id)
        if not paper:
            items.append(
                {
                    "task_id": legacy.task_id,
                    "paper_id": legacy.paper_id,
                    "action": "skip",
                    "reason": "paper_not_found",
                }
            )
            continue
        active_v2 = db.scalar(
            select(PaperTask)
            .where(
                PaperTask.paper_id == paper.paper_id,
                PaperTask.operation == PIPELINE_ROOT_OPERATION,
                PaperTask.pipeline_version == PIPELINE_VERSION,
                PaperTask.status.in_(ACTIVE_ROOT_STATUSES),
            )
            .limit(1)
        )
        if active_v2:
            items.append(
                {
                    "task_id": legacy.task_id,
                    "paper_id": paper.paper_id,
                    "action": "skip",
                    "reason": "active_v2_exists",
                    "root_task_id": active_v2.task_id,
                }
            )
            continue
        detail = dict(legacy.details or {})
        batch_item_ids = list(dict.fromkeys(
            [
                *list(detail.get("batch_item_ids") or []),
                *([detail["batch_item_id"]] if detail.get("batch_item_id") else []),
            ]
        ))
        if len(batch_item_ids) > 1:
            items.append(
                {
                    "task_id": legacy.task_id,
                    "paper_id": paper.paper_id,
                    "action": "skip",
                    "reason": "shared_batch_items_require_v1_drain",
                }
            )
            continue
        initial_stage = first_incomplete_stage(paper)
        if initial_stage is None:
            items.append(
                {
                    "task_id": legacy.task_id,
                    "paper_id": paper.paper_id,
                    "action": "skip",
                    "reason": "paper_already_ready",
                }
            )
            continue
        batch_item = (
            db.get(ImportBatchItem, batch_item_ids[0])
            if batch_item_ids
            else None
        )
        batch_id = batch_item.batch_id if batch_item else None
        preview = {
            "task_id": legacy.task_id,
            "paper_id": paper.paper_id,
            "action": "migrate",
            "initial_stage": initial_stage,
            "batch_id": batch_id,
            "batch_item_id": batch_item.item_id if batch_item else None,
        }
        items.append(preview)
        if not apply:
            continue

        now = datetime.now(UTC)
        root, stage = new_pipeline_tasks(
            paper_id=paper.paper_id,
            initial_stage=initial_stage,
            provider=legacy.provider or "migration",
            batch_id=batch_id,
            batch_item_id=batch_item.item_id if batch_item else None,
            priority=legacy.priority,
            details={
                "migration": {
                    "source_task_id": legacy.task_id,
                    "source_status": legacy.status,
                }
            },
            now=now,
        )
        legacy.status = "interrupted" if legacy.status == "running" else "superseded"
        legacy.error_code = (
            "PIPELINE_V2_CUTOVER" if legacy.status == "interrupted" else None
        )
        legacy.error_domain = "platform" if legacy.status == "interrupted" else None
        legacy.error_message = (
            "Interrupted during the explicit Pipeline V2 cutover."
            if legacy.status == "interrupted"
            else None
        )
        legacy.finished_at = now
        if batch_item:
            batch_item.status = "pending"
            batch_item.pipeline_task_id = root.task_id
            batch_item.current_stage = initial_stage
            batch_item.pipeline_version = PIPELINE_VERSION
            batch_item.finished_at = None
            batch_item.error_message = None
            batch = db.get(ImportBatch, batch_id)
            if batch:
                batch.intake_closed = True
                batch.status = "running"
        _reset_paper_statuses(paper, initial_stage)
        db.add_all((root, stage))
        preview["root_task_id"] = root.task_id
        migrated += 1

    return {
        "dry_run": not apply,
        "interrupt_running": interrupt_running,
        "candidate_count": len(candidates),
        "migrated_count": migrated,
        "items": items,
    }


def _reset_paper_statuses(paper: Paper, initial_stage: str) -> None:
    status_fields = (
        ("parse_status", "queued"),
        ("evidence_status", "not_started"),
        ("multimodal_status", "not_started"),
        ("embedding_status", "not_started"),
        ("index_status", "not_started"),
    )
    first_field = {
        "parse": 0,
        "evidence": 1,
        "resource_context": 2,
        "vectorize": 3,
        "index": 4,
    }[initial_stage]
    for field, value in status_fields[first_field:]:
        setattr(paper, field, value)
