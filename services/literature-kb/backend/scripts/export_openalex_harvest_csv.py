from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from app.db.session import SessionLocal
from app.models.harvest import CorpusDocument, HarvestJobItem


TOPICS = {
    "harvest_9c7f9115445e4dce": ("T10018", "电池材料与储能"),
    "harvest_3d0e7282f7e14b24": ("T10663", "电池安全、老化与BMS"),
}

FIELDNAMES = [
    "topic_id",
    "topic_name",
    "harvest_job_id",
    "document_id",
    "openalex_id",
    "title",
    "doi",
    "publication_year",
    "publication_type",
    "language",
    "source_classification",
    "download_status",
    "content_url_available",
    "content_url",
    "arxiv_id",
    "openalex_pdf_url_count",
    "openalex_oa_pdf_url_count",
    "best_oa_pdf_url",
    "best_oa_landing_page_url",
    "pdf_candidate_urls",
    "metadata_source_url",
]


def _unique_pdf_urls(payload: dict) -> list[str]:
    urls: list[str] = []
    for location in payload.get("locations") or []:
        if not isinstance(location, dict):
            continue
        pdf_url = location.get("pdf_url")
        if pdf_url and pdf_url not in urls:
            urls.append(pdf_url)
    return urls


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    output_dir = root / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "openalex_harvest_metadata_2020_2026.csv"

    counts = Counter()
    total = 0
    db = SessionLocal()
    try:
        query = (
            db.query(HarvestJobItem, CorpusDocument)
            .join(CorpusDocument, CorpusDocument.document_id == HarvestJobItem.document_id)
            .filter(HarvestJobItem.job_id.in_(TOPICS))
            .order_by(HarvestJobItem.job_id, HarvestJobItem.item_id)
            .yield_per(2000)
        )
        with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            for item, document in query:
                topic_id, topic_name = TOPICS[item.job_id]
                details = item.details or {}
                source = details.get("source_availability") or {}
                payload = document.metadata_payload or {}
                best_location = payload.get("best_oa_location") or {}
                pdf_urls = _unique_pdf_urls(payload)
                classification = source.get("classification", "unknown")
                counts[(topic_id, classification)] += 1
                total += 1
                writer.writerow(
                    {
                        "topic_id": topic_id,
                        "topic_name": topic_name,
                        "harvest_job_id": item.job_id,
                        "document_id": document.document_id,
                        "openalex_id": document.external_id or payload.get("id"),
                        "title": document.title,
                        "doi": document.doi,
                        "publication_year": document.year,
                        "publication_type": document.publication_type,
                        "language": document.language,
                        "source_classification": classification,
                        "download_status": item.status,
                        "content_url_available": source.get("content_url_available", False),
                        "content_url": document.content_url,
                        "arxiv_id": source.get("arxiv_id"),
                        "openalex_pdf_url_count": source.get("openalex_pdf_url_count", 0),
                        "openalex_oa_pdf_url_count": source.get("openalex_oa_pdf_url_count", 0),
                        "best_oa_pdf_url": best_location.get("pdf_url"),
                        "best_oa_landing_page_url": best_location.get("landing_page_url"),
                        "pdf_candidate_urls": json.dumps(pdf_urls, ensure_ascii=False),
                        "metadata_source_url": payload.get("id"),
                    }
                )
    finally:
        db.close()

    print(f"output={output_path}")
    print(f"rows={total}")
    for (topic_id, classification), count in sorted(counts.items()):
        print(f"{topic_id}|{classification}|{count}")


if __name__ == "__main__":
    main()
