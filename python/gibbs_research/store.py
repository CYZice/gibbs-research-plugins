"""Immutable artifact revisions; SQLite transactions serialize concurrent saves."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class ConflictError(ValueError):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS revisions (
                    artifact_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, revision));
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "artifacts.sqlite3", timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, kind, payload, artifact_id=None, base_revision=None):
        if kind not in {"plot", "report"}:
            raise ValueError("Unsupported artifact kind")
        artifact_id = artifact_id or f"{kind}_{uuid.uuid4().hex}"
        self.validate_id(artifact_id)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT MAX(revision), kind FROM revisions WHERE artifact_id=?", (artifact_id,)).fetchone()
            latest = row[0] or 0
            if latest and row[1] != kind:
                raise ValueError("Artifact kind mismatch")
            if (latest and base_revision != latest) or (not latest and base_revision is not None):
                raise ConflictError("Artifact changed; reload the latest revision before saving")
            revision = latest + 1
            created = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (artifact_id, revision, kind, created, encoded))
        return self.get(artifact_id, revision)

    @staticmethod
    def validate_id(artifact_id):
        if not re.fullmatch(r"(?:plot|report)_[a-f0-9]{32}", artifact_id):
            raise ValueError("Invalid artifact id")

    def get(self, artifact_id, revision=None):
        self.validate_id(artifact_id)
        with self.connect() as db:
            if revision is None:
                row = db.execute("SELECT * FROM revisions WHERE artifact_id=? ORDER BY revision DESC LIMIT 1", (artifact_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM revisions WHERE artifact_id=? AND revision=?", (artifact_id, revision)).fetchone()
        if not row:
            raise KeyError("Artifact revision not found")
        return dict(artifact_id=row[0], revision=row[1], kind=row[2], created_at=row[3], **json.loads(row[4]))

    def list(self):
        with self.connect() as db:
            rows = db.execute("SELECT artifact_id, MAX(revision) FROM revisions GROUP BY artifact_id ORDER BY MAX(created_at) DESC").fetchall()
        return [self.get(a, r) for a, r in rows]

    def history(self, artifact_id):
        self.validate_id(artifact_id)
        with self.connect() as db:
            rows = db.execute("SELECT revision FROM revisions WHERE artifact_id=? ORDER BY revision DESC", (artifact_id,)).fetchall()
        return [self.get(artifact_id, r[0]) for r in rows]

    def feedback(self, artifact_id, revision, text):
        self.get(artifact_id, revision)
        if not text.strip():
            raise ValueError("Feedback is empty")
        item = dict(id=uuid.uuid4().hex, artifact_id=artifact_id, revision=revision, text=text, status="pending")
        with self.connect() as db:
            db.execute("INSERT INTO feedback VALUES (:id,:artifact_id,:revision,:text,:status)", item)
        return item

    def pending(self):
        with self.connect() as db:
            rows = db.execute("SELECT id, artifact_id, revision, text, status FROM feedback WHERE status='pending'").fetchall()
        return [dict(zip(("id", "artifact_id", "revision", "text", "status"), row)) for row in rows]

    def resolve_feedback(self, feedback_id, artifact_id):
        with self.connect() as db:
            cur = db.execute("UPDATE feedback SET status='resolved' WHERE id=? AND artifact_id=?", (feedback_id, artifact_id))
            if cur.rowcount != 1:
                raise ValueError("Feedback not found for this artifact")
