from __future__ import annotations

import ipaddress
import socket
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from app.core.config import Settings
from app.schemas.discovery import DiscoveryDownloadCandidate, DiscoveryDownloadResponse
from app.services.discovery_download_storage import DiscoveryDownloadStorage, DiscoveryStorageError

FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class PdfDownloadError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.cause_type = type(cause).__name__ if cause is not None else None
        self.cause_errno = getattr(cause, "errno", None)
        self.retryable = code in {
            "PDF_UPSTREAM_UNAVAILABLE",
            "PDF_RATE_LIMITED",
            "PDF_UPSTREAM_ERROR",
            "PDF_HOST_UNRESOLVED",
            "PDF_STORAGE_FAILED",
        }


class PdfDownloadService:
    def __init__(self, settings: Settings, session=None, resolver=None, storage=None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.resolver = resolver or socket.getaddrinfo
        self.storage = storage or DiscoveryDownloadStorage(settings)

    def download(
        self,
        external_id: str,
        candidate: DiscoveryDownloadCandidate,
        *,
        doi: str | None = None,
        title: str | None = None,
        allow_unknown_license: bool = False,
        storage_key: str | None = None,
        metadata_key: str | None = None,
        metadata_payload: dict | None = None,
    ) -> DiscoveryDownloadResponse:
        # ``allow_unknown_license`` is retained for API compatibility. The
        # downloader attempts every provider-supplied URL and relies on network,
        # size, and PDF-content validation as the technical gates.
        response, final_url = self._open(candidate.url)
        temporary_path = None
        try:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > self.settings.discovery_max_download_bytes:
                raise PdfDownloadError(
                    "PDF_TOO_LARGE", "PDF exceeds the configured size limit.", 413
                )
            digest = sha256()
            size = 0
            first_bytes = b""
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", suffix=".part", delete=False
                ) as output:
                    temporary_path = Path(output.name)
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        if not first_bytes:
                            first_bytes = chunk[:8]
                        size += len(chunk)
                        if size > self.settings.discovery_max_download_bytes:
                            raise PdfDownloadError(
                                "PDF_TOO_LARGE",
                                "PDF exceeds the configured size limit.",
                                413,
                            )
                        digest.update(chunk)
                        output.write(chunk)
            except requests.RequestException as exc:
                raise PdfDownloadError(
                    "PDF_UPSTREAM_ERROR",
                    "PDF response body was interrupted before completion.",
                    502,
                    cause=exc,
                ) from exc
            except OSError as exc:
                raise PdfDownloadError(
                    "PDF_STORAGE_FAILED",
                    "Could not write the temporary PDF.",
                    500,
                    cause=exc,
                ) from exc
            if not first_bytes.startswith(b"%PDF-"):
                raise PdfDownloadError(
                    "INVALID_PDF", "Downloaded response is not a valid PDF file.", 502
                )
            checksum = digest.hexdigest()
            storage_key = storage_key or f"downloads/{checksum}.pdf"
            metadata_key = metadata_key or f"downloads/{checksum}.json"
            stored_metadata = {
                "path": Path(storage_key).name,
                "external_id": external_id,
                "doi": doi,
                "title": title,
                "source_provider": candidate.provider,
                "source_url": self._redact_url(final_url),
                "license": candidate.license,
                "access": candidate.access,
                "sha256": checksum,
                "size_bytes": size,
            }
            if metadata_payload is not None:
                stored_metadata["openalex"] = metadata_payload
            duplicate = False
            stored = False
            storage_error = None
            for attempt in range(3):
                try:
                    duplicate = self.storage.store(
                        temporary_path,
                        storage_key,
                        metadata_key,
                        stored_metadata,
                    )
                    stored = True
                    break
                except (DiscoveryStorageError, OSError) as exc:
                    storage_error = exc
                    if attempt < 2:
                        time.sleep(0.25 * (2**attempt))
                except ValueError as exc:
                    storage_error = exc
                    break
            if not stored:
                raise PdfDownloadError(
                    "PDF_STORAGE_FAILED",
                    "Could not store downloaded PDF.",
                    500,
                    cause=storage_error,
                ) from storage_error
            return DiscoveryDownloadResponse(
                external_id=external_id,
                doi=doi,
                storage_key=storage_key,
                metadata_key=metadata_key,
                sha256=checksum,
                size_bytes=size,
                source_provider=candidate.provider,
                source_url=self._redact_url(final_url),
                license=candidate.license,
                access=candidate.access,
                duplicate=duplicate,
            )
        finally:
            response.close()
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def _open(self, url: str):
        current = url
        for _ in range(6):
            self._validate_public_https_url(current)
            try:
                response = self.session.get(
                    current,
                    headers={
                        "Accept": "application/pdf",
                        "User-Agent": self.settings.discovery_user_agent,
                    },
                    timeout=(5, self.settings.discovery_request_timeout_seconds),
                    stream=True,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise PdfDownloadError(
                    "PDF_UPSTREAM_UNAVAILABLE", "PDF host is unavailable.", 502
                ) from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise PdfDownloadError(
                        "INVALID_REDIRECT", "PDF host returned an invalid redirect.", 502
                    )
                current = urljoin(current, location)
                continue
            if response.status_code == 429:
                response.close()
                raise PdfDownloadError("PDF_RATE_LIMITED", "PDF host rate limit exceeded.", 503)
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                if status == 403:
                    raise PdfDownloadError(
                        "PDF_FORBIDDEN", "PDF host denied access to this file.", 502
                    )
                if status in {404, 410}:
                    raise PdfDownloadError(
                        "PDF_NOT_FOUND", f"PDF host returned HTTP {status}.", 502
                    )
                if status < 500:
                    raise PdfDownloadError(
                        "PDF_CLIENT_ERROR", f"PDF host returned HTTP {status}.", 502
                    )
                raise PdfDownloadError(
                    "PDF_UPSTREAM_ERROR", f"PDF host returned HTTP {status}.", 502
                )
            return response, current
        raise PdfDownloadError("TOO_MANY_REDIRECTS", "PDF URL redirected too many times.", 502)

    def _validate_public_https_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise PdfDownloadError(
                "UNSAFE_PDF_URL", "PDF URL must be an unauthenticated HTTPS URL."
            )
        try:
            literal_host = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal_host = None
        if literal_host is not None and not literal_host.is_global:
            raise PdfDownloadError(
                "UNSAFE_PDF_URL", "PDF URL must not contain a non-public IP address."
            )
        try:
            addresses = self.resolver(parsed.hostname, parsed.port or 443, 0, socket.SOCK_STREAM)
        except OSError as exc:
            raise PdfDownloadError(
                "PDF_HOST_UNRESOLVED", "PDF host could not be resolved.", 502
            ) from exc
        if not addresses:
            raise PdfDownloadError("PDF_HOST_UNRESOLVED", "PDF host could not be resolved.", 502)
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if (
                self.settings.discovery_allow_fake_ip_dns
                and literal_host is None
                and ip in FAKE_IP_NETWORK
            ):
                continue
            if not ip.is_global:
                raise PdfDownloadError(
                    "UNSAFE_PDF_URL", "PDF host must resolve only to public network addresses."
                )

    @staticmethod
    def _redact_url(url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
