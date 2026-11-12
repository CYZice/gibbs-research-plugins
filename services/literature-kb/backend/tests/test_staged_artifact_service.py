from types import SimpleNamespace

from app.core.config import Settings
from app.models.import_batch import ImportBatchItem
from app.models.paper import Paper
from app.models.staged_artifact import StagedArtifact
from app.services.staged_artifact_service import (
    ImportRegistrationService,
    StagedArtifactService,
)


class Db:
    def __init__(self):
        self.values = {}
        self.added = []

    def scalar(self, _statement):
        return None

    def add(self, value):
        self.added.append(value)
        key = getattr(value, "artifact_id", None)
        if key:
            self.values[(StagedArtifact, key)] = value

    def flush(self):
        pass

    def get(self, model, key):
        return self.values.get((model, key))


def test_local_pdf_is_staged_as_a_durable_artifact(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nstaged")
    downloads = tmp_path / "downloads"
    db = Db()
    settings = Settings(
        app_env="test",
        discovery_download_root=str(downloads),
        discovery_storage_provider="local",
    )

    artifact = StagedArtifactService(db, settings).stage_local_file(
        source,
        source_type="user_upload",
        source_external_id="source.pdf",
        metadata_payload={"title": "Source"},
    )

    assert artifact.status == "available"
    assert artifact.sha256
    assert (downloads / artifact.storage_key).read_bytes().startswith(b"%PDF-")


def test_registration_consumes_artifact_and_links_paper(monkeypatch, tmp_path):
    artifact = StagedArtifact(
        artifact_id="artifact_1",
        storage_provider="cos",
        storage_key="staged/a.pdf",
        sha256="a" * 64,
        size_bytes=10,
        source_type="user_upload",
        source_external_id="a.pdf",
        status="available",
        metadata_payload={"title": "A"},
    )
    item = ImportBatchItem(
        item_id="item_1",
        batch_id="batch_1",
        source_key="a.pdf",
        staged_artifact_id=artifact.artifact_id,
        status="running",
    )
    paper = Paper(
        paper_id="paper_1",
        title="A",
        parse_status="not_started",
        evidence_status="not_started",
        multimodal_status="not_started",
        embedding_status="not_started",
        index_status="not_started",
    )
    db = Db()
    db.values[(ImportBatchItem, item.item_id)] = item
    db.values[(StagedArtifact, artifact.artifact_id)] = artifact
    db.values[(Paper, paper.paper_id)] = paper

    class Storage:
        def __init__(self, _settings, *, provider=None):
            assert provider == "cos"

        def download_to_path(self, _storage_key, destination):
            destination.write_bytes(b"%PDF-1.7\nregistered")

    class Ingestion:
        def __init__(self, *_args):
            pass

        def ingest_pdf_stream(self, *_args, **_kwargs):
            return SimpleNamespace(paper_id=paper.paper_id, status="succeeded")

    monkeypatch.setattr(
        "app.services.staged_artifact_service.DiscoveryDownloadStorage", Storage
    )
    monkeypatch.setattr(
        "app.services.staged_artifact_service.IngestionService", Ingestion
    )

    result = ImportRegistrationService(
        db, Settings(app_env="test", storage_root=str(tmp_path))
    ).register(item.item_id)

    assert result.paper.paper_id == paper.paper_id
    assert result.duplicate is False
    assert artifact.status == "registered"
    assert artifact.paper_id == paper.paper_id
    assert item.paper_id == paper.paper_id
