import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.harvest import CorpusDocument, HarvestCredential, HarvestJob, HarvestJobItem
from app.schemas.harvest import (
    HarvestContentExecuteRequest,
    HarvestFreeDownloadExecuteRequest,
)
from app.services.harvest_service import (
    HarvestCredentialService,
    HarvestError,
    HarvestService,
)
from app.services.harvest_tasks import (
    harvest_download_job_id,
    harvest_rq_job_id,
    resume_harvest_jobs,
    run_harvest_download,
)
from app.services.pdf_download_service import PdfDownloadError


def settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "app_env": "test",
        "admin_secrets_master_key": base64.b64encode(b"h" * 32).decode(),
        "discovery_download_root": str(tmp_path),
        "harvest_cli_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_high_recall_topic_filters_do_not_add_license_or_content_gates(tmp_path):
    value = HarvestService._works_filter(
        {"level": "subfield", "openalex_id": "https://openalex.org/subfields/2505"},
        {
            "year_from": 2015,
            "year_to": 2026,
            "types": ["article", "review"],
            "languages": [],
        },
    )

    assert value == (
        "topics.subfield.id:2505,from_publication_date:2015-01-01,"
        "to_publication_date:2026-12-31,type:article|review"
    )
    assert "primary_topic" not in value
    assert "license" not in value
    assert "has_content" not in value


def test_openalex_pdf_candidates_keep_closed_and_unlicensed_locations():
    candidates = HarvestService._openalex_candidates(
        {
            "best_oa_location": {
                "pdf_url": "https://repo.example/a.pdf",
                "is_oa": False,
                "license": None,
            },
            "primary_location": {
                "pdf_url": "https://publisher.example/b.pdf",
                "is_oa": False,
                "license": "all-rights-reserved",
            },
            "locations": [
                {"pdf_url": "https://repo.example/a.pdf", "is_oa": True},
            ],
        }
    )

    assert [item.url for item in candidates] == [
        "https://repo.example/a.pdf",
        "https://publisher.example/b.pdf",
    ]
    assert candidates[0].license is None
    assert candidates[1].license == "all-rights-reserved"


def test_current_openalex_content_urls_pdf_field_is_supported():
    assert (
        HarvestService._content_pdf_url(
            {
                "content_urls": {
                    "pdf": "https://content.openalex.org/works/W123.pdf"
                }
            }
        )
        == "https://content.openalex.org/works/W123.pdf"
    )
    assert HarvestService._content_pdf_url(
        {"content_url": "https://content.openalex.org/works/W456.pdf"}
    ).endswith("W456.pdf")


def test_rate_limit_response_updates_dynamic_content_cost(tmp_path):
    payload = {
        "rate_limit": {
            "daily_budget_usd": 1,
            "daily_used_usd": 0.25,
            "daily_remaining_usd": 0.75,
            "prepaid_remaining_usd": 9,
            "resets_at": "2026-07-24T00:00:00Z",
            "endpoint_costs_usd": {"content": 0.0125},
        }
    }
    response = SimpleNamespace(status_code=200, json=lambda: payload)
    session = SimpleNamespace(get=lambda *args, **kwargs: response)
    service = HarvestCredentialService(settings(tmp_path), session=session)
    row = HarvestCredential(
        credential_id="harvestkey_test",
        provider="openalex",
        alias="test",
        encrypted_api_key=service.cipher.encrypt("openalex-key"),
        key_suffix="alex-key",
        key_fingerprint="fingerprint",
        status="active",
    )

    service.refresh_quota(row)

    assert row.daily_remaining_usd == 0.75
    assert row.prepaid_remaining_usd == 9
    assert row.content_cost_usd == 0.0125
    assert row.resets_at == datetime(2026, 7, 24, tzinfo=UTC)


def test_content_lease_never_uses_prepaid_balance(tmp_path):
    quota_response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "rate_limit": {
                "daily_budget_usd": 1,
                "daily_used_usd": 1,
                "daily_remaining_usd": 0,
                "prepaid_remaining_usd": 100,
                "resets_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                "endpoint_costs_usd": {"content": 0.01},
            }
        },
    )
    service = HarvestCredentialService(
        settings(tmp_path),
        session=SimpleNamespace(get=lambda *args, **kwargs: quota_response),
        redis_client=SimpleNamespace(eval=lambda *args: pytest.fail("must not reserve")),
    )
    now = datetime.now(UTC)
    row = HarvestCredential(
        credential_id="harvestkey_test",
        provider="openalex",
        alias="test",
        encrypted_api_key=service.cipher.encrypt("openalex-key"),
        key_suffix="alex-key",
        key_fingerprint="fingerprint",
        status="active",
        daily_remaining_usd=0,
        prepaid_remaining_usd=100,
        content_cost_usd=0.01,
        last_checked_at=now,
        resets_at=now + timedelta(hours=2),
    )

    class Db:
        @staticmethod
        def scalars(_statement):
            return SimpleNamespace(all=lambda: [row])

    with pytest.raises(HarvestError, match="daily free allowance") as raised:
        service.lease(Db(), "content")

    assert raised.value.code == "OPENALEX_FREE_QUOTA_EXHAUSTED"
    assert row.status == "daily_exhausted"
    assert row.prepaid_remaining_usd == 100


def test_content_batch_reserves_only_whole_files_from_daily_credit(tmp_path):
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "rate_limit": {
                "daily_budget_usd": 1,
                "daily_used_usd": 0.965,
                "daily_remaining_usd": 0.035,
                "prepaid_remaining_usd": 50,
                "resets_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "endpoint_costs_usd": {"content": 0.01},
            }
        },
    )
    reservations = []
    redis_client = SimpleNamespace(
        eval=lambda *args: reservations.append(args) or 1
    )
    service = HarvestCredentialService(
        settings(tmp_path),
        session=SimpleNamespace(get=lambda *args, **kwargs: response),
        redis_client=redis_client,
    )
    row = HarvestCredential(
        credential_id="harvestkey_batch",
        provider="openalex",
        alias="batch",
        encrypted_api_key=service.cipher.encrypt("openalex-key"),
        key_suffix="alex-key",
        key_fingerprint="batch-fingerprint",
        status="active",
    )

    class Db:
        @staticmethod
        def scalars(_statement):
            return SimpleNamespace(all=lambda: [row])

    _, _, cost, units = service.lease_many(Db(), "content", 10)

    assert cost == 0.01
    assert units == 3
    assert row.daily_remaining_usd == pytest.approx(0.005)
    assert reservations[0][3] == 30_000


def test_official_cli_receives_key_only_via_environment(tmp_path, monkeypatch):
    service = HarvestService(None, settings(tmp_path))
    document = CorpusDocument(
        document_id="corpus_test",
        provider="openalex",
        external_id="W123",
        metadata_payload={},
    )
    observed = {}
    sentinel = object()

    monkeypatch.setattr("app.services.harvest_service.shutil.which", lambda _: "openalex")

    def run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        output = Path(command[command.index("--output") + 1])
        nested = output / "W12"
        nested.mkdir()
        (nested / "W123.pdf").write_bytes(b"%PDF-1.7\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("app.services.harvest_service.subprocess.run", run)
    monkeypatch.setattr(service, "_store_cli_pdf", lambda *_args: sentinel)

    result = service._download_openalex_content(document, "secret-openalex-key")

    assert result is sentinel
    assert "--stdin" in observed["command"]
    assert "--content" in observed["command"]
    assert "secret-openalex-key" not in observed["command"]
    assert observed["env"]["OPENALEX_API_KEY"] == "secret-openalex-key"


def test_harvest_rq_job_id_uses_only_rq_safe_separators():
    job_id = harvest_rq_job_id(
        "harvest",
        "import",
        "harvestitem_123",
        "batch_456",
    )

    assert job_id == "harvest-import-harvestitem_123-batch_456"
    assert ":" not in job_id


def test_harvest_download_job_id_is_stable():
    assert harvest_download_job_id("harvestitem_123") == "harvest-download-harvestitem_123"


def test_free_download_failure_is_kept_as_content_eligible_no_source(tmp_path, monkeypatch):
    job = HarvestJob(job_id="harvest_test", name="test", scope={}, filters={})
    document = CorpusDocument(
        document_id="corpus_test",
        provider="openalex",
        external_id="W123",
        title="Deferred download paper",
        doi="10.1000/deferred",
        metadata_payload={"id": "https://openalex.org/W123"},
        candidate_urls=[],
        content_url="https://content.openalex.org/works/W123.pdf",
    )
    item = HarvestJobItem(
        item_id="harvestitem_test",
        job_id=job.job_id,
        document_id=document.document_id,
        status="resolving",
        attempts=0,
    )

    class Db:
        commits = 0

        @staticmethod
        def get(model, _identifier):
            return {HarvestJob: job, CorpusDocument: document}.get(model)

        def commit(self):
            self.commits += 1

    class Credentials:
        @staticmethod
        def lease(*_args, **_kwargs):
            pytest.fail("Content credit must not be leased during free download")

    db = Db()
    service = HarvestService(db, settings(tmp_path), credential_service=Credentials())
    monkeypatch.setattr(service, "_unpaywall_candidates", lambda *_args: [])
    monkeypatch.setattr(service, "_secondary_candidates", lambda *_args: [])
    monkeypatch.setattr(service, "reconcile_job", lambda *_args: None)

    service.download_item(item)

    assert item.status == "no_source"
    assert item.details["content_eligible"] is True
    assert item.details["content_status"] == "not_requested"
    assert item.details["content_reason"] == "free_sources_exhausted"
    assert document.title == "Deferred download paper"
    assert document.doi == "10.1000/deferred"


def test_harvest_non_retryable_pdf_error_becomes_no_source(tmp_path, monkeypatch):
    job = HarvestJob(job_id="harvest_test", name="test", scope={}, filters={})
    document = CorpusDocument(
        document_id="corpus_test",
        provider="openalex",
        external_id="W123",
        metadata_payload={"id": "https://openalex.org/W123"},
        candidate_urls=[
            {
                "provider": "openalex",
                "url": "https://example.org/missing.pdf",
                "access": "commercial_allowed",
                "is_oa": True,
            }
        ],
    )
    item = HarvestJobItem(
        item_id="harvestitem_test",
        job_id=job.job_id,
        document_id=document.document_id,
        status="pending",
        attempts=0,
    )

    class Db:
        @staticmethod
        def get(model, _identifier):
            return {HarvestJob: job, CorpusDocument: document}.get(model)

        @staticmethod
        def commit():
            pass

    class Download:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def download(*_args, **_kwargs):
            raise PdfDownloadError("PDF_NOT_FOUND", "missing")

    service = HarvestService(Db(), settings(tmp_path))
    monkeypatch.setattr("app.services.harvest_service.PdfDownloadService", Download)
    monkeypatch.setattr(service, "_unpaywall_candidates", lambda *_args: [])
    monkeypatch.setattr(service, "_secondary_candidates", lambda *_args: [])
    monkeypatch.setattr(service, "reconcile_job", lambda *_args: None)

    service.download_item(item)

    assert item.status == "no_source"
    assert item.next_attempt_at is None
    assert item.attempts == 1


def test_harvest_storage_error_is_retried_and_keeps_cause_details(
    tmp_path, monkeypatch
):
    job = HarvestJob(job_id="harvest_test", name="test", scope={}, filters={})
    document = CorpusDocument(
        document_id="corpus_test",
        provider="openalex",
        external_id="W123",
        metadata_payload={"id": "https://openalex.org/W123"},
        candidate_urls=[
            {
                "provider": "openalex",
                "url": "https://example.org/paper.pdf",
                "access": "commercial_allowed",
                "is_oa": True,
            }
        ],
    )
    item = HarvestJobItem(
        item_id="harvestitem_test",
        job_id=job.job_id,
        document_id=document.document_id,
        status="pending",
        attempts=0,
    )

    class Db:
        @staticmethod
        def get(model, _identifier):
            return {HarvestJob: job, CorpusDocument: document}.get(model)

        @staticmethod
        def commit():
            pass

    class Download:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def download(*_args, **_kwargs):
            raise PdfDownloadError(
                "PDF_STORAGE_FAILED",
                "storage unavailable",
                500,
                cause=PermissionError(13, "denied"),
            )

    service = HarvestService(Db(), settings(tmp_path))
    monkeypatch.setattr("app.services.harvest_service.PdfDownloadService", Download)
    monkeypatch.setattr(service, "_unpaywall_candidates", lambda *_args: [])
    monkeypatch.setattr(service, "_secondary_candidates", lambda *_args: [])
    monkeypatch.setattr(service, "reconcile_job", lambda *_args: None)

    service.download_item(item)

    assert item.status == "pending"
    assert item.next_attempt_at is not None
    assert item.error_code == "PDF_STORAGE_FAILED"
    assert item.details["source_attempts"] == [
        {
            "source": "openalex",
            "code": "PDF_STORAGE_FAILED",
            "retryable": True,
            "cause_type": "PermissionError",
            "cause_errno": 13,
        }
    ]


def test_download_uses_lazy_fallbacks_and_reuses_session(tmp_path, monkeypatch):
    job = HarvestJob(job_id="harvest_test", name="test", scope={}, filters={})
    document = CorpusDocument(
        document_id="corpus_test",
        provider="openalex",
        external_id="W123",
        doi="10.1000/example",
        metadata_payload={"id": "https://openalex.org/W123"},
        candidate_urls=[
            {
                "provider": "openalex",
                "url": "https://primary.example/paper.pdf",
                "access": "unknown",
                "is_oa": True,
            }
        ],
    )
    item = HarvestJobItem(
        item_id="harvestitem_test",
        job_id=job.job_id,
        document_id=document.document_id,
        status="pending",
        attempts=0,
    )

    class Db:
        @staticmethod
        def get(model, _identifier):
            return {HarvestJob: job, CorpusDocument: document}.get(model)

        @staticmethod
        def commit():
            pass

    session = object()
    calls = []

    class Download:
        def __init__(self, _settings, *, session=None):
            assert session is service.session
            calls.append("downloader")

        @staticmethod
        def download(_external_id, candidate, **_kwargs):
            calls.append(f"download:{candidate.provider}")
            if candidate.provider == "openalex":
                raise PdfDownloadError("PDF_NOT_FOUND", "missing")
            return SimpleNamespace()

    service = HarvestService(Db(), settings(tmp_path), session=session)
    monkeypatch.setattr("app.services.harvest_service.PdfDownloadService", Download)
    monkeypatch.setattr(
        service,
        "_unpaywall_candidates",
        lambda *_args: (
            calls.append("resolve:unpaywall")
            or [
                SimpleNamespace(
                    provider="unpaywall",
                    url="https://fallback.example/paper.pdf",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        service,
        "_secondary_candidates",
        lambda *_args: pytest.fail("secondary lookup must stay lazy"),
    )
    monkeypatch.setattr(
        service,
        "_mark_staged",
        lambda *_args: calls.append("staged"),
    )

    service.download_item(item)

    assert calls == [
        "downloader",
        "download:openalex",
        "resolve:unpaywall",
        "download:unpaywall",
        "staged",
    ]


def test_secondary_candidates_prioritize_arxiv_before_core(tmp_path, monkeypatch):
    document = CorpusDocument(
        document_id="corpus_test",
        provider="openalex",
        external_id="W123",
        doi="10.1000/example",
        metadata_payload={"ids": {"arxiv": "2501.12345"}},
    )

    class Provider:
        def __init__(self, name):
            self.name = name

        def resolve(self, **_kwargs):
            return self.name

        def download_candidates(self, paper):
            return [SimpleNamespace(provider=paper)]

    class Discovery:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def provider(name):
            return Provider(name)

    service = HarvestService(SimpleNamespace(), settings(tmp_path, core_api_key="core-key"))
    monkeypatch.setattr("app.services.harvest_service.DiscoveryService", Discovery)

    candidates = service._secondary_candidates(document)

    assert [candidate.provider for candidate in candidates] == ["arxiv", "core"]


def test_content_estimate_does_not_reserve_or_change_items(tmp_path, monkeypatch):
    job = HarvestJob(job_id="harvest_test", name="test", scope={}, filters={})
    item = HarvestJobItem(
        item_id="harvestitem_test",
        job_id=job.job_id,
        document_id="corpus_test",
        status="no_source",
    )

    class Statement:
        pass

    class Db:
        @staticmethod
        def scalars(_statement):
            return SimpleNamespace(all=lambda: [item])

    class Credentials:
        @staticmethod
        def quota_summary(_db):
            return {
                "items": [{"status": "active", "content_cost_usd": 0.01}],
                "daily_remaining_usd": 0.02,
                "estimated_content_files": 2,
            }

    service = HarvestService(Db(), settings(tmp_path), credential_service=Credentials())
    monkeypatch.setattr(
        service, "_content_items_statement", lambda *_args, **_kwargs: Statement()
    )

    result = service.estimate_content_downloads(
        job, selection="items", item_ids=[item.item_id, "harvestitem_missing"]
    )

    assert result["eligible_items"] == 1
    assert result["excluded_items"] == 1
    assert result["estimated_cost_usd"] == 0.01
    assert item.status == "no_source"


def test_explicit_content_request_queues_only_selected_items(tmp_path, monkeypatch):
    job = HarvestJob(
        job_id="harvest_test",
        name="test",
        scope={},
        filters={},
        status="completed_with_errors",
    )
    selected = HarvestJobItem(
        item_id="harvestitem_selected",
        job_id=job.job_id,
        document_id="corpus_selected",
        status="no_source",
    )

    class Statement:
        def with_for_update(self, **_kwargs):
            return self

    class Db:
        commits = 0

        @staticmethod
        def scalars(_statement):
            return SimpleNamespace(all=lambda: [selected])

        def commit(self):
            self.commits += 1

    class Credentials:
        @staticmethod
        def quota_summary(_db):
            return {
                "items": [{"status": "active", "content_cost_usd": 0.01}],
                "daily_remaining_usd": 1,
                "estimated_content_files": 100,
            }

    db = Db()
    service = HarvestService(
        db,
        settings(tmp_path, harvest_allow_openalex_content=True),
        credential_service=Credentials(),
    )
    monkeypatch.setattr(
        service, "_content_items_statement", lambda *_args, **_kwargs: Statement()
    )

    queued = service.request_content_downloads(
        job, selection="items", item_ids=[selected.item_id]
    )

    assert queued == [selected.item_id]
    assert selected.status == "resolving"
    assert selected.details["phase"] == "content"
    assert selected.details["content_status"] == "queued"
    assert job.status == "downloading"


def test_content_request_is_rejected_while_global_switch_is_disabled(tmp_path):
    job = HarvestJob(job_id="harvest_test", name="test", scope={}, filters={})
    service = HarvestService(None, settings(tmp_path))

    with pytest.raises(HarvestError, match="disabled") as raised:
        service.request_content_downloads(job, selection="all_eligible")

    assert raised.value.code == "OPENALEX_CONTENT_DISABLED"


def test_content_execute_schema_requires_explicit_confirmation():
    request = HarvestContentExecuteRequest(
        selection="items", item_ids=["harvestitem_test"], confirm=True
    )

    assert request.confirm is True
    with pytest.raises(ValidationError):
        HarvestContentExecuteRequest(selection="all_eligible", confirm=False)


def test_free_download_request_queues_metadata_ready_free_candidates(tmp_path, monkeypatch):
    job = HarvestJob(
        job_id="harvest_test",
        name="test",
        scope={},
        filters={},
        status="completed",
    )
    selected = HarvestJobItem(
        item_id="harvestitem_selected",
        job_id=job.job_id,
        document_id="corpus_selected",
        status="metadata_ready",
        details={"source_availability": {"classification": "free_candidate"}},
    )

    class Statement:
        def with_for_update(self, **_kwargs):
            return self

    class Db:
        commits = 0

        @staticmethod
        def scalars(_statement):
            return SimpleNamespace(all=lambda: [selected])

        def commit(self):
            self.commits += 1

    db = Db()
    service = HarvestService(db, settings(tmp_path))
    monkeypatch.setattr(
        service, "_free_download_items_statement", lambda *_args, **_kwargs: Statement()
    )

    queued = service.request_free_downloads(
        job, selection="items", item_ids=[selected.item_id]
    )

    assert queued == 1
    assert selected.status == "pending"
    assert selected.details["free_download_status"] == "queued"
    assert job.status == "downloading"


def test_free_download_execute_schema_requires_explicit_confirmation():
    request = HarvestFreeDownloadExecuteRequest(
        selection="items", item_ids=["harvestitem_test"], confirm=True
    )

    assert request.confirm is True
    with pytest.raises(ValidationError):
        HarvestFreeDownloadExecuteRequest(
            selection="all_free_candidates", confirm=False
        )


def test_download_task_waits_for_item_lock_instead_of_skipping_it(tmp_path, monkeypatch):
    lock_kwargs = []

    class Statement:
        def where(self, *_args):
            return self

        def with_for_update(self, **kwargs):
            lock_kwargs.append(kwargs)
            return self

    class Db:
        @staticmethod
        def scalar(_statement):
            return None

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(
        "app.services.harvest_tasks.get_settings",
        lambda: settings(tmp_path, harvest_enabled=True),
    )
    monkeypatch.setattr("app.services.harvest_tasks.SessionLocal", lambda: Db())
    monkeypatch.setattr("app.services.harvest_tasks.select", lambda *_args: Statement())

    result = run_harvest_download("harvestitem_locked")

    assert result == {"status": "missing_or_busy"}
    assert lock_kwargs == [{}]


def test_download_task_commits_fast_path_state_changes(tmp_path, monkeypatch):
    item = HarvestJobItem(
        item_id="harvestitem_existing",
        job_id="harvest_existing",
        document_id="corpus_existing",
        status="resolving",
    )

    class Statement:
        def where(self, *_args):
            return self

        def with_for_update(self, **_kwargs):
            return self

    class Db:
        def __init__(self):
            self.commits = 0

        @staticmethod
        def scalar(_statement):
            return item

        def commit(self):
            self.commits += 1

        @staticmethod
        def close():
            pass

    db = Db()
    monkeypatch.setattr(
        "app.services.harvest_tasks.get_settings",
        lambda: settings(tmp_path, harvest_enabled=True),
    )
    monkeypatch.setattr("app.services.harvest_tasks.SessionLocal", lambda: db)
    monkeypatch.setattr("app.services.harvest_tasks.select", lambda *_args: Statement())
    monkeypatch.setattr(
        HarvestService,
        "download_item",
        lambda _service, current: setattr(current, "status", "staged"),
    )

    result = run_harvest_download(item.item_id)

    assert result == {"status": "staged", "item_id": item.item_id}
    assert db.commits == 1


def test_resume_harvest_jobs_uses_free_dispatch_for_metadata_only(tmp_path, monkeypatch):
    metadata_job = HarvestJob(
        job_id="harvest_meta",
        name="meta",
        scope={},
        filters={},
        status="downloading",
        ingest_mode="metadata_only",
        enumeration_complete=True,
    )
    stage_job = HarvestJob(
        job_id="harvest_stage",
        name="stage",
        scope={},
        filters={},
        status="downloading",
        ingest_mode="stage_only",
        enumeration_complete=True,
    )
    pending_stage = HarvestJobItem(
        item_id="harvestitem_stage",
        job_id=stage_job.job_id,
        document_id="corpus_stage",
        status="pending",
    )

    class FakeScalarResult:
        def __init__(self, rows):
            self._rows = list(rows)

        def all(self):
            return list(self._rows)

    class FakeDb:
        def __init__(self):
            self.commits = 0

        def scalars(self, statement):
            sql = str(statement)
            if "FROM harvest_jobs" in sql:
                return FakeScalarResult([metadata_job, stage_job])
            if "updated_at <=" in sql:
                return FakeScalarResult([])
            if "job_id = :job_id_1" in sql and "status = :status_1" in sql:
                return FakeScalarResult([pending_stage])
            return FakeScalarResult([])

        def commit(self):
            self.commits += 1

        def close(self):
            pass

    class FakeConnection:
        pass

    queued_expand = []
    queued_free = []
    queued_download = []
    monkeypatch.setattr("app.services.harvest_tasks.SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(
        "app.services.harvest_tasks.get_settings",
        lambda: settings(tmp_path, harvest_enabled=True),
    )
    monkeypatch.setattr(
        "app.services.harvest_tasks.redis.Redis.from_url",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    monkeypatch.setattr(
        "app.services.harvest_tasks._download_delivery_is_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.harvest_tasks.enqueue_harvest_expand",
        lambda job_id, delay_seconds=0: queued_expand.append((job_id, delay_seconds)),
    )
    monkeypatch.setattr(
        "app.services.harvest_tasks.enqueue_harvest_free_download_dispatch",
        lambda job_id, delay_seconds=0: queued_free.append((job_id, delay_seconds)),
    )
    monkeypatch.setattr(
        "app.services.harvest_tasks.enqueue_harvest_download",
        lambda item_id, delay_seconds=0: queued_download.append((item_id, delay_seconds)),
    )

    result = resume_harvest_jobs()

    assert result["status"] == "ok"
    assert ("harvest_meta", 0) in queued_free
    assert ("harvest_meta", 0) in queued_expand
    assert ("harvest_stage", 0) in queued_expand
    assert queued_download == [("harvestitem_stage", 0)]


def test_metadata_only_source_classification_distinguishes_free_and_content_only():
    free_document = CorpusDocument(
        document_id="corpus_free",
        provider="openalex",
        external_id="W123",
        doi="10.48550/arxiv.2401.00001",
        metadata_payload={"ids": {"arxiv": "2401.00001"}},
        candidate_urls=[{"url": "https://repo.example/paper.pdf", "is_oa": True}],
        content_url="https://content.openalex.org/works/W123.pdf",
    )
    content_document = CorpusDocument(
        document_id="corpus_content",
        provider="openalex",
        external_id="W456",
        metadata_payload={},
        candidate_urls=[],
        content_url="https://content.openalex.org/works/W456.pdf",
    )

    free = HarvestService._source_availability(free_document)
    content = HarvestService._source_availability(content_document)

    assert free["classification"] == "free_candidate"
    assert free["arxiv_candidate"] is True
    assert content["classification"] == "content_only"
    assert content["download_verified"] is False
