from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.schemas.discovery import (
    DiscoveryDownloadRequest,
    DiscoveryDownloadResponse,
    DiscoveryPaper,
    DiscoveryResolveRequest,
    DiscoverySearchRequest,
    DiscoverySearchResponse,
    DiscoverySourcesResponse,
)
from app.services.discovery_service import DiscoveryError, DiscoveryService
from app.services.pdf_download_service import PdfDownloadError, PdfDownloadService

router = APIRouter(prefix="/api/v1/discovery", tags=["External Discovery"])


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"


def _json(data: Any, request: Request, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(data),
        headers={"X-Request-ID": _request_id(request)},
    )


def _error(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "request_id": request_id,
            }
        },
        headers={"X-Request-ID": request_id},
    )


@router.get("/sources", response_model=DiscoverySourcesResponse)
def sources(request: Request):
    settings = get_settings()
    discovery = DiscoveryService(settings)
    return _json(
        {
            "items": [
                {
                    "source": source,
                    "role": "search_and_resolve",
                    "configured": bool(settings.core_api_key) if source == "core" else True,
                    "requires_key": source == "core",
                    "notes": (
                        "CORE_API_KEY is required."
                        if source == "core"
                        else "Requests are globally limited to one every three seconds."
                        if source == "arxiv"
                        else "A free API key is recommended for usable daily limits."
                    ),
                }
                for source in discovery.providers
            ] + [
                {
                    "source": "unpaywall",
                    "role": "doi_resolver",
                    "configured": bool(settings.unpaywall_email),
                    "requires_key": False,
                    "notes": "A contact email is required by the public API.",
                },
            ]
        },
        request,
    )


@router.post("/search", response_model=DiscoverySearchResponse)
def search(body: DiscoverySearchRequest, request: Request):
    try:
        result = DiscoveryService(get_settings()).search(
            body.source,
            body.query,
            page_size=body.page_size,
            cursor=body.cursor,
            year_from=body.year_from,
            year_to=body.year_to,
            open_access_only=body.open_access_only,
            publication_types=body.publication_types,
        )
        return _json(result, request)
    except DiscoveryError as exc:
        return _error(request, exc.code, str(exc), exc.status_code)


@router.post("/works/resolve", response_model=DiscoveryPaper)
def resolve(body: DiscoveryResolveRequest, request: Request):
    try:
        result = DiscoveryService(get_settings()).resolve(
            source=body.source, external_id=body.external_id, openalex_id=body.openalex_id, doi=body.doi
        )
        return _json(result, request)
    except DiscoveryError as exc:
        return _error(request, exc.code, str(exc), exc.status_code)


@router.post(
    "/downloads",
    response_model=DiscoveryDownloadResponse,
    status_code=status.HTTP_201_CREATED,
)
def download(body: DiscoveryDownloadRequest, request: Request):
    settings = get_settings()
    discovery = DiscoveryService(settings)
    try:
        paper = discovery.resolve(
            source=body.source, external_id=body.external_id, openalex_id=body.openalex_id, doi=body.doi
        )
        last_download_error = None
        for candidate in discovery.import_candidates(paper):
            try:
                result = PdfDownloadService(settings).download(
                    paper.external_id,
                    candidate,
                    doi=paper.doi,
                    title=paper.title,
                )
                return _json(result, request, status.HTTP_201_CREATED)
            except PdfDownloadError as exc:
                last_download_error = exc
        if last_download_error:
            raise last_download_error
        raise DiscoveryError("NO_DOWNLOAD_CANDIDATE", "No PDF download candidate is available.", 404)
    except (DiscoveryError, PdfDownloadError) as exc:
        return _error(request, exc.code, str(exc), exc.status_code)
