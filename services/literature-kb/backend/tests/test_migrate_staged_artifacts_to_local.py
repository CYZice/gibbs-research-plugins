import json
from hashlib import sha256

import pytest

from scripts.migrate_staged_artifacts_to_local import (
    artifact_target,
    finalize_artifact,
    verify_metadata,
    verify_pdf,
)


def test_artifact_target_stays_under_download_root(tmp_path):
    root = tmp_path / "downloads"

    assert artifact_target(
        root, "corpus/openalex/W123/paper.pdf"
    ) == root / "corpus/openalex/W123/paper.pdf"
    assert artifact_target(root, "downloads/a.pdf") == root / "a.pdf"

    with pytest.raises(ValueError):
        artifact_target(root, "../outside.pdf")


def test_verify_pdf_checks_magic_size_and_sha256(tmp_path):
    payload = b"%PDF-1.7\nverified"
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(payload)

    verify_pdf(
        pdf,
        expected_size=len(payload),
        expected_sha256=sha256(payload).hexdigest(),
    )

    with pytest.raises(ValueError, match="size mismatch"):
        verify_pdf(
            pdf,
            expected_size=len(payload) + 1,
            expected_sha256=sha256(payload).hexdigest(),
        )


def test_verify_metadata_requires_json_object(tmp_path):
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"title": "Paper"}), encoding="utf-8")
    verify_metadata(metadata)

    metadata.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        verify_metadata(metadata)


def test_finalize_artifact_only_switches_after_local_verification(
    monkeypatch, tmp_path
):
    payload = b"%PDF-1.7\nverified"
    artifact = type(
        "Artifact",
        (),
        {
            "artifact_id": "artifact_1",
            "storage_provider": "cos",
            "storage_key": "corpus/W1/paper.pdf",
            "metadata_key": "corpus/W1/metadata.json",
            "size_bytes": len(payload),
            "sha256": sha256(payload).hexdigest(),
        },
    )()

    class Db:
        committed = False

        def scalar(self, _statement):
            return artifact

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            pass

    db = Db()
    monkeypatch.setattr(
        "scripts.migrate_staged_artifacts_to_local.SessionLocal", lambda: db
    )
    target = artifact_target(tmp_path, artifact.storage_key)
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    metadata = artifact_target(tmp_path, artifact.metadata_key)
    metadata.write_text('{"title": "Paper"}', encoding="utf-8")

    result = finalize_artifact(artifact.artifact_id, tmp_path)

    assert result["status"] == "migrated"
    assert artifact.storage_provider == "local"
    assert db.committed is True
