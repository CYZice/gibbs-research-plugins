from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.import_batch import ImportBatchItem
from app.models.paper import Paper
from app.schemas.discovery import DiscoveryDownloadCandidate, DiscoveryPaper
from app.services.discovery_service import DiscoveryError
from app.services.pdf_download_service import PdfDownloadError
from app.services.remote_fetch_service import RemoteFetchResult, RemoteFetchService


def paper(source: str) -> DiscoveryPaper:
    return DiscoveryPaper(
        source=source,
        external_id=f"{source}-id",
        title="Paper",
        download_candidates=[
            DiscoveryDownloadCandidate(
                provider=source,
                url=f"https://example.org/{source}.pdf",
                license="cc-by",
                access="commercial_allowed",
                is_oa=True,
                version="publishedVersion",
            )
        ],
    )


class Db:
    def __init__(self, item=None):
        self.item = item
        self.added = []

    def get(self, model, _key):
        if model is ImportBatchItem:
            return self.item
        return None

    def scalar(self, _statement):
        return None

    def add(self, value):
        self.added.append(value)

    def flush(self):
        pass


def test_remote_fetch_falls_back_between_sources(monkeypatch):
    class Discovery:
        def __init__(self, _settings):
            pass

        def resolve(self, *, source, external_id):
            del external_id
            return paper(source)

        @staticmethod
        def import_candidate_rank(_candidate):
            return (0,)

        @staticmethod
        def source_candidate_rank(_candidate, source):
            return ({"core": 0, "arxiv": 1}.get(source, 9),)

    monkeypatch.setattr(
        "app.services.remote_fetch_service.DiscoveryService", Discovery
    )
    item = ImportBatchItem(
        item_id="item_1", batch_id="batch_1", source_key="remote", status="running"
    )
    service = RemoteFetchService(Db(item), Settings(app_env="test"))
    existing = Paper(paper_id="paper_1", title="Paper")

    def fetch(_item, _discovery, _paper, source):
        if source == "core":
            raise PdfDownloadError("INVALID_PDF", "bad pdf")
        return RemoteFetchResult(None, existing, True, source)

    monkeypatch.setattr(service, "fetch_resolved", fetch)
    task = SimpleNamespace(
        batch_item_id="item_1",
        provider="auto",
        details={
            "remote": {
                "options": [
                    {"source": "core", "external_id": "core-id"},
                    {"source": "arxiv", "external_id": "arxiv-id"},
                ]
            }
        },
    )

    result = service.run(task)

    assert result.paper.paper_id == "paper_1"
    assert result.selected_source == "arxiv"
    assert [row["status"] for row in task.details["source_attempts"]] == [
        "failed",
        "succeeded",
    ]


def test_remote_fetch_respects_an_explicit_source(monkeypatch):
    class Discovery:
        def __init__(self, _settings):
            pass

        def resolve(self, *, source, external_id):
            del source, external_id
            raise DiscoveryError("EXTERNAL_WORK_NOT_FOUND", "gone", 404)

    monkeypatch.setattr(
        "app.services.remote_fetch_service.DiscoveryService", Discovery
    )
    item = ImportBatchItem(
        item_id="item_1", batch_id="batch_1", source_key="remote", status="running"
    )
    task = SimpleNamespace(
        batch_item_id="item_1",
        provider="core",
        details={
            "remote": {
                "options": [
                    {"source": "core", "external_id": "missing"},
                    {"source": "arxiv", "external_id": "available"},
                ],
                "preferred_source": "core",
            }
        },
    )

    with pytest.raises(DiscoveryError, match="gone"):
        RemoteFetchService(Db(item), Settings(app_env="test")).run(task)


def test_remote_fetch_stages_pdf_without_registering_paper(monkeypatch):
    download_calls = []

    class Ingestion:
        def __init__(self, *_args):
            pass

        def find_existing(self, _metadata):
            return None

    class Download:
        def __init__(self, _settings):
            pass

        def download(self, _external_id, candidate, **_kwargs):
            download_calls.append(candidate.url)
            if candidate.url.endswith("bad.pdf"):
                raise PdfDownloadError("INVALID_PDF", "bad pdf")
            return SimpleNamespace(
                storage_key="downloads/good.pdf",
                metadata_key="downloads/good.json",
                sha256="a" * 64,
                size_bytes=10,
            )

    monkeypatch.setattr(
        "app.services.remote_fetch_service.IngestionService", Ingestion
    )
    monkeypatch.setattr(
        "app.services.remote_fetch_service.PdfDownloadService", Download
    )
    item = ImportBatchItem(
        item_id="item_1", batch_id="batch_1", source_key="remote", status="running"
    )
    resolved = paper("core").model_copy(
        update={
            "download_candidates": [
                paper("core").download_candidates[0].model_copy(
                    update={"url": "https://example.org/bad.pdf"}
                ),
                paper("core").download_candidates[0].model_copy(
                    update={"url": "https://example.org/good.pdf"}
                ),
            ]
        }
    )
    discovery = SimpleNamespace(
        import_candidates=lambda *_args, **_kwargs: resolved.download_candidates
    )
    db = Db(item)

    result = RemoteFetchService(db, Settings(app_env="test")).fetch_resolved(
        item, discovery, resolved, "core"
    )

    assert result.paper is None
    assert result.artifact.storage_key == "downloads/good.pdf"
    assert item.staged_artifact_id == result.artifact.artifact_id
    assert download_calls == [
        "https://example.org/bad.pdf",
        "https://example.org/good.pdf",
    ]


def test_existing_paper_is_reused_without_download(monkeypatch):
    existing = Paper(paper_id="paper_1", title="Existing")
    merged = []

    class Ingestion:
        def __init__(self, *_args):
            pass

        def find_existing(self, _metadata):
            return existing

        def merge_existing(self, paper, metadata, source):
            merged.append((paper.paper_id, metadata["core_id"], source))

    monkeypatch.setattr(
        "app.services.remote_fetch_service.IngestionService", Ingestion
    )
    item = ImportBatchItem(
        item_id="item_1", batch_id="batch_1", source_key="remote", status="running"
    )

    result = RemoteFetchService(Db(item), Settings(app_env="test")).fetch_resolved(
        item, object(), paper("core"), "core"
    )

    assert result.duplicate is True
    assert item.paper_id == existing.paper_id
    assert merged == [("paper_1", "core-id", "core")]
