from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.paper import Paper
from app.services.mineru_service import MinerUService


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild canonical MinerU content artifacts without a network call.")
    parser.add_argument("paper_id")
    args = parser.parse_args()
    with SessionLocal() as db:
        paper = db.get(Paper, args.paper_id)
        if not paper:
            raise SystemExit(f"Paper not found: {args.paper_id}")
        _, map_key, text = MinerUService(db, get_settings()).rebuild_content_map(paper)
        print(f"rebuilt {map_key} ({len(text)} chars)")


if __name__ == "__main__":
    main()
