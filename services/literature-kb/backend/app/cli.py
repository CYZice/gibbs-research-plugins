from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.directory_import_service import DirectoryImportService


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue a local literature import batch.")
    parser.add_argument("source", help="Directory containing PDF files")
    parser.add_argument("--manifest", help="Optional JSONL metadata manifest")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        batch = DirectoryImportService(db, get_settings()).import_directory(
            args.source, args.manifest
        )
        print(batch.batch_id)
    finally:
        db.close()


if __name__ == "__main__":
    main()
