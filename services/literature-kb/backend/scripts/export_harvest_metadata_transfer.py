from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from app.db.session import SessionLocal
from app.models.harvest import CorpusDocument, HarvestJob, HarvestJobItem


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export OpenAlex harvest metadata for a production transfer."
    )
    parser.add_argument("--job-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    exported = 0
    try:
        jobs = (
            db.query(HarvestJob)
            .filter(HarvestJob.job_id.in_(args.job_id))
            .order_by(HarvestJob.job_id)
            .all()
        )
        missing = set(args.job_id) - {job.job_id for job in jobs}
        if missing:
            raise SystemExit(f"Harvest jobs not found: {', '.join(sorted(missing))}")
        with gzip.open(args.output, "wt", encoding="utf-8") as stream:
            for job in jobs:
                stream.write(
                    json.dumps(
                        {
                            "record_type": "job",
                            "job": {
                                "job_id": job.job_id,
                                "name": job.name,
                                "provider": job.provider,
                                "scope": job.scope,
                                "filters": job.filters,
                                "taxonomy_snapshot": job.taxonomy_snapshot,
                                "billing_mode": job.billing_mode,
                                "ingest_mode": job.ingest_mode,
                                "estimated_items": job.estimated_items,
                                "details": job.details,
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            rows = (
                db.query(HarvestJobItem, CorpusDocument)
                .join(CorpusDocument, CorpusDocument.document_id == HarvestJobItem.document_id)
                .filter(HarvestJobItem.job_id.in_(args.job_id))
                .order_by(HarvestJobItem.job_id, HarvestJobItem.item_id)
                .yield_per(1000)
            )
            for item, document in rows:
                stream.write(
                    json.dumps(
                        {
                            "record_type": "item",
                            "job_id": item.job_id,
                            "document": {
                                "provider": document.provider,
                                "external_id": document.external_id,
                                "doi": document.doi,
                                "title": document.title,
                                "year": document.year,
                                "publication_type": document.publication_type,
                                "language": document.language,
                                "metadata_payload": document.metadata_payload,
                                "candidate_urls": document.candidate_urls,
                                "content_url": document.content_url,
                            },
                            "item_details": item.details,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                exported += 1
    finally:
        db.close()

    print(f"output={args.output}")
    print(f"items={exported}")


if __name__ == "__main__":
    main()
