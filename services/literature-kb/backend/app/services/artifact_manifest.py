import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

MANIFEST_VERSION = "artifact_manifest.v1"


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def parser_config_hash(values: dict[str, Any]) -> str:
    payload = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return stable_hash(payload)


def write_artifact_manifest(
    parsed_dir: Path,
    paper_id: str,
    pdf_sha256: str | None,
    parser_config: dict[str, Any],
) -> Path:
    root = parsed_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "artifact_manifest.json"
    content_list = _first_match(
        root,
        (
            "*content_list_v2.json",
            "content_list_v2.json",
            "*content_list.json",
            "content_list.json",
        ),
    )
    layout = root / "layout.json"
    mineru_result = root / "mineru_result.json"
    mineru_zip = root / "mineru_result.zip"
    mineru_config = parser_config.get("mineru", {})
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "paper_id": paper_id,
        "pdf_sha256": pdf_sha256,
        "created_at": datetime.now(UTC).isoformat(),
        "parsers": {
            "mineru": {
                "enabled": bool(mineru_config.get("enabled", True)),
                "version": mineru_config.get("version"),
                "config_hash": parser_config_hash(mineru_config),
                "content_list_path": _relative(root, content_list),
                "layout_path": _relative(root, layout if layout.exists() else None),
                "raw_zip_path": _relative(root, mineru_zip if mineru_zip.exists() else None),
                "result_json_path": _relative(
                    root, mineru_result if mineru_result.exists() else None
                ),
            },
        },
        "assets": _assets(root),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


def _first_match(root: Path, patterns: tuple[str, ...]) -> Path | None:
    for pattern in patterns:
        matches = sorted(root.rglob(pattern))
        if matches:
            return matches[0]
    return None


def _relative(root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _assets(root: Path) -> list[dict[str, Any]]:
    image_root = root / "images"
    if not image_root.exists():
        return []
    rows = []
    for path in sorted(item for item in image_root.rglob("*") if item.is_file()):
        relative_path = _relative(root, path)
        if relative_path is None:
            continue
        rows.append(
            {
                "relative_path": relative_path,
                "asset_type": "figure",
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
                "page": None,
                "bbox": None,
            }
        )
    return rows
