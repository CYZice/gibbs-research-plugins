import json
import time
from types import SimpleNamespace

from app.core.config import Settings
from app.models.paper import Paper
from app.schemas.discovery import (
    DiscoveryDownloadCandidate,
    DiscoveryPaper,
    DiscoverySearchResponse,
)
from app.schemas.v1 import LiteratureSearchRequest
from app.services.discovery_service import DiscoveryError, DiscoveryService
from app.services.literature_search_service import LiteratureSearchService
from app.services.paper_search_service import PaperSearchResult, PaperSearchService
from app.services.remote_fetch_service import RemoteFetchService


class FakeRedis:
    def __init__(self):
        self.values = {}

    def ping(self):
        return True

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, _ttl, value):
        self.values[key] = value


class FakeDb:
    def scalars(self, _statement):
        return SimpleNamespace(all=lambda: [])


def remote_paper() -> DiscoveryPaper:
    return DiscoveryPaper(
        external_id="W123",
        doi="10.1000/example",
        title="An open paper",
        year=2025,
        open_access=True,
        download_candidates=[
            DiscoveryDownloadCandidate(
                provider="openalex",
                url="https://example.org/paper.pdf",
                license="cc-by",
                access="commercial_allowed",
                is_oa=True,
            )
        ],
    )


def test_fusion_merges_exact_doi_and_prefers_local_identity():
    local = Paper(
        paper_id="paper_1",
        title="An open paper",
        doi="10.1000/example",
        year=2025,
        parse_status="succeeded",
        evidence_status="ready",
        embedding_status="ready",
    )
    service = LiteratureSearchService(
        FakeDb(), Settings(app_env="test"), redis_client=FakeRedis()
    )

    hits = service._fuse(
        [PaperSearchResult(local, 0.9, ["title"])], [remote_paper()]
    )

    assert len(hits) == 1
    assert hits[0].identity_key == "doi:10.1000/example"
    assert hits[0].local_paper is local
    assert {origin["source"] for origin in hits[0].origins} == {"local", "openalex"}
    assert hits[0].remote["license_policy"] == "allowed"


def test_fusion_transitively_merges_all_remote_sources_and_keeps_options():
    core = remote_paper().model_copy(update={
        "source": "core", "external_id": "C1", "arxiv_id": None,
        "download_candidates": [remote_paper().download_candidates[0].model_copy(
            update={"provider": "core"}
        )],
    })
    arxiv = remote_paper().model_copy(update={
        "source": "arxiv", "external_id": "2401.00001", "doi": None,
        "arxiv_id": "2401.00001",
        "download_candidates": [remote_paper().download_candidates[0].model_copy(
            update={"provider": "arxiv", "version": "submittedVersion", "license": None, "access": "unknown"}
        )],
    })
    bridge = remote_paper().model_copy(update={
        "source": "openalex", "external_id": "W1", "arxiv_id": "2401.00001",
    })
    service = LiteratureSearchService(
        FakeDb(), Settings(app_env="test"), redis_client=FakeRedis()
    )

    hits = service._fuse([], [core, arxiv, bridge])

    assert len(hits) == 1
    assert {option["source"] for option in hits[0].remote_options} == {"core", "arxiv", "openalex"}
    assert hits[0].remote["source"] == "core"


def test_displayed_recommendation_uses_the_same_candidate_rank_as_import():
    submitted = remote_paper().download_candidates[0].model_copy(
        update={"provider": "core", "version": "submittedVersion"}
    )
    published = submitted.model_copy(update={"version": "publishedVersion"})
    accepted = submitted.model_copy(
        update={"provider": "arxiv", "version": "acceptedVersion"}
    )
    core = remote_paper().model_copy(update={
        "source": "core",
        "external_id": "C1",
        "download_candidates": [submitted, published],
    })
    arxiv = remote_paper().model_copy(update={
        "source": "arxiv",
        "external_id": "A1",
        "download_candidates": [accepted],
    })
    service = LiteratureSearchService(
        FakeDb(), Settings(app_env="test"), redis_client=FakeRedis()
    )

    hit = service._fuse([], [core, arxiv])[0]
    ranked_sources = []
    for paper in (core, arxiv):
        candidate = RemoteFetchService.best_candidate(
            DiscoveryService(Settings(app_env="test")), paper
        )
        ranked_sources.append((
            DiscoveryService.source_candidate_rank(candidate, paper.source),
            paper.source,
        ))

    assert hit.remote["source"] == sorted(ranked_sources)[0][1] == "core"
    assert hit.remote["version"] == "publishedVersion"


def test_provider_ranks_are_independent_and_exact_title_wins():
    openalex = [
        remote_paper().model_copy(update={
            "source": "openalex",
            "external_id": f"W{index}",
            "doi": None,
            "title": f"Unrelated result {index}",
        })
        for index in range(40)
    ]
    arxiv = remote_paper().model_copy(update={
        "source": "arxiv",
        "external_id": "1706.03762",
        "doi": None,
        "arxiv_id": "1706.03762",
        "title": "Attention Is All You Need",
        "download_candidates": [remote_paper().download_candidates[0].model_copy(
            update={"provider": "arxiv"}
        )],
    })
    service = LiteratureSearchService(
        FakeDb(), Settings(app_env="test"), redis_client=FakeRedis()
    )

    hits = service._fuse([], [*openalex, arxiv], "Attention Is All You Need")
    base = service._fuse([], [openalex[0], arxiv])

    assert hits[0].identity_key == "arxiv:1706.03762"
    assert hits[0].score > 1
    assert base[0].score == base[1].score == 1 / 61
    assert hits[0].origins == [{"source": "arxiv", "id": "1706.03762"}]


def test_remote_provider_fetch_uses_source_specific_caps():
    class Discovery:
        def __init__(self):
            self.page_sizes = []

        def search(self, source, *_args, **kwargs):
            self.page_sizes.append((source, kwargs["page_size"]))
            return DiscoverySearchResponse(source=source, items=[], total=0)

    discovery = Discovery()
    service = LiteratureSearchService(
        FakeDb(),
        Settings(app_env="test"),
        discovery=discovery,
        redis_client=FakeRedis(),
    )

    service._remote_page(
        LiteratureSearchRequest(query="paper", sources=["core"], page_size=20),
        "core",
        "*",
    )
    service._remote_page(
        LiteratureSearchRequest(query="paper", sources=["arxiv"], page_size=20),
        "arxiv",
        "*",
    )
    service._remote_page(
        LiteratureSearchRequest(query="paper", sources=["openalex"], page_size=20),
        "openalex",
        "*",
    )

    assert discovery.page_sizes == [("core", 2), ("arxiv", 1), ("openalex", 10)]


def test_federated_pagination_replays_cached_provider_pages_without_losing_hits():
    class Discovery:
        def search(self, source, *_args, **_kwargs):
            if source == "arxiv":
                return DiscoverySearchResponse(
                    source=source,
                    items=[remote_paper().model_copy(update={
                        "source": source,
                        "external_id": "1706.03762",
                        "doi": None,
                        "arxiv_id": "1706.03762",
                        "title": "Attention Is All You Need",
                    })],
                    total=1,
                )
            return DiscoverySearchResponse(
                source=source,
                items=[
                    remote_paper().model_copy(update={
                        "source": source,
                        "external_id": f"W{index}",
                        "doi": None,
                        "title": f"OpenAlex result {index}",
                    })
                    for index in range(40)
                ],
                total=40,
            )

    service = LiteratureSearchService(
        FakeDb(),
        Settings(app_env="test"),
        discovery=Discovery(),
        redis_client=FakeRedis(),
    )
    request = LiteratureSearchRequest(
        query="Attention Is All You Need",
        sources=["openalex", "arxiv"],
        page_size=20,
    )

    pages = []
    while True:
        result = service.search(request)
        pages.extend(result["items"])
        if not result["next_cursor"]:
            break
        request = request.model_copy(update={"cursor": result["next_cursor"]})

    assert len(pages) == 41
    assert len({next(iter(hit.record_keys)) for hit in pages}) == 41
    assert pages[0].metadata["title"] == "Attention Is All You Need"


def test_unexpected_provider_failure_is_isolated():
    class Discovery:
        def search(self, source, *_args, **_kwargs):
            if source == "core":
                raise RuntimeError("broken parser")
            return DiscoverySearchResponse(
                source=source,
                items=[remote_paper().model_copy(update={"source": source})],
                total=1,
            )

    service = LiteratureSearchService(
        FakeDb(), Settings(app_env="test"), discovery=Discovery(), redis_client=FakeRedis()
    )
    result = service.search(
        LiteratureSearchRequest(query="paper", sources=["core", "openalex"])
    )

    assert len(result["items"]) == 1
    assert result["partial"] is True
    assert {item["source"]: item["status"] for item in result["source_status"]} == {
        "core": "unavailable", "openalex": "ok"
    }


def test_similar_title_is_only_marked_as_possible_duplicate():
    local = Paper(
        paper_id="paper_1",
        title="Retrieval Augmented Generation for Science",
        year=2025,
    )
    remote = remote_paper().model_copy(
        update={
            "external_id": "W999",
            "doi": "10.1000/different",
            "title": "Retrieval-Augmented Generation for Science",
        }
    )
    service = LiteratureSearchService(
        FakeDb(), Settings(app_env="test"), redis_client=FakeRedis()
    )

    hits = service._fuse([PaperSearchResult(local, 0.9, ["title"])], [remote])

    assert len(hits) == 2
    remote_hit = next(hit for hit in hits if hit.remote)
    assert remote_hit.possible_duplicate is True
    assert remote_hit.local_paper is None


def test_remote_failure_returns_local_results_as_partial(monkeypatch):
    local = Paper(
        paper_id="paper_1",
        title="Local result",
        parse_status="succeeded",
        evidence_status="ready",
        embedding_status="ready",
    )

    class UnavailableDiscovery:
        def search_openalex(self, *_args, **_kwargs):
            raise DiscoveryError("UPSTREAM_UNAVAILABLE", "offline", retryable=True)

    monkeypatch.setattr(
        PaperSearchService,
        "search",
        lambda *_args, **_kwargs: ([PaperSearchResult(local, 1.0, ["title"])], False),
    )
    service = LiteratureSearchService(
        FakeDb(),
        Settings(app_env="test"),
        discovery=UnavailableDiscovery(),
        redis_client=FakeRedis(),
    )

    result = service.search(LiteratureSearchRequest(query="local result"))

    assert [hit.local_paper.paper_id for hit in result["items"]] == ["paper_1"]
    assert result["partial"] is True
    assert result["source_status"][1]["status"] == "unavailable"


def test_remote_failure_uses_stale_cache():
    class Discovery:
        fail = False

        def search_openalex(self, *_args, **_kwargs):
            if self.fail:
                raise DiscoveryError("UPSTREAM_UNAVAILABLE", "offline", retryable=True)
            return DiscoverySearchResponse(items=[remote_paper()], total=1)

    redis = FakeRedis()
    discovery = Discovery()
    service = LiteratureSearchService(
        FakeDb(),
        Settings(app_env="test"),
        discovery=discovery,
        redis_client=redis,
    )
    request = LiteratureSearchRequest(query="open paper", sources=["openalex"])
    first, first_status = service._remote_page(request, "openalex", "*")
    fresh_key = next(key for key in redis.values if ":fresh:" in key)
    redis.values.pop(fresh_key)
    discovery.fail = True

    cached, cached_status = service._remote_page(request, "openalex", "*")

    assert first.items[0].external_id == cached.items[0].external_id
    assert first_status["status"] == "ok"
    assert cached_status["status"] == "stale_cache"
    assert cached_status["cache"] == "stale"


def test_exhausted_provider_is_not_restarted_while_another_provider_continues():
    class Discovery:
        def __init__(self):
            self.calls = []

        def search(self, source, *_args, **kwargs):
            self.calls.append((source, kwargs["cursor"]))
            next_cursor = "1" if source == "arxiv" and kwargs["cursor"] == "*" else None
            return DiscoverySearchResponse(
                source=source,
                items=[remote_paper().model_copy(update={
                    "source": source,
                    "external_id": f"{source}-{kwargs['cursor']}",
                    "doi": None,
                })],
                total=2 if source == "arxiv" else 1,
                next_cursor=next_cursor,
            )

    redis = FakeRedis()
    discovery = Discovery()
    service = LiteratureSearchService(
        FakeDb(), Settings(app_env="test"), discovery=discovery, redis_client=redis
    )
    request = LiteratureSearchRequest(query="paper", sources=["core", "arxiv"])

    first = service.search(request)
    service.search(request.model_copy(update={"cursor": first["next_cursor"]}))

    assert discovery.calls == [("core", "*"), ("arxiv", "*"), ("arxiv", "1")]


def test_federation_deadline_uses_stale_cache(monkeypatch):
    redis = FakeRedis()
    service = LiteratureSearchService(
        FakeDb(),
        Settings(app_env="test", discovery_search_timeout_seconds=0.01),
        redis_client=redis,
    )
    request = LiteratureSearchRequest(query="slow paper", sources=["core"])
    key = service._remote_cache_key(request, "core", "*")
    redis.values[f"literature:remote:stale:{key}"] = json.dumps(
        DiscoverySearchResponse(
            source="core", items=[remote_paper().model_copy(update={"source": "core"})], total=1
        ).model_dump(mode="json")
    )

    def slow_page(*_args):
        time.sleep(0.05)
        return DiscoverySearchResponse(source="core", items=[], total=0), {}

    monkeypatch.setattr(service, "_remote_page", slow_page)
    result = service.search(request)

    assert result["source_status"][0]["status"] == "stale_cache"
    assert result["items"][0].remote["source"] == "core"


def test_federation_deadline_keeps_fast_source_when_another_times_out():
    class Discovery:
        def search(self, source, *_args, **_kwargs):
            if source == "core":
                time.sleep(0.08)
            return DiscoverySearchResponse(
                source=source,
                items=[remote_paper().model_copy(update={
                    "source": source, "external_id": source, "doi": None,
                })],
                total=1,
            )

    service = LiteratureSearchService(
        FakeDb(),
        Settings(app_env="test", discovery_search_timeout_seconds=0.05),
        discovery=Discovery(),
        redis_client=FakeRedis(),
    )

    result = service.search(
        LiteratureSearchRequest(query="paper", sources=["core", "openalex"])
    )

    assert [hit.remote["source"] for hit in result["items"]] == ["openalex"]
    assert {status["source"]: status["status"] for status in result["source_status"]} == {
        "core": "timeout",
        "openalex": "ok",
    }
    assert result["partial"] is True
