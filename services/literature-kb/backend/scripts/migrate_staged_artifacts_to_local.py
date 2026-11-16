from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.staged_artifact import StagedArtifact  # noqa: E402
from app.services.discovery_download_storage import (  # noqa: E402
    DiscoveryDownloadStorage,
)

_thread_state = threading.local()


def artifact_target(root: Path, storage_key: str) -> Path:
    root = root.resolve()
    target = (root / storage_key.removeprefix("downloads/")).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Artifact storage key escapes the configured local root.")
    return target


def verify_pdf(path: Path, *, expected_size: int, expected_sha256: str) -> None:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("Migrated artifact is not a PDF.")
        stream.seek(0)
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    if size != expected_size:
        raise ValueError(
            f"Migrated artifact size mismatch: expected {expected_size}, got {size}."
        )
    if digest.hexdigest() != expected_sha256:
        raise ValueError("Migrated artifact SHA256 mismatch.")


def verify_metadata(path: Path) -> None:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Migrated metadata must be a JSON object.")


def migrate_artifact(
    artifact_id: str,
    local_root: Path,
    *,
    update_provider: bool = True,
) -> dict:
    started_at = time.monotonic()
    db = SessionLocal()
    try:
        artifact = db.scalar(
            select(StagedArtifact)
            .where(StagedArtifact.artifact_id == artifact_id)
            .with_for_update(skip_locked=True)
        )
        if not artifact:
            return {"artifact_id": artifact_id, "status": "locked_or_missing"}
        if artifact.storage_provider != "cos":
            return {
                "artifact_id": artifact_id,
                "status": "already_migrated",
                "storage_provider": artifact.storage_provider,
            }

        storage = getattr(_thread_state, "cos_storage", None)
        if storage is None:
            storage = DiscoveryDownloadStorage(get_settings(), provider="cos")
            _thread_state.cos_storage = storage

        pdf_target = artifact_target(local_root, artifact.storage_key)
        pdf_target.parent.mkdir(parents=True, exist_ok=True)
        reused_pdf = False
        if pdf_target.exists():
            try:
                verify_pdf(
                    pdf_target,
                    expected_size=artifact.size_bytes,
                    expected_sha256=artifact.sha256,
                )
                reused_pdf = True
            except (OSError, ValueError):
                quarantine = pdf_target.with_name(
                    f"{pdf_target.name}.invalid-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
                )
                pdf_target.replace(quarantine)
        if not reused_pdf:
            pdf_part = pdf_target.with_name(f"{pdf_target.name}.{artifact_id}.part")
            storage.download_to_path(artifact.storage_key, pdf_part)
            verify_pdf(
                pdf_part,
                expected_size=artifact.size_bytes,
                expected_sha256=artifact.sha256,
            )
            pdf_part.replace(pdf_target)

        reused_metadata = False
        if artifact.metadata_key:
            metadata_target = artifact_target(local_root, artifact.metadata_key)
            metadata_target.parent.mkdir(parents=True, exist_ok=True)
            if metadata_target.exists():
                try:
                    verify_metadata(metadata_target)
                    reused_metadata = True
                except (OSError, ValueError, json.JSONDecodeError):
                    quarantine = metadata_target.with_name(
                        f"{metadata_target.name}.invalid-"
                        f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
                    )
                    metadata_target.replace(quarantine)
            if not reused_metadata:
                metadata_part = metadata_target.with_name(
                    f"{metadata_target.name}.{artifact_id}.part"
                )
                storage.download_to_path(artifact.metadata_key, metadata_part)
                verify_metadata(metadata_part)
                metadata_part.replace(metadata_target)

        status = "migrated" if update_provider else "copied"
        if update_provider:
            artifact.storage_provider = "local"
            db.commit()
        else:
            db.rollback()
        return {
            "artifact_id": artifact_id,
            "status": status,
            "storage_key": artifact.storage_key,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "reused_pdf": reused_pdf,
            "reused_metadata": reused_metadata,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
    except Exception as exc:
        db.rollback()
        return {
            "artifact_id": artifact_id,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
    finally:
        db.close()


def finalize_artifact(artifact_id: str, local_root: Path) -> dict:
    started_at = time.monotonic()
    db = SessionLocal()
    try:
        artifact = db.scalar(
            select(StagedArtifact)
            .where(StagedArtifact.artifact_id == artifact_id)
            .with_for_update(skip_locked=True)
        )
        if not artifact:
            return {"artifact_id": artifact_id, "status": "locked_or_missing"}
        if artifact.storage_provider != "cos":
            return {
                "artifact_id": artifact_id,
                "status": "already_migrated",
                "storage_provider": artifact.storage_provider,
            }
        verify_pdf(
            artifact_target(local_root, artifact.storage_key),
            expected_size=artifact.size_bytes,
            expected_sha256=artifact.sha256,
        )
        if artifact.metadata_key:
            verify_metadata(artifact_target(local_root, artifact.metadata_key))
        artifact.storage_provider = "local"
        db.commit()
        return {
            "artifact_id": artifact_id,
            "status": "migrated",
            "storage_key": artifact.storage_key,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
    except Exception as exc:
        db.rollback()
        return {
            "artifact_id": artifact_id,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy staged artifacts from COS to local storage with verification."
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--local-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--copy-only", action="store_true")
    mode.add_argument("--finalize-only", action="store_true")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be zero or greater")
    if not 1 <= args.workers <= 32:
        parser.error("--workers must be between 1 and 32")

    settings = get_settings()
    local_root = (
        args.local_root or Path(settings.discovery_download_root)
    ).resolve()
    db = SessionLocal()
    try:
        statement = (
            select(StagedArtifact.artifact_id)
            .where(StagedArtifact.storage_provider == "cos")
            .order_by(StagedArtifact.created_at, StagedArtifact.artifact_id)
        )
        if args.limit:
            statement = statement.limit(args.limit)
        artifact_ids = list(db.scalars(statement))
    finally:
        db.close()

    summary = {
        "selected": len(artifact_ids),
        "copied": 0,
        "migrated": 0,
        "already_migrated": 0,
        "locked_or_missing": 0,
        "failed": 0,
        "bytes_migrated": 0,
    }
    if args.dry_run:
        print(json.dumps({**summary, "dry_run": True}, ensure_ascii=False))
        return

    manifest = args.manifest or (
        local_root
        / "_migration-manifests"
        / f"cos-to-local-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    with manifest.open("a", encoding="utf-8") as output:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            if args.finalize_only:
                futures = {
                    executor.submit(
                        finalize_artifact, artifact_id, local_root
                    ): artifact_id
                    for artifact_id in artifact_ids
                }
            else:
                futures = {
                    executor.submit(
                        migrate_artifact,
                        artifact_id,
                        local_root,
                        update_provider=not args.copy_only,
                    ): artifact_id
                    for artifact_id in artifact_ids
                }
            for completed, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                status = result["status"]
                summary[status] = summary.get(status, 0) + 1
                if status in {"copied", "migrated"}:
                    summary["bytes_migrated"] += int(result["size_bytes"])
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
                output.flush()
                if completed % 25 == 0 or completed == len(futures):
                    elapsed = max(time.monotonic() - started_at, 0.001)
                    print(
                        json.dumps(
                            {
                                **summary,
                                "completed": completed,
                                "elapsed_seconds": round(elapsed, 1),
                                "artifacts_per_second": round(completed / elapsed, 2),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

    print(f"manifest={manifest}")
    if summary["failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
