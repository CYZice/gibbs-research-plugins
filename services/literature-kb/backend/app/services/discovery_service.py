from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree

import redis
import requests
from rq import get_current_job

from app.core.config import Settings
from app.core.ids import new_id
from app.db.session import SessionLocal
from app.models.operational_control import ProviderCallEvent
from app.schemas.discovery import (
    AccessClass,
    DiscoveryDownloadCandidate,
    DiscoveryPaper,
    DiscoverySearchResponse,
)
from app.services.trace import get_current_trace

OPENALEX_BASE_URL = "https://api.openalex.org"
CORE_BASE_URL = "https://api.core.ac.uk/v3"
ARXIV_API_URL = "https://export.arxiv.org/api/query"
UNPAYWALL_BASE_URL = "https://api.unpaywall.org/v2"


class _MetaTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "meta":
            return
        values = {str(key).lower(): value for key, value in attrs if value is not None}
        name = str(values.get("name") or values.get("property") or "").lower()
        content = str(values.get("content") or "").strip()
        if name and content:
            self.values.setdefault(name, []).append(content)


class DiscoveryError(RuntimeError):
    def __init__(
        self, code: str, message: str, status_code: int = 502, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class DiscoveryProvider(Protocol):
    source: str

    def search(self, query: str, **filters) -> DiscoverySearchResponse: ...

    def resolve(self, *, external_id: str | None = None, doi: str | None = None) -> DiscoveryPaper: ...

    def download_candidates(self, paper: DiscoveryPaper) -> list[DiscoveryDownloadCandidate]: ...


class OpenAlexProvider:
    source = "openalex"

    def __init__(self, discovery: DiscoveryService) -> None:
        self.discovery = discovery

    def search(self, query: str, **filters) -> DiscoverySearchResponse:
        external_id = self.discovery._openalex_query_id(query)
        doi = self.discovery._query_doi(query)
        if external_id or doi:
            paper = self.discovery._resolve_openalex(external_id=external_id, doi=doi)
            items = [paper]
            if filters.get("year_from") and (paper.year or 0) < int(filters["year_from"]):
                items = []
            if filters.get("year_to") and (paper.year or 9999) > int(filters["year_to"]):
                items = []
            if filters.get("open_access_only") and paper.open_access is not True:
                items = []
            if filters.get("publication_types") and paper.publication_type not in filters["publication_types"]:
                items = []
            if filters.get("languages") and paper.language not in filters["languages"]:
                items = []
            return DiscoverySearchResponse(
                source=self.source, items=items, total=len(items), next_cursor=None
            )
        return self.discovery._search_openalex(query, **filters)

    def resolve(self, *, external_id: str | None = None, doi: str | None = None) -> DiscoveryPaper:
        return self.discovery._resolve_openalex(external_id=external_id, doi=doi)

    def download_candidates(self, paper: DiscoveryPaper) -> list[DiscoveryDownloadCandidate]:
        return paper.download_candidates


class CoreProvider:
    source = "core"

    def __init__(self, discovery: DiscoveryService) -> None:
        self.discovery = discovery

    def search(self, query: str, **filters) -> DiscoverySearchResponse:
        if not self.discovery.settings.core_api_key:
            raise DiscoveryError("DISCOVERY_SOURCE_UNCONFIGURED", "CORE_API_KEY is required.", 503)
        offset = 0 if filters.get("cursor") in {None, "*"} else int(filters.get("cursor") or 0)
        clauses = [query]
        if filters.get("year_from"):
            clauses.append(f"yearPublished>={int(filters['year_from'])}")
        if filters.get("year_to"):
            clauses.append(f"yearPublished<={int(filters['year_to'])}")
        if filters.get("languages"):
            clauses.append(
                "(" + " OR ".join(
                    f'language.code:\"{value}\"' for value in filters["languages"]
                ) + ")"
            )
        limit = int(filters.get("page_size") or 25)
        payload = self.discovery._get_json(
            f"{CORE_BASE_URL}/search/works/",
            params={
                "q": " AND ".join(f"({value})" for value in clauses),
                "limit": limit,
                "offset": offset,
            },
            headers=self.discovery._core_headers(),
            source="CORE",
            timeout_seconds=self.discovery.settings.discovery_search_timeout_seconds,
            max_attempts=2,
        )
        items = [self.discovery._core_paper(item) for item in payload.get("results") or []]
        total = int(payload.get("totalHits") or len(items))
        next_offset = offset + len(items)
        return DiscoverySearchResponse(
            source=self.source,
            items=items,
            total=total,
            next_cursor=str(next_offset) if items and next_offset < total else None,
        )

    def resolve(self, *, external_id: str | None = None, doi: str | None = None) -> DiscoveryPaper:
        if not self.discovery.settings.core_api_key:
            raise DiscoveryError("DISCOVERY_SOURCE_UNCONFIGURED", "CORE_API_KEY is required.", 503)
        if external_id:
            payload = self.discovery._get_json(
                f"{CORE_BASE_URL}/works/{quote(str(external_id), safe='')}",
                params={},
                headers=self.discovery._core_headers(),
                source="CORE",
            )
            return self.discovery._core_paper(payload)
        normalized = self.discovery._normalize_doi(doi)
        result = self.search(f'doi:\"{normalized}\"', page_size=10, cursor="*")
        paper = next((item for item in result.items if item.doi == normalized), None)
        if not paper:
            raise DiscoveryError("EXTERNAL_WORK_NOT_FOUND", "CORE work not found.", 404)
        return paper

    def download_candidates(self, paper: DiscoveryPaper) -> list[DiscoveryDownloadCandidate]:
        return paper.download_candidates


class ArxivProvider:
    source = "arxiv"

    def __init__(self, discovery: DiscoveryService) -> None:
        self.discovery = discovery

    def search(self, query: str, **filters) -> DiscoverySearchResponse:
        if filters.get("languages"):
            raise DiscoveryError("UNSUPPORTED_FILTER", "arXiv does not support language filters.", 400)
        types = set(filters.get("publication_types") or [])
        if types and not types <= {"article", "preprint"}:
            raise DiscoveryError("UNSUPPORTED_FILTER", "arXiv only provides e-prints.", 400)
        arxiv_id = self.discovery._arxiv_query_id(query)
        if arxiv_id:
            result = self.discovery._arxiv_response(
                {"id_list": arxiv_id, "max_results": 1},
                timeout_seconds=self.discovery.settings.discovery_search_timeout_seconds,
            )
            items = result.items
            if filters.get("year_from"):
                items = [item for item in items if (item.year or 0) >= int(filters["year_from"])]
            if filters.get("year_to"):
                items = [item for item in items if (item.year or 9999) <= int(filters["year_to"])]
            return DiscoverySearchResponse(
                source=self.source, items=items, total=len(items), next_cursor=None
            )
        safe_query = query.replace('"', " ")
        clauses = [f'all:\"{safe_query}\"']
        if filters.get("year_from") or filters.get("year_to"):
            start_date = f"{int(filters.get('year_from') or 1991)}01010000"
            end_date = f"{int(filters.get('year_to') or 2200)}12312359"
            clauses.append(f"submittedDate:[{start_date} TO {end_date}]")
        start = 0 if filters.get("cursor") in {None, "*"} else int(filters.get("cursor") or 0)
        return self.discovery._arxiv_response(
            {
                "search_query": " AND ".join(clauses),
                "start": start,
                "max_results": int(filters.get("page_size") or 25),
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            timeout_seconds=self.discovery.settings.discovery_search_timeout_seconds,
        )

    def resolve(self, *, external_id: str | None = None, doi: str | None = None) -> DiscoveryPaper:
        if doi and not external_id:
            raise DiscoveryError("INVALID_IDENTIFIER", "arXiv resolve requires an arXiv ID.", 400)
        arxiv_id = self.discovery._normalize_arxiv_id(external_id or "")
        try:
            result = self.discovery._arxiv_response(
                {"id_list": arxiv_id, "max_results": 1}
            )
        except DiscoveryError as exc:
            if not exc.retryable:
                raise
            return self.discovery._resolve_arxiv_html(arxiv_id, warning=exc.code)
        if not result.items:
            raise DiscoveryError("EXTERNAL_WORK_NOT_FOUND", "arXiv work not found.", 404)
        return result.items[0]

    def download_candidates(self, paper: DiscoveryPaper) -> list[DiscoveryDownloadCandidate]:
        return paper.download_candidates


class DiscoveryService:
    def __init__(self, settings: Settings, session=None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.providers: dict[str, DiscoveryProvider] = {
            "openalex": OpenAlexProvider(self),
            "core": CoreProvider(self),
            "arxiv": ArxivProvider(self),
        }

    def provider(self, source: str) -> DiscoveryProvider:
        try:
            return self.providers[source.lower()]
        except KeyError as exc:
            raise DiscoveryError("DISCOVERY_SOURCE_UNSUPPORTED", f"Unsupported discovery source: {source}.", 400) from exc

    def search(self, source: str, query: str, **filters) -> DiscoverySearchResponse:
        try:
            result = self.provider(source).search(query, **filters)
        except DiscoveryError:
            raise
        except Exception as exc:
            raise DiscoveryError(
                "UPSTREAM_INVALID_RESPONSE", f"{source} returned invalid data.", 502, retryable=True
            ) from exc
        return result.model_copy(update={"source": source.lower()})

    def search_openalex(
        self,
        query: str,
        *,
        page_size: int = 25,
        cursor: str = "*",
        year_from: int | None = None,
        year_to: int | None = None,
        open_access_only: bool = True,
        publication_types: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> DiscoverySearchResponse:
        return self.search(
            "openalex", query, page_size=page_size, cursor=cursor, year_from=year_from,
            year_to=year_to, open_access_only=open_access_only,
            publication_types=publication_types, languages=languages,
        )

    def _search_openalex(
        self,
        query: str,
        *,
        page_size: int = 25,
        cursor: str = "*",
        year_from: int | None = None,
        year_to: int | None = None,
        open_access_only: bool = True,
        publication_types: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> DiscoverySearchResponse:
        filters = []
        if open_access_only:
            filters.append("open_access.is_oa:true")
        if year_from or year_to:
            filters.append(
                f"publication_year:{year_from or '*'}-{year_to or '*'}"
            )
        if publication_types:
            filters.append("type:" + "|".join(publication_types))
        if languages:
            filters.append("language:" + "|".join(languages))
        params = {
            "search": query,
            "per-page": page_size,
            "cursor": cursor,
        }
        if filters:
            params["filter"] = ",".join(filters)
        params.update(self._openalex_identity())
        payload = self._get_json(
            f"{OPENALEX_BASE_URL}/works",
            params=params,
            source="OpenAlex",
            timeout_seconds=self.settings.discovery_search_timeout_seconds,
            max_attempts=2,
        )
        meta = payload.get("meta") or {}
        items = [self._paper(item) for item in payload.get("results") or []]
        return DiscoverySearchResponse(
            items=items,
            total=int(meta.get("count") or len(items)),
            next_cursor=meta.get("next_cursor"),
        )

    def resolve(
        self,
        *,
        source: str = "openalex",
        external_id: str | None = None,
        doi: str | None = None,
        openalex_id: str | None = None,
    ) -> DiscoveryPaper:
        if external_id and openalex_id:
            raise DiscoveryError("INVALID_IDENTIFIER", "Provide only one external identifier.", 400)
        try:
            provider = self.provider(source)
            paper = provider.resolve(external_id=external_id or openalex_id, doi=doi)
            candidates = provider.download_candidates(paper)
        except DiscoveryError:
            raise
        except Exception as exc:
            raise DiscoveryError(
                "UPSTREAM_INVALID_RESPONSE", f"{source} returned invalid data.", 502, retryable=True
            ) from exc
        return paper.model_copy(update={"source": source.lower(), "download_candidates": candidates})

    def _resolve_openalex(self, *, external_id: str | None = None, doi: str | None = None) -> DiscoveryPaper:
        identifier = external_id or f"doi:{self._normalize_doi(doi or '')}"
        encoded = quote(identifier, safe=":")
        payload = self._get_json(
            f"{OPENALEX_BASE_URL}/works/{encoded}",
            params=self._openalex_identity(),
            source="OpenAlex",
        )
        paper = self._paper(payload)
        if paper.doi and self.settings.unpaywall_email:
            try:
                unpaywall = self._get_json(
                    f"{UNPAYWALL_BASE_URL}/{quote(paper.doi, safe='')}",
                    params={"email": self.settings.unpaywall_email},
                    source="Unpaywall",
                )
                paper.download_candidates = self._deduplicate_candidates(
                    [*paper.download_candidates, *self._unpaywall_candidates(unpaywall)]
                )
            except DiscoveryError as exc:
                paper.warnings.append(f"Unpaywall enrichment unavailable: {exc.code}")
        return paper

    def choose_download_candidate(
        self, paper: DiscoveryPaper, *, allow_unknown_license: bool = False
    ) -> DiscoveryDownloadCandidate:
        # ``allow_unknown_license`` is retained for API compatibility. Download
        # eligibility is determined only by whether a provider supplied a PDF URL.
        candidates = paper.download_candidates
        if not candidates:
            raise DiscoveryError(
                "NO_DOWNLOAD_CANDIDATE",
                "No PDF download candidate is available.",
                404,
            )
        version_rank = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
        return min(
            candidates,
            key=lambda item: (
                version_rank.get(item.version or "", 9),
                0 if item.host_type == "publisher" else 1,
            ),
        )

    @staticmethod
    def license_policy(candidate: DiscoveryDownloadCandidate) -> str:
        """Retained for response compatibility; a PDF candidate is always downloadable."""
        return "allowed"

    def choose_import_candidate(
        self, paper: DiscoveryPaper, *, license_acknowledged: bool = False
    ) -> DiscoveryDownloadCandidate:
        return self.import_candidates(
            paper, license_acknowledged=license_acknowledged
        )[0]

    def import_candidates(
        self, paper: DiscoveryPaper, *, license_acknowledged: bool = False
    ) -> list[DiscoveryDownloadCandidate]:
        # ``license_acknowledged`` is retained for API compatibility. Every
        # provider-supplied PDF candidate is attempted.
        candidates = paper.download_candidates
        if not candidates:
            raise DiscoveryError(
                "NO_DOWNLOAD_CANDIDATE",
                "No PDF download candidate is available.",
                404,
            )
        return sorted(candidates, key=self.import_candidate_rank)

    @classmethod
    def import_candidate_rank(cls, item: DiscoveryDownloadCandidate) -> tuple:
        return (
            {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}.get(
                item.version, 3
            ),
            0 if item.host_type == "publisher" else 1,
        )

    @classmethod
    def source_candidate_rank(
        cls, item: DiscoveryDownloadCandidate | None, source: str
    ) -> tuple:
        candidate_rank = cls.import_candidate_rank(item) if item else (9, 9)
        return (
            {"core": 0, "arxiv": 1, "openalex": 2}.get(source, 9),
            *candidate_rank,
        )

    def _paper(self, value: dict) -> DiscoveryPaper:
        ids = value.get("ids") or {}
        primary = value.get("primary_location") or value.get("best_oa_location") or {}
        source = primary.get("source") or {}
        candidates = self._openalex_candidates(value)
        arxiv_id = self._identifier_value(ids.get("arxiv"), "/abs/")
        if not arxiv_id:
            arxiv_id = next(
                (
                    normalized
                    for location in value.get("locations") or []
                    for url in (location.get("landing_page_url"), location.get("pdf_url"))
                    if (normalized := self._arxiv_query_id(url or ""))
                ),
                None,
            )
        return DiscoveryPaper(
            external_id=str(value.get("id") or "").rsplit("/", 1)[-1],
            doi=self._normalize_doi(value.get("doi")),
            arxiv_id=arxiv_id,
            pmid=self._identifier_value(ids.get("pmid"), "/"),
            pmcid=self._identifier_value(ids.get("pmcid"), "/"),
            title=value.get("display_name") or value.get("title"),
            authors=[
                {
                    "name": (row.get("author") or {}).get("display_name"),
                    "orcid": self._identifier_value((row.get("author") or {}).get("orcid"), "/"),
                }
                for row in value.get("authorships") or []
                if (row.get("author") or {}).get("display_name")
            ],
            year=value.get("publication_year"),
            publication_date=value.get("publication_date"),
            publication_type=value.get("type"),
            language=value.get("language"),
            venue=source.get("display_name"),
            abstract=self._abstract(value.get("abstract_inverted_index")),
            cited_by_count=int(value.get("cited_by_count") or 0),
            open_access=(value.get("open_access") or {}).get("is_oa"),
            oa_status=(value.get("open_access") or {}).get("oa_status"),
            landing_page_url=primary.get("landing_page_url"),
            download_candidates=candidates,
        )

    def _core_paper(self, value: dict) -> DiscoveryPaper:
        language = value.get("language")
        language_code = language.get("code") if isinstance(language, dict) else language
        arxiv_values = [value.get("arxivId"), *(value.get("oaiIds") or [])]
        for identifier in value.get("identifiers") or []:
            if isinstance(identifier, dict):
                arxiv_values.append(identifier.get("identifier"))
        arxiv_id = None
        for raw_value in arxiv_values:
            if not raw_value:
                continue
            normalized = re.sub(
                r"^oai:arxiv\.org:", "", str(raw_value).strip(), flags=re.IGNORECASE
            )
            arxiv_id = self._arxiv_query_id(normalized)
            if arxiv_id:
                break
        authors = []
        for author in value.get("authors") or []:
            name = author.get("name") if isinstance(author, dict) else str(author)
            if name:
                authors.append({"name": name, "orcid": None})
        urls = []
        if value.get("downloadUrl"):
            urls.append(value["downloadUrl"])
        urls.extend(value.get("sourceFulltextUrls") or [])
        for link in value.get("links") or []:
            if isinstance(link, dict) and link.get("type") in {"download", "pdf"} and link.get("url"):
                urls.append(link["url"])
        license_value = value.get("license")
        candidates = self._deduplicate_candidates([
            DiscoveryDownloadCandidate(
                provider="core",
                url=url,
                landing_page_url=value.get("fullTextIdentifier"),
                version=value.get("version"),
                license=license_value,
                access=self._access_class(license_value, True),
                is_oa=True,
                host_type="repository",
                source_name="CORE",
            )
            for url in urls if isinstance(url, str) and url.startswith("https://")
        ])
        publication_date = value.get("publishedDate") or value.get("datePublished")
        year = value.get("yearPublished") or value.get("year")
        if not year and publication_date:
            try:
                year = int(str(publication_date)[:4])
            except ValueError:
                year = None
        return DiscoveryPaper(
            source="core",
            external_id=str(value.get("id") or value.get("coreId") or ""),
            doi=self._normalize_doi(value.get("doi")),
            arxiv_id=arxiv_id,
            title=value.get("title"),
            authors=authors,
            year=int(year) if year else None,
            publication_date=str(publication_date)[:10] if publication_date and len(str(publication_date)) >= 10 else None,
            publication_type=(value.get("documentType") or {}).get("type")
            if isinstance(value.get("documentType"), dict) else value.get("documentType"),
            language=language_code,
            venue=value.get("publisher") or next(iter(value.get("journals") or []), None),
            abstract=value.get("abstract"),
            cited_by_count=int(value.get("citationCount") or 0),
            open_access=True,
            oa_status="open",
            landing_page_url=value.get("fullTextIdentifier") or value.get("downloadUrl"),
            download_candidates=candidates,
        )

    def _arxiv_response(
        self, params: dict, *, timeout_seconds: float | None = None
    ) -> DiscoverySearchResponse:
        timeout_seconds = timeout_seconds or self.settings.discovery_request_timeout_seconds
        rate_client = None
        if self.settings.app_env != "test":
            try:
                rate_client = redis.Redis.from_url(self.settings.redis_url, decode_responses=True)
                if not rate_client.set(
                    "discovery:rate:arxiv",
                    "1",
                    nx=True,
                    ex=max(1, int(timeout_seconds)) + 3,
                ):
                    raise DiscoveryError(
                        "UPSTREAM_RATE_LIMITED", "arXiv permits one request every three seconds.",
                        503, retryable=True,
                    )
            except redis.RedisError as exc:
                raise DiscoveryError(
                    "UPSTREAM_UNAVAILABLE", "arXiv rate limiter is unavailable.",
                    503, retryable=True,
                ) from exc
        started = time.perf_counter()
        try:
            response = self.session.get(
                ARXIV_API_URL,
                params=params,
                headers={"User-Agent": self.settings.discovery_user_agent},
                timeout=(min(5, timeout_seconds), timeout_seconds),
            )
        except requests.RequestException as exc:
            error = DiscoveryError(
                "UPSTREAM_TIMEOUT" if isinstance(exc, requests.Timeout) else "UPSTREAM_UNAVAILABLE",
                "arXiv request failed.", 503, retryable=True,
            )
            self._record_call("arxiv", ARXIV_API_URL, "failure", round((time.perf_counter() - started) * 1000), error)
            raise error from exc
        finally:
            if rate_client is not None:
                try:
                    rate_client.expire("discovery:rate:arxiv", 3)
                except redis.RedisError:
                    pass
        if response.status_code == 429:
            raise DiscoveryError("UPSTREAM_RATE_LIMITED", "arXiv rate limit exceeded.", 503, retryable=True)
        if response.status_code >= 400:
            raise DiscoveryError("UPSTREAM_ERROR", f"arXiv returned HTTP {response.status_code}.", 502)
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise DiscoveryError("UPSTREAM_INVALID_RESPONSE", "arXiv returned invalid Atom XML.", 502, retryable=True) from exc
        namespaces = {
            "atom": "http://www.w3.org/2005/Atom",
            "open": "http://a9.com/-/spec/opensearch/1.1/",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        items = [self._arxiv_paper(entry, namespaces) for entry in root.findall("atom:entry", namespaces)]
        total_node = root.find("open:totalResults", namespaces)
        start_node = root.find("open:startIndex", namespaces)
        per_page_node = root.find("open:itemsPerPage", namespaces)
        total = int(total_node.text or 0) if total_node is not None else len(items)
        start = int(start_node.text or 0) if start_node is not None else int(params.get("start") or 0)
        per_page = int(per_page_node.text or 0) if per_page_node is not None else len(items)
        self._record_call("arxiv", ARXIV_API_URL, "success", round((time.perf_counter() - started) * 1000))
        return DiscoverySearchResponse(
            source="arxiv", items=items, total=total,
            next_cursor=str(start + per_page) if items and start + per_page < total else None,
        )

    def _arxiv_paper(self, entry, namespaces: dict[str, str]) -> DiscoveryPaper:
        def text(path: str) -> str | None:
            node = entry.find(path, namespaces)
            return " ".join((node.text or "").split()) if node is not None and node.text else None

        entry_id = text("atom:id") or ""
        versioned_id = entry_id.rsplit("/", 1)[-1]
        arxiv_id = self._normalize_arxiv_id(versioned_id)
        links = [node.attrib for node in entry.findall("atom:link", namespaces)]
        pdf_url = next((link.get("href") for link in links if link.get("type") == "application/pdf"), None)
        landing_url = next((link.get("href") for link in links if link.get("rel") == "alternate"), None)
        license_value = text("arxiv:license")
        authors = [
            {"name": " ".join((node.text or "").split()), "orcid": None}
            for node in entry.findall("atom:author/atom:name", namespaces) if node.text
        ]
        published = text("atom:published")
        doi = text("arxiv:doi")
        primary = entry.find("arxiv:primary_category", namespaces)
        candidates = [DiscoveryDownloadCandidate(
            provider="arxiv", url=pdf_url, landing_page_url=landing_url,
            version="submittedVersion", license=license_value,
            access=self._access_class(license_value, True), is_oa=True,
            host_type="repository", source_name="arXiv",
        )] if pdf_url else []
        return DiscoveryPaper(
            source="arxiv", external_id=arxiv_id, doi=self._normalize_doi(doi), arxiv_id=arxiv_id,
            title=text("atom:title"), authors=authors,
            year=int(published[:4]) if published else None,
            publication_date=published[:10] if published else None,
            publication_type="preprint", language=None,
            venue=text("arxiv:journal_ref") or (primary.attrib.get("term") if primary is not None else "arXiv"),
            abstract=text("atom:summary"), open_access=True, oa_status="green",
            landing_page_url=landing_url, download_candidates=candidates,
        )

    def _resolve_arxiv_html(self, arxiv_id: str, *, warning: str) -> DiscoveryPaper:
        landing_url = f"https://arxiv.org/abs/{quote(arxiv_id, safe='.') }"
        try:
            response = self.session.get(
                landing_url,
                headers={"User-Agent": self.settings.discovery_user_agent},
                timeout=(5, self.settings.discovery_request_timeout_seconds),
            )
        except requests.RequestException as exc:
            raise DiscoveryError(
                "UPSTREAM_UNAVAILABLE",
                "arXiv abstract page request failed.",
                503,
                retryable=True,
            ) from exc
        status_code = response.status_code
        encoding = getattr(response, "encoding", None) or "utf-8"
        content = response.content
        response.close()
        if status_code == 404:
            raise DiscoveryError("EXTERNAL_WORK_NOT_FOUND", "arXiv work not found.", 404)
        if status_code >= 400:
            raise DiscoveryError(
                "UPSTREAM_ERROR",
                f"arXiv abstract page returned HTTP {status_code}.",
                502,
                retryable=status_code >= 500 or status_code == 429,
            )
        parser = _MetaTagParser()
        try:
            parser.feed(content.decode(encoding, errors="replace"))
        except (UnicodeError, ValueError) as exc:
            raise DiscoveryError(
                "UPSTREAM_INVALID_RESPONSE",
                "arXiv abstract page returned invalid HTML.",
                502,
                retryable=True,
            ) from exc
        values = parser.values
        title = next(iter(values.get("citation_title") or []), None)
        pdf_url = next(iter(values.get("citation_pdf_url") or []), None)
        if not title or not pdf_url:
            raise DiscoveryError(
                "UPSTREAM_INVALID_RESPONSE",
                "arXiv abstract page is missing citation metadata.",
                502,
                retryable=True,
            )
        date_value = next(iter(values.get("citation_date") or []), None)
        publication_date = None
        if date_value:
            for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%Y/%m", "%Y-%m"):
                try:
                    parsed = datetime.strptime(date_value, pattern)
                    publication_date = date(parsed.year, parsed.month, parsed.day)
                    break
                except ValueError:
                    continue
        doi_value = next(iter(values.get("citation_doi") or []), None)
        return DiscoveryPaper(
            source="arxiv",
            external_id=arxiv_id,
            doi=self._normalize_doi(doi_value),
            arxiv_id=arxiv_id,
            title=" ".join(title.split()),
            authors=[
                {"name": " ".join(author.split()), "orcid": None}
                for author in values.get("citation_author") or []
            ],
            year=publication_date.year if publication_date else None,
            publication_date=publication_date,
            publication_type="preprint",
            venue="arXiv",
            abstract=next(iter(values.get("description") or []), None),
            open_access=True,
            oa_status="green",
            landing_page_url=landing_url,
            download_candidates=[
                DiscoveryDownloadCandidate(
                    provider="arxiv",
                    url=pdf_url,
                    landing_page_url=landing_url,
                    version="submittedVersion",
                    license=None,
                    access="unknown",
                    is_oa=True,
                    host_type="repository",
                    source_name="arXiv",
                )
            ],
            warnings=[f"arXiv Atom API unavailable: {warning}"],
        )

    def _openalex_candidates(self, work: dict) -> list[DiscoveryDownloadCandidate]:
        locations = []
        if work.get("best_oa_location"):
            locations.append(work["best_oa_location"])
        locations.extend(work.get("locations") or [])
        candidates = []
        for location in locations:
            url = location.get("pdf_url")
            if not url:
                continue
            source = location.get("source") or {}
            version = location.get("version")
            if not version:
                version = (
                    "publishedVersion"
                    if location.get("is_published")
                    else "acceptedVersion"
                    if location.get("is_accepted")
                    else None
                )
            candidates.append(
                DiscoveryDownloadCandidate(
                    provider="openalex",
                    url=url,
                    landing_page_url=location.get("landing_page_url"),
                    version=version,
                    license=location.get("license"),
                    access=self._access_class(location.get("license"), bool(location.get("is_oa"))),
                    is_oa=bool(location.get("is_oa")),
                    host_type=source.get("type"),
                    source_name=source.get("display_name"),
                )
            )
        return self._deduplicate_candidates(candidates)

    def _unpaywall_candidates(self, payload: dict) -> list[DiscoveryDownloadCandidate]:
        locations = []
        if payload.get("best_oa_location"):
            locations.append(payload["best_oa_location"])
        locations.extend(payload.get("oa_locations") or [])
        return [
            DiscoveryDownloadCandidate(
                provider="unpaywall",
                url=item["url_for_pdf"],
                landing_page_url=item.get("url_for_landing_page"),
                version=item.get("version"),
                license=item.get("license"),
                access=self._access_class(item.get("license"), True),
                is_oa=True,
                host_type=item.get("host_type"),
                source_name=(item.get("endpoint") or {}).get("id"),
            )
            for item in locations
            if item.get("url_for_pdf")
        ]

    def _get_json(
        self,
        url: str,
        *,
        params: dict,
        headers: dict | None = None,
        source: str,
        timeout_seconds: float | None = None,
        max_attempts: int = 1,
    ) -> dict:
        started = time.perf_counter()
        try:
            payload = self._request_json(
                url,
                params=params,
                headers=headers,
                source=source,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        except DiscoveryError as exc:
            self._record_call(
                source,
                url,
                "failure",
                round((time.perf_counter() - started) * 1000),
                exc,
            )
            raise
        self._record_call(
            source,
            url,
            "success",
            round((time.perf_counter() - started) * 1000),
        )
        return payload

    def _request_json(
        self,
        url: str,
        *,
        params: dict,
        headers: dict | None = None,
        source: str,
        timeout_seconds: float | None = None,
        max_attempts: int = 1,
    ) -> dict:
        response = None
        deadline = (
            time.perf_counter() + timeout_seconds if timeout_seconds is not None else None
        )
        for attempt in range(max(1, max_attempts)):
            remaining = (
                max(0.05, deadline - time.perf_counter())
                if deadline is not None
                else self.settings.discovery_request_timeout_seconds
            )
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={"User-Agent": self.settings.discovery_user_agent, **(headers or {})},
                    timeout=(min(1, remaining), remaining),
                )
            except requests.RequestException as exc:
                if attempt + 1 < max_attempts:
                    continue
                raise DiscoveryError(
                    "UPSTREAM_TIMEOUT"
                    if isinstance(exc, requests.Timeout)
                    else "UPSTREAM_UNAVAILABLE",
                    f"{source} request failed.",
                    retryable=True,
                ) from exc
            if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt + 1 >= max_attempts:
                break
            retry_after = response.headers.get("Retry-After")
            delay = 0.1
            if retry_after:
                try:
                    delay = min(1.0, max(0.0, float(retry_after)))
                except ValueError:
                    try:
                        value = parsedate_to_datetime(retry_after)
                        if value.tzinfo is None:
                            value = value.replace(tzinfo=UTC)
                        delay = min(1.0, max(0.0, (value - datetime.now(UTC)).total_seconds()))
                    except (TypeError, ValueError):
                        pass
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.perf_counter()))
            if delay:
                time.sleep(delay)
        assert response is not None
        if response.status_code == 404:
            raise DiscoveryError("EXTERNAL_WORK_NOT_FOUND", f"{source} work not found.", 404)
        if response.status_code == 429:
            raise DiscoveryError(
                "UPSTREAM_RATE_LIMITED",
                f"{source} rate limit exceeded.",
                503,
                retryable=True,
            )
        if response.status_code >= 400:
            raise DiscoveryError(
                "UPSTREAM_ERROR",
                f"{source} returned HTTP {response.status_code}.",
                400 if response.status_code == 400 else 502,
                retryable=response.status_code >= 500,
            )
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise DiscoveryError(
                "UPSTREAM_INVALID_RESPONSE",
                f"{source} returned invalid JSON.",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise DiscoveryError(
                "UPSTREAM_INVALID_RESPONSE",
                f"{source} returned invalid data.",
                retryable=True,
            )
        return payload

    def _record_call(
        self,
        source: str,
        url: str,
        status: str,
        latency_ms: int,
        error: DiscoveryError | None = None,
    ) -> None:
        if self.settings.app_env == "test":
            return
        trace = get_current_trace()
        job = get_current_job()
        error_code = error.code if error else None
        error_code = {
            "UPSTREAM_RATE_LIMITED": "PROVIDER_RATE_LIMITED",
            "UPSTREAM_TIMEOUT": "PROVIDER_TIMEOUT",
        }.get(error_code, error_code)
        db = SessionLocal()
        try:
            db.add(
                ProviderCallEvent(
                    event_id=new_id("pevent"),
                    role="discovery",
                    provider=source.lower(),
                    model=None,
                    credential_id=None,
                    operation="search" if url.rstrip("/").endswith("works") else "resolve",
                    status=status,
                    error_code=error_code,
                    http_status=error.status_code if error else 200,
                    latency_ms=latency_ms,
                    attempt=1,
                    request_id=trace.request_id if trace else None,
                    trace_id=trace.trace_id if trace else None,
                    task_id=str(job.args[0]) if job and job.args else None,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _openalex_identity(self) -> dict[str, str]:
        values = {}
        if self.settings.openalex_api_key:
            values["api_key"] = self.settings.openalex_api_key
        if self.settings.discovery_contact_email:
            values["mailto"] = self.settings.discovery_contact_email
        return values

    def _core_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.core_api_key}"}

    @staticmethod
    def _normalize_doi(value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if normalized.lower().startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        return normalized.lower()

    @staticmethod
    def _identifier_value(value: str | None, marker: str) -> str | None:
        if not value:
            return None
        return value.rsplit(marker, 1)[-1]

    @staticmethod
    def _normalize_arxiv_id(value: str) -> str:
        normalized = value.strip().lower()
        normalized = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", normalized)
        normalized = re.sub(r"^arxiv:", "", normalized)
        normalized = re.sub(r"\.pdf$", "", normalized)
        return re.sub(r"v\d+$", "", normalized)

    @classmethod
    def _arxiv_query_id(cls, value: str) -> str | None:
        normalized = cls._normalize_doi(value) or ""
        if normalized.startswith("10.48550/arxiv."):
            normalized = normalized.removeprefix("10.48550/arxiv.")
        normalized = cls._normalize_arxiv_id(normalized)
        if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})", normalized):
            return normalized
        return None

    @staticmethod
    def _openalex_query_id(value: str) -> str | None:
        normalized = value.strip()
        normalized = re.sub(r"^https?://openalex\.org/", "", normalized, flags=re.IGNORECASE)
        return normalized.upper() if re.fullmatch(r"W\d+", normalized, re.IGNORECASE) else None

    @classmethod
    def _query_doi(cls, value: str) -> str | None:
        normalized = cls._normalize_doi(value)
        return normalized if normalized and re.fullmatch(r"10\.\d{4,9}/\S+", normalized) else None

    @staticmethod
    def _abstract(index: dict | None) -> str | None:
        if not index:
            return None
        positions = [(position, word) for word, offsets in index.items() for position in offsets]
        return " ".join(word for _, word in sorted(positions)) or None

    @staticmethod
    def _access_class(license_value: str | None, is_oa: bool) -> AccessClass:
        if not is_oa:
            return "restricted"
        if not license_value:
            return "unknown"
        normalized = license_value.lower().replace("_", "-")
        if "cc-by-nc" in normalized:
            return "noncommercial_only"
        if normalized in {"cc0", "public-domain"} or normalized.startswith("cc-by"):
            return "commercial_allowed"
        return "open_license_other"

    @staticmethod
    def _deduplicate_candidates(
        values: list[DiscoveryDownloadCandidate],
    ) -> list[DiscoveryDownloadCandidate]:
        unique = {}
        for item in values:
            parsed = urlsplit(item.url)
            key = urlunsplit(
                (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
            )
            unique.setdefault(key, item)
        return list(unique.values())
