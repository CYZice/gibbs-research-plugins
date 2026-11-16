import argparse
import json

from app.db.session import SessionLocal
from app.services.pipeline_migration_service import migrate_v1_process_tasks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview or migrate active Pipeline V1 process tasks."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the migration. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--interrupt-running",
        action="store_true",
        help="Explicitly interrupt and migrate running V1 tasks.",
    )
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    db = SessionLocal()
    try:
        result = migrate_v1_process_tasks(
            db,
            apply=args.apply,
            interrupt_running=args.interrupt_running,
            limit=args.limit,
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
