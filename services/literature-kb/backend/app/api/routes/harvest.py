from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.models.harvest import CorpusDocument, HarvestJob, HarvestJobItem
from app.models.import_batch import ImportBatchItem
from app.schemas.harvest import (
    HarvestContentExecuteRequest,
    HarvestContentRequest,
    HarvestCreateRequest,
    HarvestEstimateRequest,
    HarvestFreeDownloadExecuteRequest,
    HarvestFreeDownloadRequest,
    HarvestImportRequest,
)
from app.services.harvest_service import HarvestError, HarvestService, job_payload
from app.services.harvest_tasks import (
    enqueue_harvest_content_batch,
    enqueue_harvest_download,
    enqueue_harvest_expand,
    enqueue_harvest_free_download_dispatch,
)

router = APIRouter(prefix="/api/v1/harvest", tags=["Corpus Harvest"])


def _enabled() -> None:
    if not get_settings().harvest_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "HARVEST_DISABLED",
                "message": "Corpus harvest is disabled.",
            },
        )


def _raise(exc: HarvestError):
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.get("/openalex/taxonomy/search")
def taxonomy_search(
    q: str = Query(min_length=1, max_length=200),
    level: str | None = Query(default=None, pattern="^(domain|field|subfield|topic)$"),
    db: Session = Depends(get_db),
):
    _enabled()
    try:
        result = HarvestService(db, get_settings()).taxonomy_search(q, level)
        db.commit()
        return {"items": result}
    except HarvestError as exc:
        _raise(exc)


@router.get("/openalex/taxonomy/{level}/{openalex_id}/children")
def taxonomy_children(
    level: str,
    openalex_id: str,
    db: Session = Depends(get_db),
):
    _enabled()
    if level not in {"domain", "field", "subfield", "topic"}:
        raise HTTPException(status_code=400, detail="Invalid taxonomy level.")
    try:
        result = {
            "items": HarvestService(db, get_settings()).taxonomy_children(
                level, openalex_id
            )
        }
        db.commit()
        return result
    except HarvestError as exc:
        _raise(exc)


@router.post("/jobs/estimate")
def estimate_job(body: HarvestEstimateRequest, db: Session = Depends(get_db)):
    _enabled()
    try:
        result = HarvestService(db, get_settings()).estimate(body)
        db.commit()
        return result
    except HarvestError as exc:
        _raise(exc)


@router.post("/jobs", status_code=202)
def create_job(body: HarvestCreateRequest, db: Session = Depends(get_db)):
    _enabled()
    try:
        job = HarvestService(db, get_settings()).create_job(body)
        db.commit()
        db.refresh(job)
        enqueue_harvest_expand(job.job_id)
        return job_payload(job)
    except HarvestError as exc:
        db.rollback()
        _raise(exc)


@router.get("/jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    _enabled()
    rows = db.scalars(
        select(HarvestJob).order_by(HarvestJob.created_at.desc()).limit(limit)
    ).all()
    return {"items": [job_payload(row) for row in rows]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    payload = job_payload(job)
    payload["quota"] = HarvestService(
        db, get_settings()
    ).credentials.quota_summary(db)
    return payload


@router.get("/jobs/{job_id}/items")
def get_job_items(
    job_id: str,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    include_metadata: bool = False,
    db: Session = Depends(get_db),
):
    _enabled()
    if not db.get(HarvestJob, job_id):
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    statement = (
        select(HarvestJobItem, CorpusDocument, ImportBatchItem)
        .join(CorpusDocument, CorpusDocument.document_id == HarvestJobItem.document_id)
        .outerjoin(
            ImportBatchItem,
            ImportBatchItem.item_id == HarvestJobItem.import_batch_item_id,
        )
        .where(HarvestJobItem.job_id == job_id)
        .order_by(HarvestJobItem.item_id)
    )
    if status:
        statement = statement.where(HarvestJobItem.status == status)
    if cursor:
        statement = statement.where(HarvestJobItem.item_id > cursor)
    rows = db.execute(statement.limit(limit + 1)).all()
    next_cursor = rows[limit - 1][0].item_id if len(rows) > limit else None
    return {
        "items": [
            {
                "item_id": item.item_id,
                "document_id": document.document_id,
                "openalex_id": document.external_id,
                "doi": document.doi,
                "title": document.title,
                "year": document.year,
                "status": item.status,
                "attempts": item.attempts,
                "selected_source": item.selected_source,
                "error_code": item.error_code,
                "error_message": item.error_message,
                "pdf_storage_key": document.pdf_storage_key,
                "pdf_size_bytes": document.pdf_size_bytes,
                "registered_paper_id": item.registered_paper_id,
                "import_batch_item_id": item.import_batch_item_id,
                "pipeline_task_id": (
                    batch_item.pipeline_task_id if batch_item else None
                ),
                "pipeline_status": batch_item.status if batch_item else None,
                "pipeline_stage": batch_item.current_stage if batch_item else None,
                "content_eligible": bool((item.details or {}).get("content_eligible")),
                "content_status": (item.details or {}).get("content_status"),
                "content_reason": (item.details or {}).get("content_reason"),
                "source_availability": (item.details or {}).get("source_availability"),
                "download_candidates": document.candidate_urls or [],
                "content_url": document.content_url,
                **(
                    {"metadata_payload": document.metadata_payload}
                    if include_metadata
                    else {}
                ),
            }
            for item, document, batch_item in rows[:limit]
        ],
        "next_cursor": next_cursor,
    }


@router.post("/jobs/{job_id}/content/estimate")
def estimate_content_downloads(
    job_id: str,
    body: HarvestContentRequest,
    db: Session = Depends(get_db),
):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    return HarvestService(db, get_settings()).estimate_content_downloads(
        job,
        selection=body.selection,
        item_ids=body.item_ids,
    )


@router.post("/jobs/{job_id}/content/execute", status_code=202)
def execute_content_downloads(
    job_id: str,
    body: HarvestContentExecuteRequest,
    db: Session = Depends(get_db),
):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    try:
        item_ids = HarvestService(db, get_settings()).request_content_downloads(
            job,
            selection=body.selection,
            item_ids=body.item_ids,
        )
    except HarvestError as exc:
        db.rollback()
        _raise(exc)
    enqueue_harvest_content_batch(item_ids[0], delay_seconds=0)
    return {"job": job_payload(job), "queued_items": len(item_ids)}


@router.post("/jobs/{job_id}/free-downloads/estimate")
def estimate_free_downloads(
    job_id: str,
    body: HarvestFreeDownloadRequest,
    db: Session = Depends(get_db),
):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    return HarvestService(db, get_settings()).estimate_free_downloads(
        job,
        selection=body.selection,
        item_ids=body.item_ids,
    )


@router.post("/jobs/{job_id}/free-downloads/execute", status_code=202)
def execute_free_downloads(
    job_id: str,
    body: HarvestFreeDownloadExecuteRequest,
    db: Session = Depends(get_db),
):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    try:
        queued_items = HarvestService(db, get_settings()).request_free_downloads(
            job,
            selection=body.selection,
            item_ids=body.item_ids,
        )
    except HarvestError as exc:
        db.rollback()
        _raise(exc)
    enqueue_harvest_free_download_dispatch(job_id, delay_seconds=0)
    return {"job": job_payload(job), "queued_items": queued_items}


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: str, db: Session = Depends(get_db)):
    return _set_status(db, job_id, "paused")


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str, db: Session = Depends(get_db)):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    if job.status in {"cancelled", "completed", "completed_with_errors"}:
        raise HTTPException(status_code=409, detail="Terminal harvest job cannot be resumed.")
    job.status = "downloading" if job.enumeration_complete else "enumerating"
    job.next_retry_at = None
    for item in db.scalars(
        select(HarvestJobItem).where(
            HarvestJobItem.job_id == job_id,
            HarvestJobItem.status == "waiting_quota",
        )
    ).all():
        item.status = "pending"
    db.commit()
    if job.enumeration_complete and job.ingest_mode == "metadata_only":
        enqueue_harvest_free_download_dispatch(job_id, delay_seconds=0)
    else:
        enqueue_harvest_expand(job_id)
    return job_payload(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    job.status = "cancelled"
    for item in db.scalars(
        select(HarvestJobItem).where(
            HarvestJobItem.job_id == job_id,
            HarvestJobItem.status.in_(("pending", "waiting_quota")),
        )
    ).all():
        item.status = "cancelled"
        item.error_code = "HARVEST_CANCELLED"
    db.commit()
    return job_payload(job)


@router.post("/jobs/{job_id}/retry-failed")
def retry_failed(job_id: str, db: Session = Depends(get_db)):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    rows = db.scalars(
        select(HarvestJobItem).where(
            HarvestJobItem.job_id == job_id,
            HarvestJobItem.status.in_(("failed", "no_source")),
        )
    ).all()
    for item in rows:
        item.status = "pending" if job.ingest_mode == "metadata_only" else "resolving"
        item.error_code = None
        item.error_message = None
        item.next_attempt_at = None
    job.status = "downloading" if job.enumeration_complete else "enumerating"
    db.commit()
    if job.ingest_mode == "metadata_only":
        enqueue_harvest_free_download_dispatch(job_id, delay_seconds=0)
    else:
        for item in rows:
            enqueue_harvest_download(item.item_id)
    return {"job": job_payload(job), "retried_items": len(rows)}


@router.post("/jobs/{job_id}/imports", status_code=202)
def import_staged(
    job_id: str,
    body: HarvestImportRequest,
    db: Session = Depends(get_db),
):
    _enabled()
    settings = get_settings()
    if not settings.pipeline_enabled:
        raise HTTPException(
            status_code=503,
            detail="The import pipeline is disabled.",
        )
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    try:
        batch = HarvestService(db, settings).import_staged(
            job, body.item_ids if body.selection == "items" else None
        )
    except HarvestError as exc:
        db.rollback()
        _raise(exc)
    return {
        "batch_id": batch.batch_id,
        "status": batch.status,
        "total_items": batch.total_items,
    }


def _set_status(db: Session, job_id: str, status: str):
    _enabled()
    job = db.get(HarvestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Harvest job not found.")
    if job.status in {"cancelled", "completed", "completed_with_errors"}:
        raise HTTPException(status_code=409, detail="Terminal harvest job cannot be changed.")
    job.status = status
    db.commit()
    return job_payload(job)
