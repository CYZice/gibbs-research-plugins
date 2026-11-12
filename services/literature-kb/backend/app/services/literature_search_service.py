from __future__ import annotations

import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from hashlib import sha256

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.paper import Paper
from app.models.paper_identifier import PaperIdentifier
from app.schemas.discovery import DiscoveryPaper, DiscoverySearchResponse
from app.schemas.v1 import (
    LiteratureSearchRequest,
    PaperFilters,
    PaperSearchRequest,
    SearchRange,
)
from app.services.discovery_service import DiscoveryError, DiscoveryService
from app.services.paper_search_service import (
    PaperSearchResult,
    PaperSearchService,
    normalized_identifier,
)

logger = logging.getLogger(__name__)
REMOTE_FETCH_LIMITS = {"openalex": 10, "core": 2, "arxiv": 1}


@dataclass(slots=True)
class FederatedHit:
    identity_key: str
    metadata: dict
    origins: list[dict]
    score: float
    local_paper: Paper | None = None
    remote: dict | None = None
    remote_options: list[dict] = field(default_factory=list)
    possible_duplicate: bool = False
    identities: set[str] = field(default_factory=set)
    record_keys: set[str] = field(default_factory=set)


class LiteratureSearchService:
    """Federate existing local search with registered remote discovery providers."""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        *,
        discovery: DiscoveryService | None = None,
        redis_client=None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.discovery = discovery or DiscoveryService(settings)
        self.redis = redis_client

    def search(self, request: LiteratureSearchRequest) -> dict:
        state, session_available = self._load_state(request)
        local_started = time.perf_counter()
        executor = None
        exhausted_sources = set(state.get("remote_exhausted") or [])
        selected_remote_sources = [source for source in request.sources if source != "local"]
        remote_sources = [source for source in selected_remote_sources if source not in exhausted_sources]
        remote_futures = {}
        future_cursors = {}
        cursors = state.setdefault(
            "remote_cursors", {"openalex": state.get("remote_cursor", "*")}
        )
        if remote_sources:
            executor = ThreadPoolExecutor(max_workers=len(remote_sources), thread_name_prefix="discovery-search")
            future_cursors = {source: cursors.get(source, "*") for source in remote_sources}
            remote_futures = {
                source: executor.submit(self._remote_page, request, source, future_cursors[source])
                for source in remote_sources
            }

        local_results: list[PaperSearchResult] = []
        local_has_more = False
        local_stop = int(state["local_offset"])
        if "local" in request.sources:
            filters = PaperFilters(
                year=SearchRange(
                    gte=request.filters.year_from, lte=request.filters.year_to
                )
                if request.filters.year_from or request.filters.year_to
                else None,
                language=request.filters.language,
                publication_type=request.filters.publication_types,
            )
            candidates, _ = PaperSearchService(self.db, self.settings).search(
                PaperSearchRequest(
                    query=request.query,
                    search_mode=request.search_mode,
                    filters=filters,
                    page=1,
                    page_size=100,
                )
            )
            if request.filters.open_access_only:
                candidates = [item for item in candidates if item.paper.open_access is True]
            start = int(state["local_offset"])
            stop = start + request.page_size * 2
            local_stop = stop
            local_results = candidates[start:stop]
        source_status = [
            {
                "source": "local",
                "status": "ok",
                "latency_ms": int((time.perf_counter() - local_started) * 1000),
                "cache": "miss",
                "error_code": None,
            }
        ] if "local" in request.sources else []
        source_status.extend(
            {
                "source": source,
                "status": "ok",
                "latency_ms": 0,
                "cache": "miss",
                "error_code": None,
            }
            for source in selected_remote_sources
            if source in exhausted_sources
        )

        remote_items: list[DiscoveryPaper] = []
        remote_has_more = False
        remote_pages: dict[str, DiscoverySearchResponse] = {}
        if remote_futures:
            deadline = local_started + self.settings.discovery_search_timeout_seconds
            for source, future in remote_futures.items():
                try:
                    result, remote_status = future.result(
                        timeout=max(0.01, deadline - time.perf_counter())
                    )
                except TimeoutError:
                    stale = self._stale_remote_page(
                        request,
                        source,
                        future_cursors[source],
                        "UPSTREAM_TIMEOUT",
                        int(self.settings.discovery_search_timeout_seconds * 1000),
                    )
                    if stale:
                        result, remote_status = stale
                    else:
                        result = DiscoverySearchResponse(source=source, items=[], total=0, next_cursor=None)
                        remote_status = {"source": source, "status": "timeout", "latency_ms": int(self.settings.discovery_search_timeout_seconds * 1000), "cache": "miss", "error_code": "UPSTREAM_TIMEOUT"}
                except Exception:
                    logger.exception("Unhandled discovery provider failure", extra={"source": source})
                    result = DiscoverySearchResponse(source=source, items=[], total=0, next_cursor=None)
                    remote_status = {"source": source, "status": "unavailable", "latency_ms": int((time.perf_counter() - local_started) * 1000), "cache": "miss", "error_code": "UPSTREAM_UNAVAILABLE"}
                source_status.append(remote_status)
                remote_items.extend(result.items)
                if remote_status["status"] in {"ok", "cached", "stale_cache"}:
                    remote_pages[source] = result
            executor.shutdown(wait=False, cancel_futures=True)

        hits = self._fuse(local_results, remote_items, request.query)
        seen = set(state.get("seen") or [])
        seen_records = set(state.get("seen_records") or [])
        page = []
        for hit in hits:
            if hit.identity_key in seen or hit.record_keys & seen_records:
                seen.add(hit.identity_key)
                seen_records.update(hit.record_keys)
                continue
            page.append(hit)
            seen.add(hit.identity_key)
            seen_records.update(hit.record_keys)
            if len(page) == request.page_size:
                break

        local_keys = {
            self._record_key("local", item.paper.paper_id) for item in local_results
        }
        if "local" in request.sources and local_keys <= seen_records:
            state["local_offset"] = local_stop
            local_has_more = local_stop < len(candidates)
        elif local_keys:
            local_has_more = True

        for source, result in remote_pages.items():
            page_keys = {
                self._record_key(source, paper.external_id) for paper in result.items
            }
            if page_keys <= seen_records:
                cursors[source] = result.next_cursor
                if result.next_cursor is None:
                    exhausted_sources.add(source)
                else:
                    remote_has_more = True
            else:
                cursors[source] = future_cursors[source]
                exhausted_sources.discard(source)
                remote_has_more = True

        state["remote_exhausted"] = sorted(exhausted_sources)
        state["seen"] = list(seen)[-500:]
        state["seen_records"] = list(seen_records)[-1000:]
        has_more = local_has_more or remote_has_more
        next_cursor = self._save_state(state) if has_more and session_available else None
        partial = any(item["status"] not in {"ok", "cached"} for item in source_status)
        return {
            "search_id": state["search_id"],
            "items": page,
            "next_cursor": next_cursor,
            "partial": partial,
            "source_status": source_status,
        }

    def _remote_page(
        self, request: LiteratureSearchRequest, source: str, cursor: str
    ) -> tuple[DiscoverySearchResponse, dict]:
        started = time.perf_counter()
        key = self._remote_cache_key(request, source, cursor)
        cached = self._cache_get(f"literature:remote:fresh:{key}")
        if cached:
            return DiscoverySearchResponse.model_validate(cached), {
                "source": source,
                "status": "cached",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "cache": "hit",
                "error_code": None,
            }
        try:
            search = getattr(self.discovery, "search", None)
            remote_page_size = min(
                REMOTE_FETCH_LIMITS.get(source, 10), request.page_size
            )
            result = (search(source, request.query, page_size=remote_page_size, cursor=cursor,
                year_from=request.filters.year_from, year_to=request.filters.year_to,
                open_access_only=request.filters.open_access_only,
                publication_types=request.filters.publication_types or None,
                languages=request.filters.language or None)
                if search else self.discovery.search_openalex(
                request.query,
                page_size=remote_page_size,
                cursor=cursor,
                year_from=request.filters.year_from,
                year_to=request.filters.year_to,
                open_access_only=request.filters.open_access_only,
                publication_types=request.filters.publication_types or None,
                languages=request.filters.language or None,
            ))
            payload = result.model_dump(mode="json")
            self._cache_set(
                f"literature:remote:fresh:{key}",
                payload,
                self.settings.discovery_cache_fresh_seconds,
            )
            self._cache_set(
                f"literature:remote:stale:{key}",
                payload,
                self.settings.discovery_cache_stale_seconds,
            )
            return result, {
                "source": source,
                "status": "ok",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "cache": "miss",
                "error_code": None,
            }
        except DiscoveryError as exc:
            stale = self._stale_remote_page(
                request, source, cursor, exc.code,
                int((time.perf_counter() - started) * 1000),
            )
            if stale:
                return stale
            status = (
                "unconfigured"
                if exc.code == "DISCOVERY_SOURCE_UNCONFIGURED"
                else
                "rate_limited"
                if exc.code == "UPSTREAM_RATE_LIMITED"
                else "timeout"
                if exc.code == "UPSTREAM_TIMEOUT"
                else "unsupported_filter"
                if exc.status_code == 400
                else "unavailable"
            )
            return DiscoverySearchResponse(source=source, items=[], total=0, next_cursor=None), {
                "source": source,
                "status": status,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "cache": "miss",
                "error_code": exc.code,
            }
        except Exception:
            logger.exception("Unhandled discovery provider failure", extra={"source": source})
            stale = self._stale_remote_page(
                request, source, cursor, "UPSTREAM_UNAVAILABLE",
                int((time.perf_counter() - started) * 1000),
            )
            if stale:
                return stale
            return DiscoverySearchResponse(source=source, items=[], total=0, next_cursor=None), {
                "source": source, "status": "unavailable",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "cache": "miss", "error_code": "UPSTREAM_UNAVAILABLE",
            }

    def _stale_remote_page(
        self,
        request: LiteratureSearchRequest,
        source: str,
        cursor: str,
        error_code: str,
        latency_ms: int,
    ) -> tuple[DiscoverySearchResponse, dict] | None:
        key = self._remote_cache_key(request, source, cursor)
        stale = self._cache_get(f"literature:remote:stale:{key}")
        if not stale:
            return None
        return DiscoverySearchResponse.model_validate(stale), {
            "source": source,
            "status": "stale_cache",
            "latency_ms": latency_ms,
            "cache": "stale",
            "error_code": error_code,
        }

    def _fuse(
        self,
        local: list[PaperSearchResult],
        remote: list[DiscoveryPaper],
        query: str | None = None,
    ) -> list[FederatedHit]:
        entries: list[FederatedHit] = []
        identity_map: dict[str, FederatedHit] = {}
        local_ids = self._local_identifiers([item.paper for item in local])
        for rank, result in enumerate(local, start=1):
            paper = result.paper
            identities = local_ids.get(paper.paper_id, set()) | self._paper_identities(paper)
            identity_key = self._primary_identity(identities, f"local:{paper.paper_id}")
            hit = FederatedHit(
                identity_key=identity_key,
                identities=identities,
                metadata=self._local_metadata(paper),
                origins=[{"source": "local", "id": paper.paper_id}],
                local_paper=paper,
                score=1 / (60 + rank),
                record_keys={self._record_key("local", paper.paper_id)},
            )
            entries.append(hit)
            for value in identities:
                identity_map[value] = hit

        source_order = {"openalex": 0, "core": 1, "arxiv": 2}
        remote = sorted(remote, key=lambda paper: source_order.get(paper.source, 9))
        source_ranks: dict[str, int] = {}
        for paper in remote:
            source_ranks[paper.source] = source_ranks.get(paper.source, 0) + 1
            rank = source_ranks[paper.source]
            identities = self._remote_identities(paper)
            matches = []
            for value in identities:
                existing = identity_map.get(value)
                if existing and all(existing is not item for item in matches):
                    matches.append(existing)
            hit = next((item for item in matches if item.local_paper), matches[0] if matches else None)
            for other in matches:
                if other is hit:
                    continue
                hit.score += other.score
                hit.identities.update(other.identities)
                hit.origins.extend(origin for origin in other.origins if origin not in hit.origins)
                hit.remote_options.extend(other.remote_options)
                hit.possible_duplicate |= other.possible_duplicate
                hit.record_keys.update(other.record_keys)
                if other in entries:
                    entries.remove(other)
            if hit is None:
                hit = FederatedHit(
                    identity_key=self._primary_identity(
                        identities, f"{paper.source}:{paper.external_id.lower()}"
                    ),
                    identities=identities,
                    metadata=self._remote_metadata(paper),
                    origins=[],
                    score=0.0,
                    possible_duplicate=any(
                        self._similar_title(
                            paper.title, paper.year,
                            item.metadata.get("title"), item.metadata.get("year"),
                        )
                        for item in entries
                    ),
                )
                entries.append(hit)
            else:
                incoming = self._remote_metadata(paper)
                for key, value in incoming.items():
                    current = hit.metadata.get(key)
                    if current is None or current == "" or current == []:
                        hit.metadata[key] = value
            hit.identities.update(identities)
            hit.record_keys.add(self._record_key(paper.source, paper.external_id))
            hit.score += 1 / (60 + rank)
            if not any(item["source"] == paper.source for item in hit.origins):
                hit.origins.append({"source": paper.source, "id": paper.external_id})
            policy, candidate = self._remote_policy(paper)
            option = {
                "source": paper.source,
                "external_id": paper.external_id,
                "downloadable": policy != "blocked",
                "license": candidate.license if candidate else None,
                "license_policy": policy,
                "version": candidate.version if candidate else None,
                "priority": 0,
                "landing_page_url": paper.landing_page_url,
                "_rank": DiscoveryService.source_candidate_rank(candidate, paper.source),
            }
            if not any(
                item["source"] == paper.source and item["external_id"] == paper.external_id
                for item in hit.remote_options
            ):
                hit.remote_options.append(option)
            for value in hit.identities:
                identity_map[value] = hit

        query_title = self._title_key(query, None).removesuffix(":")
        for hit in entries:
            hit.identity_key = self._primary_identity(hit.identities, hit.identity_key)
            if query_title and query_title == self._title_key(
                hit.metadata.get("title"), None
            ).removesuffix(":"):
                hit.score += 1.0
            hit.remote_options.sort(key=self._remote_option_rank)
            for index, option in enumerate(hit.remote_options):
                option["priority"] = len(hit.remote_options) - index
                option.pop("_rank", None)
            hit.remote = hit.remote_options[0] if hit.remote_options else None

        return sorted(
            entries,
            key=lambda item: (
                item.score,
                bool(item.local_paper and item.local_paper.evidence_status == "ready"),
            ),
            reverse=True,
        )

    @staticmethod
    def _remote_option_rank(option: dict) -> tuple:
        return option["_rank"]

    def _local_identifiers(self, papers: list[Paper]) -> dict[str, set[str]]:
        result = {paper.paper_id: set() for paper in papers}
        if not papers:
            return result
        rows = self.db.scalars(
            select(PaperIdentifier).where(
                PaperIdentifier.paper_id.in_([paper.paper_id for paper in papers])
            )
        ).all()
        for row in rows:
            result[row.paper_id].add(f"{row.identifier_type}:{row.normalized_value}")
        return result

    @staticmethod
    def _paper_identities(paper: Paper) -> set[str]:
        values = set()
        if paper.doi:
            values.add(f"doi:{normalized_identifier(paper.doi)}")
        if paper.arxiv_id:
            values.add(f"arxiv:{normalized_identifier(paper.arxiv_id)}")
        return values

    @staticmethod
    def _remote_identities(paper: DiscoveryPaper) -> set[str]:
        values = {f"{paper.source}:{paper.external_id.lower()}"}
        for kind, value in (
            ("doi", paper.doi),
            ("arxiv", paper.arxiv_id),
            ("pmid", paper.pmid),
            ("pmcid", paper.pmcid),
        ):
            if value:
                values.add(f"{kind}:{normalized_identifier(value)}")
        return values

    @staticmethod
    def _primary_identity(identities: set[str], fallback: str) -> str:
        for prefix in ("doi:", "arxiv:", "pmid:", "pmcid:"):
            value = next((item for item in identities if item.startswith(prefix)), None)
            if value:
                return value
        return fallback

    @staticmethod
    def _local_metadata(paper: Paper) -> dict:
        return {
            "title": paper.title,
            "authors": paper.authors or [],
            "year": paper.year,
            "publication_date": paper.publication_date,
            "doi": paper.doi,
            "arxiv_id": paper.arxiv_id,
            "venue": paper.venue,
            "abstract": paper.abstract,
            "publication_type": paper.publication_type,
            "language": paper.language,
            "open_access": paper.open_access,
        }

    @staticmethod
    def _remote_metadata(paper: DiscoveryPaper) -> dict:
        return {
            "title": paper.title,
            "authors": paper.authors,
            "year": paper.year,
            "publication_date": paper.publication_date,
            "doi": paper.doi,
            "arxiv_id": paper.arxiv_id,
            "pmid": paper.pmid,
            "pmcid": paper.pmcid,
            "venue": paper.venue,
            "abstract": paper.abstract,
            "publication_type": paper.publication_type,
            "language": paper.language,
            "open_access": paper.open_access,
        }

    @staticmethod
    def _remote_policy(paper: DiscoveryPaper):
        ranked = sorted(
            paper.download_candidates, key=DiscoveryService.import_candidate_rank
        )
        candidate = ranked[0] if ranked else None
        return (DiscoveryService.license_policy(candidate) if candidate else "blocked"), candidate

    @staticmethod
    def _title_key(title: str | None, year: int | None) -> str:
        return f"{re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', (title or '').casefold())}:{year or ''}"

    @staticmethod
    def _record_key(source: str, external_id: str) -> str:
        return f"{source.casefold()}:{external_id.casefold()}"

    @classmethod
    def _similar_title(
        cls,
        first_title: str | None,
        first_year: int | None,
        second_title: str | None,
        second_year: int | None,
    ) -> bool:
        if first_year and second_year and first_year != second_year:
            return False
        first = cls._title_key(first_title, None).removesuffix(":")
        second = cls._title_key(second_title, None).removesuffix(":")
        return bool(first and second and SequenceMatcher(None, first, second).ratio() >= 0.92)

    def _load_state(self, request: LiteratureSearchRequest) -> tuple[dict, bool]:
        request_hash = self._request_hash(request)
        if request.cursor:
            try:
                search_id, _ = request.cursor.split(":", 1)
                state = self._cache_get(f"literature:session:{search_id}")
                if not state or state.get("request_hash") != request_hash:
                    raise ValueError
                return state, True
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid or expired literature search cursor.") from exc
        return {
            "search_id": f"search_{uuid.uuid4().hex[:16]}",
            "request_hash": request_hash,
            "local_offset": 0,
            "remote_cursor": "*",
            "page": 0,
            "seen": [],
        }, self._redis_available()

    def _save_state(self, state: dict) -> str | None:
        state["page"] = int(state.get("page", 0)) + 1
        if not self._cache_set(
            f"literature:session:{state['search_id']}",
            state,
            self.settings.literature_search_session_seconds,
        ):
            return None
        return f"{state['search_id']}:{state['page']}"

    def _request_hash(self, request: LiteratureSearchRequest) -> str:
        payload = request.model_dump(mode="json", exclude={"cursor"})
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _remote_cache_key(self, request: LiteratureSearchRequest, source: str, cursor: str) -> str:
        payload = request.model_dump(mode="json", exclude={"cursor", "sources", "search_mode"})
        payload.update(source=source, cursor=cursor)
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _client(self):
        if self.redis is None:
            self.redis = redis.Redis.from_url(
                self.settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
        return self.redis

    def _redis_available(self) -> bool:
        try:
            self._client().ping()
            return True
        except redis.RedisError:
            return False

    def _cache_get(self, key: str):
        try:
            value = self._client().get(key)
            return json.loads(value) if value else None
        except (redis.RedisError, TypeError, ValueError):
            return None

    def _cache_set(self, key: str, value: dict, ttl: int) -> bool:
        try:
            self._client().setex(key, ttl, json.dumps(value, ensure_ascii=False, default=str))
            return True
        except redis.RedisError:
            return False
