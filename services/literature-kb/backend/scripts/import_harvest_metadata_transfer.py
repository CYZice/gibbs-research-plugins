from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select

from app.core.ids import new_id
from app.db.session import SessionLocal
from app.models.harvest import CorpusDocument, HarvestJob, HarvestJobItem


def _update_document(document: CorpusDocument, source: dict) -> None:
    document.provider = source["provider"]
    document.doi = source.get("doi")
    document.title = source.get("title")
    document.year = source.get("year")
    document.publication_type = source.get("publication_type")
    document.language = source.get("language")
    document.metadata_payload = source["metadata_payload"]
    document.candidate_urls = source.get("candidate_urls")
    document.content_url = source.get("content_url")


def _flush_batch(db, records: list[dict]) -> int:
    if not records:
        return 0
    external_ids = [record["document"]["external_id"] for record in records]
    documents = {
        document.external_id: document
        for document in db.scalars(
            select(CorpusDocument).where(CorpusDocument.external_id.in_(external_ids))
        ).all()
    }
    item_documents: dict[str, list[tuple[dict, CorpusDocument]]] = defaultdict(list)
    for record in records:
        source = record["document"]
        document = documents.get(source["external_id"])
        if document is None:
            document = CorpusDocument(
                document_id=new_id("corpus"),
                provider=source["provider"],
                external_id=source["external_id"],
                metadata_payload=source["metadata_payload"],
            )
            _update_document(document, source)
            db.add(document)
            documents[document.external_id] = document
        else:
            _update_document(document, source)
        item_documents[record["job_id"]].append((record, document))
    db.flush()

    inserted = 0
    for job_id, pairs in item_documents.items():
        document_ids = [document.document_id for _, document in pairs]
        existing = {
            item.document_id: item
            for item in db.scalars(
                select(HarvestJobItem).where(
                    HarvestJobItem.job_id == job_id,
                    HarvestJobItem.document_id.in_(document_ids),
                )
            ).all()
        }
        for record, document in pairs:
            if document.document_id in existing:
                continue
            db.add(
                HarvestJobItem(
                    item_id=new_id("harvestitem"),
                    job_id=job_id,
                    document_id=document.document_id,
                    status="staged" if document.pdf_storage_key else "metadata_ready",
                    details=record.get("item_details") or {},
                )
            )
            inserted += 1
    db.commit()
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import an exported OpenAlex harvest metadata transfer package."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 5000:
        raise SystemExit("--batch-size must be between 1 and 5000")

    db = SessionLocal()
    batch: list[dict] = []
    imported = 0
    jobs: dict[str, HarvestJob] = {}
    try:
        with gzip.open(args.input, "rt", encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if record["record_type"] == "job":
                    source = record["job"]
                    job = db.get(HarvestJob, source["job_id"])
                    if job is None:
                        job = HarvestJob(
                            job_id=source["job_id"],
                            name=source["name"],
                            provider=source["provider"],
                            scope=source["scope"],
                            filters=source["filters"],
                            taxonomy_snapshot=source.get("taxonomy_snapshot"),
                            billing_mode=source["billing_mode"],
                            ingest_mode="metadata_only",
                            next_cursor=None,
                            enumeration_complete=True,
                            estimated_items=source.get("estimated_items"),
                            details=source.get("details"),
                        )
                        db.add(job)
                    jobs[job.job_id] = job
                    continue
                batch.append(record)
                if len(batch) >= args.batch_size:
                    imported += _flush_batch(db, batch)
                    batch.clear()
            imported += _flush_batch(db, batch)
            batch.clear()

        for job in jobs.values():
            job.enumeration_complete = True
            job.status = "completed"
            job.enumerated_items = db.scalar(
                select(func.count())
                .select_from(HarvestJobItem)
                .where(HarvestJobItem.job_id == job.job_id)
            )
        db.commit()
    finally:
        db.close()

    print(f"imported_items={imported}")
    print("jobs=" + ",".join(sorted(jobs)))


if __name__ == "__main__":
    main()
