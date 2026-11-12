from io import BytesIO
from pathlib import Path

import pytest
import requests

from app.core.config import Settings
from app.schemas.discovery import DiscoveryDownloadCandidate, DiscoveryPaper
from app.services import discovery_service
from app.services.discovery_download_storage import (
    DiscoveryDownloadStorage,
    DiscoveryStorageError,
)
from app.services.discovery_service import DiscoveryService
from app.services.pdf_download_service import PdfDownloadError, PdfDownloadService
from app.services.remote_fetch_service import RemoteFetchService


class FakeResponse:
    def __init__(self, payload=None, *, content=b"", status_code=200, headers=None):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024):
        del chunk_size
        yield self.content

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeCosNotFoundError(Exception):
    def get_status_code(self):
        return 404


class FakeCosClient:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.puts = []

    def head_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise FakeCosNotFoundError()

    def upload_file(self, *, Bucket, Key, LocalFilePath):
        del Bucket
        self.uploads.append(Key)
        self.objects[Key] = Path(LocalFilePath).read_bytes()

    def put_object(self, *, Bucket, Key, Body, ContentType):
        del Bucket, ContentType
        self.puts.append(Key)
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        del Bucket
        return {"Body": BytesIO(self.objects[Key])}


def openalex_work():
    return {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1000/example",
        "display_name": "An open paper",
        "publication_year": 2025,
        "publication_date": "2025-01-02",
        "type": "article",
        "language": "en",
        "cited_by_count": 12,
        "authorships": [
            {
                "author": {
                    "display_name": "Ada Example",
                    "orcid": "https://orcid.org/0000-0000-0000-0001",
                }
            }
        ],
        "ids": {"arxiv": "https://arxiv.org/abs/2501.00001"},
        "abstract_inverted_index": {"Open": [0], "science": [1]},
        "open_access": {"is_oa": True, "oa_status": "gold"},
        "best_oa_location": {
            "pdf_url": "https://example.org/paper.pdf",
            "landing_page_url": "https://example.org/paper",
            "license": "cc-by",
            "version": "publishedVersion",
            "is_oa": True,
            "source": {"display_name": "Example Journal", "type": "journal"},
        },
        "locations": [],
    }


def test_openalex_search_is_normalized_and_keeps_cursor():
    session = FakeSession(
        [FakeResponse({"meta": {"count": 1, "next_cursor": "next"}, "results": [openalex_work()]})]
    )
    service = DiscoveryService(
        Settings(app_env="test", openalex_api_key="key"), session=session
    )

    result = service.search_openalex("open science", page_size=25, cursor="*")

    assert result.total == 1
    assert result.next_cursor == "next"
    assert result.items[0].external_id == "W123"
    assert result.items[0].doi == "10.1000/example"
    assert result.items[0].arxiv_id == "2501.00001"
    assert result.items[0].abstract == "Open science"
    assert result.items[0].download_candidates[0].access == "commercial_allowed"
    assert session.calls[0][1]["params"]["api_key"] == "key"


def test_openalex_recovers_arxiv_id_from_locations():
    work = openalex_work()
    work["ids"].pop("arxiv")
    work["locations"] = [
        {
            "landing_page_url": "http://arxiv.org/abs/1706.03762",
            "pdf_url": "https://arxiv.org/pdf/1706.03762v5",
        }
    ]
    service = DiscoveryService(
        Settings(app_env="test"),
        session=FakeSession(
            [FakeResponse({"meta": {"count": 1}, "results": [work]})]
        ),
    )

    result = service.search("openalex", "Attention Is All You Need")

    assert result.items[0].arxiv_id == "1706.03762"


def test_registered_provider_dispatches_search_and_resolve():
    session = FakeSession(
        [
            FakeResponse({"meta": {"count": 1}, "results": [openalex_work()]}),
            FakeResponse(openalex_work()),
        ]
    )
    service = DiscoveryService(Settings(app_env="test"), session=session)

    searched = service.search("openalex", "open science")
    resolved = service.resolve(source="openalex", external_id="W123")

    assert searched.source == resolved.source == "openalex"
    assert resolved.external_id == "W123"


def test_core_search_maps_metadata_candidates_and_offset_cursor():
    session = FakeSession([FakeResponse({
        "totalHits": 2,
        "results": [{
            "id": 123, "title": "CORE paper", "doi": "10.1000/core",
            "arxivId": "2401.00001v2", "yearPublished": 2024,
            "authors": [{"name": "Core Author"}], "license": "cc-by",
            "downloadUrl": "https://core.ac.uk/download/123.pdf",
        }],
    })])
    service = DiscoveryService(
        Settings(app_env="test", core_api_key="core-key"), session=session
    )

    result = service.search(
        "core",
        "CORE paper",
        page_size=1,
        cursor="*",
        publication_types=["article"],
    )

    assert result.source == "core"
    assert result.next_cursor == "1"
    assert result.items[0].external_id == "123"
    assert result.items[0].arxiv_id == "2401.00001"
    assert result.items[0].download_candidates[0].provider == "core"
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer core-key"
    assert session.calls[0][1]["params"]["q"] == "(CORE paper)"


def test_core_maps_oai_arxiv_id_and_rejects_insecure_candidates():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "id": 42873602,
                    "title": "Attention Is All You Need",
                    "identifiers": [
                        {"identifier": "83868954", "type": "core_id"},
                        {
                            "identifier": "oai:arxiv.org:1706.03762",
                            "type": "oai_id",
                        },
                    ],
                    "oaiIds": ["oai:arxiv.org:1706.03762"],
                    "downloadUrl": "https://core.ac.uk/download/83868954.pdf",
                    "sourceFulltextUrls": ["http://arxiv.org/abs/1706.03762"],
                }
            )
        ]
    )
    service = DiscoveryService(
        Settings(app_env="test", core_api_key="core-key"), session=session
    )

    paper = service.resolve(source="core", external_id="42873602")

    assert paper.arxiv_id == "1706.03762"
    assert [item.url for item in paper.download_candidates] == [
        "https://core.ac.uk/download/83868954.pdf"
    ]


def test_arxiv_atom_is_parsed_without_version_in_canonical_id():
    xml = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
      <opensearch:totalResults>2</opensearch:totalResults><opensearch:startIndex>0</opensearch:startIndex>
      <opensearch:itemsPerPage>1</opensearch:itemsPerPage><entry>
      <id>http://arxiv.org/abs/2401.00001v3</id><title> Arxiv paper </title>
      <summary> Abstract text </summary><published>2024-01-01T00:00:00Z</published>
      <updated>2024-01-02T00:00:00Z</updated><author><name>Ada Author</name></author>
      <link href="https://arxiv.org/abs/2401.00001v3" rel="alternate" type="text/html" />
      <link href="https://arxiv.org/pdf/2401.00001v3" rel="related" type="application/pdf" />
      <arxiv:doi>10.1000/arxiv</arxiv:doi><arxiv:primary_category term="cs.AI" />
      </entry></feed>'''
    service = DiscoveryService(
        Settings(app_env="test"), session=FakeSession([FakeResponse(content=xml)])
    )

    result = service.search("arxiv", "Arxiv paper", page_size=1, cursor="*")

    assert result.next_cursor == "1"
    assert result.items[0].external_id == "2401.00001"
    assert result.items[0].doi == "10.1000/arxiv"
    assert result.items[0].download_candidates[0].url.endswith("2401.00001v3")


def test_arxiv_rate_lease_covers_request_then_keeps_three_second_cooldown(monkeypatch):
    class FakeRateClient:
        def __init__(self):
            self.set_calls = []
            self.expire_calls = []

        def set(self, *args, **kwargs):
            self.set_calls.append((args, kwargs))
            return True

        def expire(self, *args):
            self.expire_calls.append(args)

    client = FakeRateClient()
    monkeypatch.setattr(
        discovery_service.redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: client,
    )
    xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>0</opensearch:totalResults>
      <opensearch:startIndex>0</opensearch:startIndex>
      <opensearch:itemsPerPage>0</opensearch:itemsPerPage></feed>'''
    service = DiscoveryService(
        Settings(
            app_env="development",
            discovery_request_timeout_seconds=9,
            discovery_search_timeout_seconds=8,
        ),
        session=FakeSession([FakeResponse(content=xml)]),
    )
    monkeypatch.setattr(service, "_record_call", lambda *_args, **_kwargs: None)

    service.search("arxiv", "rate lease")

    assert client.set_calls[0][1]["ex"] == 11
    assert service.session.calls[0][1]["timeout"] == (5, 8.0)
    assert client.expire_calls == [("discovery:rate:arxiv", 3)]


def test_arxiv_resolve_keeps_the_longer_request_timeout(monkeypatch):
    class FakeRateClient:
        def __init__(self):
            self.set_calls = []

        def set(self, *args, **kwargs):
            self.set_calls.append((args, kwargs))
            return True

        def expire(self, *_args):
            pass

    client = FakeRateClient()
    monkeypatch.setattr(
        discovery_service.redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: client,
    )
    xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>0</opensearch:totalResults>
      <opensearch:startIndex>0</opensearch:startIndex>
      <opensearch:itemsPerPage>0</opensearch:itemsPerPage></feed>'''
    service = DiscoveryService(
        Settings(app_env="development", discovery_request_timeout_seconds=9),
        session=FakeSession([FakeResponse(content=xml)]),
    )
    monkeypatch.setattr(service, "_record_call", lambda *_args, **_kwargs: None)

    service._arxiv_response({"id_list": "2401.00001", "max_results": 1})

    assert client.set_calls[0][1]["ex"] == 12
    assert service.session.calls[0][1]["timeout"] == (5, 9.0)


def test_arxiv_resolve_falls_back_to_official_abstract_metadata(monkeypatch):
    html = b"""<html><head>
      <meta name="citation_title" content="Robust pretraining" />
      <meta name="citation_author" content="Ada Author" />
      <meta name="citation_author" content="Grace Researcher" />
      <meta name="citation_date" content="2019/07/26" />
      <meta name="citation_pdf_url" content="https://arxiv.org/pdf/1907.11692" />
      <meta name="description" content="Abstract text" />
    </head></html>"""
    response = FakeResponse(content=html)
    response.encoding = "utf-8"
    service = DiscoveryService(Settings(app_env="test"), session=FakeSession([response]))
    monkeypatch.setattr(
        service,
        "_arxiv_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            discovery_service.DiscoveryError(
                "UPSTREAM_RATE_LIMITED", "limited", 503, retryable=True
            )
        ),
    )

    paper = service.resolve(source="arxiv", external_id="1907.11692")

    assert paper.title == "Robust pretraining"
    assert paper.publication_date.isoformat() == "2019-07-26"
    assert [author["name"] for author in paper.authors] == [
        "Ada Author",
        "Grace Researcher",
    ]
    assert paper.download_candidates[0].url == "https://arxiv.org/pdf/1907.11692"
    assert paper.warnings == ["arXiv Atom API unavailable: UPSTREAM_RATE_LIMITED"]
    assert response.closed is True


@pytest.mark.parametrize(
    "query",
    [
        "1706.03762",
        "1706.03762v7",
        "https://arxiv.org/abs/1706.03762v7",
        "https://doi.org/10.48550/arXiv.1706.03762",
    ],
)
def test_arxiv_identifier_search_uses_id_list(monkeypatch, query):
    service = DiscoveryService(Settings(app_env="test"))
    calls = []
    paper = discovery_service.DiscoveryPaper(
        source="arxiv", external_id="1706.03762", arxiv_id="1706.03762", year=2017
    )

    def response(params, **_kwargs):
        calls.append(params)
        return discovery_service.DiscoverySearchResponse(
            source="arxiv", items=[paper], total=1
        )

    monkeypatch.setattr(service, "_arxiv_response", response)

    result = service.search("arxiv", query, year_from=2017, year_to=2017)

    assert result.items == [paper]
    assert calls == [{"id_list": "1706.03762", "max_results": 1}]


@pytest.mark.parametrize("query", ["W123", "https://openalex.org/W123"])
def test_openalex_identifier_search_uses_singleton(query):
    session = FakeSession([FakeResponse(openalex_work())])
    service = DiscoveryService(
        Settings(app_env="test", unpaywall_email=None), session=session
    )

    result = service.search("openalex", query)

    assert result.items[0].external_id == "W123"
    assert session.calls[0][0].endswith("/works/W123")


def test_openalex_doi_search_uses_singleton():
    session = FakeSession([FakeResponse(openalex_work())])
    service = DiscoveryService(
        Settings(app_env="test", unpaywall_email=None), session=session
    )

    result = service.search("openalex", "https://doi.org/10.1000/example")

    assert result.items[0].doi == "10.1000/example"
    assert session.calls[0][0].endswith("/works/doi:10.1000%2Fexample")


def test_resolve_adds_unpaywall_candidate_and_deduplicates_urls():
    work = openalex_work()
    session = FakeSession(
        [
            FakeResponse(work),
            FakeResponse(
                {
                    "best_oa_location": {
                        "url_for_pdf": "https://repository.example/paper.pdf",
                        "url_for_landing_page": "https://repository.example/paper",
                        "license": "cc-by-nc",
                        "version": "acceptedVersion",
                        "host_type": "repository",
                    },
                    "oa_locations": [
                        {
                            "url_for_pdf": "https://example.org/paper.pdf",
                            "license": "cc-by",
                            "version": "publishedVersion",
                            "host_type": "publisher",
                        }
                    ],
                }
            ),
        ]
    )
    service = DiscoveryService(
        Settings(app_env="test", unpaywall_email="research@example.org"), session=session
    )

    paper = service.resolve(openalex_id="W123")

    assert len(paper.download_candidates) == 2
    assert {candidate.provider for candidate in paper.download_candidates} == {
        "openalex",
        "unpaywall",
    }
    assert any(candidate.access == "noncommercial_only" for candidate in paper.download_candidates)


def test_pdf_download_rejects_private_network_before_request(tmp_path):
    session = FakeSession([])
    service = PdfDownloadService(
        Settings(app_env="test", discovery_download_root=str(tmp_path)),
        session=session,
        resolver=lambda *_args: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://localhost/paper.pdf",
        access="commercial_allowed",
        is_oa=True,
    )

    with pytest.raises(PdfDownloadError, match="public network"):
        service.download("W123", candidate)

    assert session.calls == []


def test_pdf_download_fake_ip_dns_requires_opt_in_and_never_allows_ip_literal(tmp_path):
    def resolver(*_args):
        return [(None, None, None, None, ("198.18.1.120", 443))]

    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://aclanthology.org/paper.pdf",
        access="commercial_allowed",
        is_oa=True,
    )
    blocked = PdfDownloadService(
        Settings(
            app_env="test",
            discovery_download_root=str(tmp_path),
            discovery_storage_provider="local",
            discovery_allow_fake_ip_dns=False,
        ),
        session=FakeSession([]),
        resolver=resolver,
    )
    with pytest.raises(PdfDownloadError, match="public network"):
        blocked.download("W123", candidate)

    fake_ip_candidate = candidate.model_copy(update={"url": "https://198.18.1.120/paper.pdf"})
    opted_in = PdfDownloadService(
        Settings(
            app_env="test",
            discovery_download_root=str(tmp_path),
            discovery_allow_fake_ip_dns=True,
        ),
        session=FakeSession([]),
        resolver=resolver,
    )
    with pytest.raises(PdfDownloadError, match="must not contain"):
        opted_in.download("W123", fake_ip_candidate)


def test_pdf_download_validates_and_stores_by_sha256(tmp_path):
    pdf = b"%PDF-1.7\nexample"
    session = FakeSession(
        [FakeResponse(content=pdf, headers={"Content-Type": "application/pdf"})]
    )
    service = PdfDownloadService(
        Settings(
            app_env="test",
            discovery_download_root=str(tmp_path),
            discovery_storage_provider="local",
        ),
        session=session,
        resolver=lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    candidate = DiscoveryDownloadCandidate(
        provider="unpaywall",
        url="https://example.org/paper.pdf",
        access="commercial_allowed",
        license="cc-by",
        is_oa=True,
    )

    result = service.download("W123", candidate)

    path = Path(tmp_path, result.storage_key.removeprefix("downloads/"))
    assert path.read_bytes() == pdf
    assert path.suffix == ".pdf"
    assert Path(str(path).removesuffix(".pdf") + ".json").is_file()
    assert result.size_bytes == len(pdf)


def test_local_storage_copies_into_destination_before_atomic_replace(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "container-tmp"
    destination = tmp_path / "mounted-disk"
    source_dir.mkdir()
    source = source_dir / "download.part"
    source.write_bytes(b"%PDF-1.7\ncross-filesystem")
    original_replace = Path.replace
    replace_calls = []

    def same_directory_replace(current, target):
        target = Path(target)
        replace_calls.append((current, target))
        assert current.parent == target.parent
        return original_replace(current, target)

    monkeypatch.setattr(Path, "replace", same_directory_replace)
    storage = DiscoveryDownloadStorage(
        Settings(
            app_env="test",
            discovery_download_root=str(destination),
            discovery_storage_provider="local",
        )
    )

    duplicate = storage.store(
        source,
        "corpus/openalex/W123/paper.pdf",
        "corpus/openalex/W123/metadata.json",
        {"external_id": "W123"},
    )

    assert duplicate is False
    assert source.is_file()
    assert (destination / "corpus/openalex/W123/paper.pdf").read_bytes() == (
        b"%PDF-1.7\ncross-filesystem"
    )
    assert len(replace_calls) == 1
    assert not list(destination.rglob("*.part"))


@pytest.mark.parametrize("access", ["unknown", "restricted"])
def test_pdf_downloader_attempts_candidate_regardless_of_license_metadata(tmp_path, access):
    pdf = b"%PDF-1.7\nlicense metadata does not gate this file"
    service = PdfDownloadService(
        Settings(
            app_env="test",
            discovery_download_root=str(tmp_path),
            discovery_storage_provider="local",
        ),
        session=FakeSession([FakeResponse(content=pdf)]),
        resolver=lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://example.org/paper.pdf",
        access=access,
        license=None,
        is_oa=access != "restricted",
    )

    result = service.download("W123", candidate, allow_unknown_license=False)

    assert result.size_bytes == len(pdf)
    assert result.access == access


def test_pdf_download_stores_to_cos_and_deduplicates_by_object_key(tmp_path):
    pdf = b"%PDF-1.7\nexample"
    client = FakeCosClient()
    settings = Settings(
        app_env="test",
        discovery_storage_provider="cos",
        cos_bucket="papers-1234567890",
        cos_region="ap-shanghai",
        cos_secret_id="test-id",
        cos_secret_key="test-key",
        cos_prefix="literature-kb",
    )
    storage = DiscoveryDownloadStorage(settings, cos_client=client)
    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://example.org/paper.pdf",
        access="commercial_allowed",
        license="cc-by",
        is_oa=True,
    )

    def download_once():
        return PdfDownloadService(
            settings,
            session=FakeSession([FakeResponse(content=pdf)]),
            resolver=lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
            storage=storage,
        ).download("W123", candidate, doi="10.1000/example", title="An open paper")

    first = download_once()
    second = download_once()
    object_key = f"literature-kb/{first.storage_key}"

    assert client.objects[object_key] == pdf
    assert client.uploads == [object_key]
    assert client.puts == [
        f"literature-kb/{first.metadata_key}",
        f"literature-kb/{second.metadata_key}",
    ]
    assert first.duplicate is False
    assert second.duplicate is True


def test_cos_bucket_endpoint_is_normalized_for_the_tencent_sdk():
    settings = Settings(
        app_env="test",
        discovery_storage_provider="cos",
        cos_bucket="gibbs-papers-1334037784",
        cos_endpoint="https://gibbs-papers-1334037784.cos.ap-shanghai.myqcloud.com/",
    )

    assert DiscoveryDownloadStorage(settings)._sdk_endpoint() == "cos.ap-shanghai.myqcloud.com"


def test_cos_staged_pdf_can_be_streamed_back_for_ingestion(tmp_path):
    client = FakeCosClient()
    settings = Settings(
        app_env="test",
        discovery_storage_provider="cos",
        cos_bucket="papers-1234567890",
        cos_region="ap-shanghai",
        cos_secret_id="test-id",
        cos_secret_key="test-key",
        cos_prefix="literature-kb",
    )
    storage = DiscoveryDownloadStorage(settings, cos_client=client)
    client.objects["literature-kb/downloads/example.pdf"] = b"%PDF-1.7\nstaged"
    target = tmp_path / "restored.pdf"

    storage.download_to_path("downloads/example.pdf", target)

    assert target.read_bytes() == b"%PDF-1.7\nstaged"


@pytest.mark.parametrize(
    ("access", "license_value"),
    [
        ("commercial_allowed", "cc-by"),
        ("noncommercial_only", "cc-by-nc"),
        ("open_license_other", "cc-by-nd"),
        ("unknown", None),
        ("restricted", None),
    ],
)
def test_license_metadata_does_not_block_a_pdf_candidate(access, license_value):
    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://example.org/paper.pdf",
        access=access,
        license=license_value,
        is_oa=access != "restricted",
    )

    assert DiscoveryService.license_policy(candidate) == "allowed"


def test_import_candidates_include_unknown_and_restricted_without_acknowledgement():
    candidates = [
        DiscoveryDownloadCandidate(
            provider="openalex",
            url=f"https://example.org/{access}.pdf",
            access=access,
            is_oa=access != "restricted",
        )
        for access in ("unknown", "restricted")
    ]
    paper = DiscoveryPaper(external_id="W123", download_candidates=candidates)

    selected = DiscoveryService(Settings(app_env="test")).import_candidates(
        paper, license_acknowledged=False
    )

    assert {candidate.access for candidate in selected} == {"unknown", "restricted"}


def test_remote_fetch_does_not_persist_source_url_query_or_fragment():
    assert (
        RemoteFetchService.clean_url(
            "https://repository.example/paper?signature=secret#download"
        )
        == "https://repository.example/paper"
    )


def test_pdf_download_rejects_non_pdf_body(tmp_path):
    session = FakeSession(
        [FakeResponse(content=b"<html>sign in</html>", headers={"Content-Type": "text/html"})]
    )
    service = PdfDownloadService(
        Settings(app_env="test", discovery_download_root=str(tmp_path)),
        session=session,
        resolver=lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://example.org/login",
        access="commercial_allowed",
        is_oa=True,
    )

    with pytest.raises(PdfDownloadError, match="valid PDF"):
        service.download("W123", candidate)


def test_interrupted_pdf_stream_is_upstream_error_not_storage_failure(tmp_path):
    class InterruptedResponse(FakeResponse):
        def iter_content(self, chunk_size=1024):
            del chunk_size
            yield b"%PDF-1.7\npartial"
            raise requests.exceptions.ChunkedEncodingError("incomplete response body")

    service = PdfDownloadService(
        Settings(app_env="test", discovery_download_root=str(tmp_path)),
        session=FakeSession([InterruptedResponse()]),
        resolver=lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://example.org/interrupted.pdf",
        access="commercial_allowed",
        is_oa=True,
    )

    with pytest.raises(PdfDownloadError) as exc_info:
        service.download("W123", candidate)

    assert exc_info.value.code == "PDF_UPSTREAM_ERROR"
    assert exc_info.value.retryable is True
    assert exc_info.value.cause_type == "ChunkedEncodingError"
    assert not list(tmp_path.rglob("*.pdf"))


def test_local_storage_is_retried_before_redownloading(tmp_path, monkeypatch):
    class FlakyStorage:
        def __init__(self):
            self.calls = 0

        def store(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                raise DiscoveryStorageError("transient T7 write failure")
            return False

    storage = FlakyStorage()
    session = FakeSession([FakeResponse(content=b"%PDF-1.7\ncomplete")])
    service = PdfDownloadService(
        Settings(app_env="test", discovery_download_root=str(tmp_path)),
        session=session,
        resolver=lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
        storage=storage,
    )
    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://example.org/paper.pdf",
        access="commercial_allowed",
        is_oa=True,
    )
    monkeypatch.setattr("app.services.pdf_download_service.time.sleep", lambda *_: None)

    result = service.download("W123", candidate)

    assert result.size_bytes == len(b"%PDF-1.7\ncomplete")
    assert storage.calls == 3
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable"),
    [
        (403, "PDF_FORBIDDEN", False),
        (404, "PDF_NOT_FOUND", False),
        (410, "PDF_NOT_FOUND", False),
        (429, "PDF_RATE_LIMITED", True),
        (503, "PDF_UPSTREAM_ERROR", True),
    ],
)
def test_pdf_download_classifies_http_statuses_for_harvest_retries(
    tmp_path, status_code, expected_code, retryable
):
    service = PdfDownloadService(
        Settings(app_env="test", discovery_download_root=str(tmp_path)),
        session=FakeSession([FakeResponse(status_code=status_code)]),
        resolver=lambda *_args: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    candidate = DiscoveryDownloadCandidate(
        provider="openalex",
        url="https://example.org/paper.pdf",
        access="commercial_allowed",
        is_oa=True,
    )

    with pytest.raises(PdfDownloadError) as raised:
        service.download("W123", candidate)

    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable

    assert not list(tmp_path.glob("*.pdf"))
