from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import redis
import requests
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.ids import new_id
from app.models.harvest import (
    CorpusDocument,
    HarvestCredential,
    HarvestJob,
    HarvestJobItem,
)
from app.models.import_batch import ImportBatch, ImportBatchItem
from app.models.staged_artifact import StagedArtifact
from app.schemas.discovery import DiscoveryDownloadCandidate
from app.schemas.harvest import HarvestCreateRequest, HarvestEstimateRequest
from app.services.discovery_download_storage import DiscoveryDownloadStorage
from app.services.discovery_service import DiscoveryService
from app.services.pdf_download_service import PdfDownloadError, PdfDownloadService
from app.services.pipeline_batch_service import PipelineBatchService
from app.services.provider_control_service import SecretCipher
from app.services.staged_artifact_service import StagedArtifactService

OPENALEX_API_URL = "https://api.openalex.org"
OPENALEX_CONTENT_URL = "https://content.openalex.org/works"
UNPAYWALL_API_URL = "https://api.unpaywall.org/v2"
TERMINAL_JOB_STATUSES = {
    "completed",
    "completed_with_errors",
    "cancelled",
    "failed",
}
ACTIVE_ITEM_STATUSES = {"pending", "resolving", "downloading", "waiting_quota"}


class HarvestError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class HarvestCredentialService:
    RESERVE_SCRIPT = """
    local used = tonumber(redis.call('GET', KEYS[1]) or '0')
    local amount = tonumber(ARGV[1])
    local limit = tonumber(ARGV[2])
    if used + amount > limit then return 0 end
    redis.call('INCRBY', KEYS[1], amount)
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
    return 1
    """

    def __init__(self, settings: Settings, session=None, redis_client=None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.redis = redis_client or redis.Redis.from_url(settings.redis_url)
        self.cipher = SecretCipher(settings.admin_secrets_master_key)

    def create(self, db, *, alias: str, api_key: str, weight: float = 1.0):
        fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
        existing = db.scalar(
            select(HarvestCredential).where(
                HarvestCredential.key_fingerprint == fingerprint
            )
        )
        if existing:
            raise HarvestError("HARVEST_KEY_EXISTS", "This OpenAlex API key already exists.", 409)
        row = HarvestCredential(
            credential_id=new_id("harvestkey"),
            provider="openalex",
            alias=alias.strip(),
            encrypted_api_key=self.cipher.encrypt(api_key.strip()),
            key_suffix=api_key.strip()[-8:],
            key_fingerprint=fingerprint,
            status="active",
            weight=weight,
        )
        db.add(row)
        db.flush()
        self.refresh_quota(row)
        return row

    def refresh_quota(self, row: HarvestCredential) -> dict[str, Any]:
        key = self.cipher.decrypt(row.encrypted_api_key)
        try:
            response = self.session.get(
                f"{OPENALEX_API_URL}/rate-limit",
                params={"api_key": key},
                headers={"User-Agent": self.settings.discovery_user_agent},
                timeout=self.settings.discovery_search_timeout_seconds,
            )
        except requests.RequestException as exc:
            row.last_error_code = "OPENALEX_UNAVAILABLE"
            row.last_failure_at = datetime.now(UTC)
            raise HarvestError(
                "OPENALEX_UNAVAILABLE",
                "OpenAlex quota endpoint is unavailable.",
                502,
                retryable=True,
            ) from exc
        if response.status_code in {401, 403}:
            row.status = "invalid"
            row.last_error_code = "OPENALEX_KEY_INVALID"
            row.last_failure_at = datetime.now(UTC)
            raise HarvestError("OPENALEX_KEY_INVALID", "OpenAlex API key is invalid.", 400)
        if response.status_code >= 400:
            raise HarvestError(
                "OPENALEX_QUOTA_FAILED",
                f"OpenAlex quota endpoint returned HTTP {response.status_code}.",
                502,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        payload = response.json().get("rate_limit") or {}
        costs = payload.get("endpoint_costs_usd") or {}
        row.daily_budget_usd = self._float(payload.get("daily_budget_usd"))
        row.daily_used_usd = self._float(payload.get("daily_used_usd"))
        row.daily_remaining_usd = self._float(payload.get("daily_remaining_usd"))
        row.prepaid_remaining_usd = self._float(payload.get("prepaid_remaining_usd"))
        row.content_cost_usd = self._float(costs.get("content")) or 0.01
        row.resets_at = self._datetime(payload.get("resets_at"))
        row.last_checked_at = datetime.now(UTC)
        row.last_success_at = datetime.now(UTC)
        row.last_error_code = None
        row.consecutive_failures = 0
        if row.status == "daily_exhausted" and (
            (row.daily_remaining_usd or 0) > 0
        ):
            row.status = "active"
        return payload

    def lease(self, db, operation: str) -> tuple[HarvestCredential, str, float]:
        row, api_key, cost, _ = self.lease_many(db, operation, 1)
        return row, api_key, cost

    def lease_many(
        self, db, operation: str, max_units: int
    ) -> tuple[HarvestCredential, str, float, int]:
        now = datetime.now(UTC)
        rows = db.scalars(
            select(HarvestCredential)
            .where(
                HarvestCredential.provider == "openalex",
                HarvestCredential.status.in_(("active", "daily_exhausted")),
            )
            .order_by(HarvestCredential.last_success_at.asc().nullsfirst())
            .with_for_update()
        ).all()
        if not rows and self.settings.openalex_api_key and operation != "content":
            row, api_key, cost = self._legacy_credential(operation)
            return row, api_key, cost, 1
        earliest_reset = None
        for row in rows:
            if row.status == "daily_exhausted" and row.resets_at and row.resets_at > now:
                earliest_reset = min(earliest_reset or row.resets_at, row.resets_at)
                continue
            stale = not row.last_checked_at or row.last_checked_at < now - timedelta(seconds=60)
            if operation == "content" or stale or row.status == "daily_exhausted":
                try:
                    self.refresh_quota(row)
                except HarvestError:
                    continue
            cost = (
                row.content_cost_usd or 0.01
                if operation == "content"
                else 0.0001
            )
            remaining = row.daily_remaining_usd or 0.0
            if remaining + 1e-9 < cost:
                row.status = "daily_exhausted"
                if row.resets_at:
                    earliest_reset = min(
                        earliest_reset or row.resets_at, row.resets_at
                    )
                continue
            units = min(max_units, max(1, int((remaining + 1e-9) / cost)))
            reserved_cost = cost * units
            reset_seconds = max(
                60, int(((row.resets_at or (now + timedelta(days=1))) - now).total_seconds()) + 60
            )
            reset_day = (row.resets_at or now).date().isoformat()
            reserve_key = f"harvest:quota:{row.credential_id}:{reset_day}"
            try:
                allowed = self.redis.eval(
                    self.RESERVE_SCRIPT,
                    1,
                    reserve_key,
                    max(1, round(reserved_cost * 1_000_000)),
                    max(
                        1,
                        round(
                            (row.daily_budget_usd or remaining)
                            * 1_000_000
                        ),
                    ),
                    reset_seconds,
                )
            except redis.RedisError:
                allowed = 1
            if not allowed:
                continue
            row.daily_remaining_usd = max(0.0, remaining - reserved_cost)
            return (
                row,
                self.cipher.decrypt(row.encrypted_api_key),
                cost,
                units,
            )
        raise HarvestError(
            "OPENALEX_FREE_QUOTA_EXHAUSTED",
            "All configured OpenAlex keys have exhausted their daily free allowance.",
            429,
            retryable=True,
        )

    def quota_summary(self, db) -> dict[str, Any]:
        rows = db.scalars(
            select(HarvestCredential)
            .where(HarvestCredential.provider == "openalex")
            .order_by(HarvestCredential.created_at)
        ).all()
        return {
            "daily_remaining_usd": round(
                sum(row.daily_remaining_usd or 0 for row in rows if row.status == "active"), 6
            ),
            "estimated_content_files": sum(
                int((row.daily_remaining_usd or 0) / (row.content_cost_usd or 0.01))
                for row in rows
                if row.status == "active"
            ),
            "next_reset_at": min(
                (
                    row.resets_at
                    for row in rows
                    if row.resets_at
                    and row.status in {"active", "daily_exhausted"}
                ),
                default=None,
            ),
            "items": [credential_payload(row) for row in rows],
        }

    def _legacy_credential(self, operation: str):
        class Legacy:
            credential_id = "settings_openalex"
            resets_at = None

        return Legacy(), self.settings.openalex_api_key, 0.01 if operation == "content" else 0.0001

    @staticmethod
    def _float(value) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _datetime(value) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class HarvestService:
    def __init__(self, db, settings: Settings, session=None, credential_service=None) -> None:
        self.db = db
        self.settings = settings
        self.session = session or requests.Session()
        self.credentials = credential_service or HarvestCredentialService(
            settings, session=self.session
        )

    def taxonomy_search(self, query: str, level: str | None = None) -> list[dict]:
        levels = [level] if level else ["domain", "field", "subfield", "topic"]
        items = []
        for current in levels:
            payload = self._openalex_get(
                f"/{current}s", {"search": query, "per_page": 25}, operation="list"
            )
            items.extend(self._taxonomy_item(current, row) for row in payload.get("results") or [])
        return sorted(
            items,
            key=lambda item: (
                -int(item.get("works_count") or 0),
                item["display_name"],
            ),
        )

    def taxonomy_children(self, level: str, openalex_id: str) -> list[dict]:
        child = {"domain": "field", "field": "subfield", "subfield": "topic"}.get(level)
        if not child:
            return []
        payload = self._openalex_get(
            f"/{child}s",
            {
                "filter": f"{level}.id:{self._short_id(openalex_id)}",
                "per_page": 100,
                "sort": "-works_count",
            },
            operation="list",
        )
        return [self._taxonomy_item(child, row) for row in payload.get("results") or []]

    def estimate(self, body: HarvestEstimateRequest) -> dict:
        payload = self._openalex_get(
            "/works",
            {
                "filter": self._works_filter(
                    body.scope.model_dump(), body.filters.model_dump()
                ),
                "per_page": 1,
            },
            operation="list",
        )
        total = int((payload.get("meta") or {}).get("count") or 0)
        return {
            "estimated_items": total,
            "scope": body.scope.model_dump(),
            "filters": body.filters.model_dump(),
            "license_filter_applied": False,
        }

    def create_job(self, body: HarvestCreateRequest) -> HarvestJob:
        estimate = self.estimate(body)
        snapshot = {
            "selected": body.scope.model_dump(),
            "children": self.taxonomy_children(
                body.scope.level, body.scope.openalex_id
            )
            if body.scope.include_descendants
            else [],
        }
        job = HarvestJob(
            job_id=new_id("harvest"),
            name=body.name,
            provider="openalex",
            status="enumerating",
            scope=body.scope.model_dump(),
            filters=body.filters.model_dump(),
            taxonomy_snapshot=snapshot,
            billing_mode=body.billing_mode,
            ingest_mode=body.ingest_mode,
            next_cursor="*",
            estimated_items=estimate["estimated_items"],
            details={"license_filter_applied": False},
        )
        self.db.add(job)
        self.db.flush()
        return job

    def expand_one_page(self, job: HarvestJob) -> int:
        if job.status in TERMINAL_JOB_STATUSES | {"paused"} or job.enumeration_complete:
            return 0
        pending = self.db.scalar(
            select(func.count()).select_from(HarvestJobItem).where(
                HarvestJobItem.job_id == job.job_id,
                HarvestJobItem.status.in_(tuple(ACTIVE_ITEM_STATUSES)),
            )
        ) or 0
        if pending >= self.settings.harvest_expand_buffer_items:
            return 0
        payload = self._openalex_get(
            "/works",
            {
                "filter": self._works_filter(job.scope, job.filters),
                "per_page": 100,
                "cursor": job.next_cursor or "*",
                "select": (
                    "id,doi,title,publication_year,publication_date,type,language,"
                    "authorships,primary_topic,topics,locations,best_oa_location,"
                    "primary_location,content_urls,has_content,open_access,ids,"
                    "abstract_inverted_index,"
                    "cited_by_count"
                ),
            },
            operation="list",
        )
        inserted = 0
        for work in payload.get("results") or []:
            document = self._upsert_document(work)
            existing_item = self.db.scalar(
                select(HarvestJobItem).where(
                    HarvestJobItem.job_id == job.job_id,
                    HarvestJobItem.document_id == document.document_id,
                )
            )
            if existing_item:
                continue
            item = HarvestJobItem(
                item_id=new_id("harvestitem"),
                job_id=job.job_id,
                document_id=document.document_id,
                status=(
                    "staged"
                    if document.pdf_storage_key
                    else "metadata_ready"
                    if job.ingest_mode == "metadata_only"
                    else "pending"
                ),
                details={"source_availability": self._source_availability(document)},
            )
            self.db.add(item)
            self.db.flush()
            inserted += 1
            if item.status == "staged":
                job.staged_items += 1
        meta = payload.get("meta") or {}
        job.enumerated_items = self.db.scalar(
            select(func.count()).select_from(HarvestJobItem).where(
                HarvestJobItem.job_id == job.job_id
            )
        ) or job.enumerated_items
        job.next_cursor = meta.get("next_cursor")
        if not job.next_cursor or not payload.get("results"):
            job.enumeration_complete = True
            job.status = "downloading"
        self.reconcile_job(job)
        return inserted

    def download_item(self, item: HarvestJobItem) -> None:
        job = self.db.get(HarvestJob, item.job_id)
        document = self.db.get(CorpusDocument, item.document_id)
        if not job or not document or job.status in TERMINAL_JOB_STATUSES | {"paused"}:
            return
        if document.pdf_storage_key:
            item.status = "staged"
            self.reconcile_job(job)
            return
        item.status = "downloading"
        item.attempts += 1
        self.db.commit()
        openalex_candidates = [
            DiscoveryDownloadCandidate.model_validate(row)
            for row in document.candidate_urls or []
        ]
        errors = []
        seen_urls = set()
        downloader = PdfDownloadService(self.settings, session=self.session)
        for phase in ("openalex", "unpaywall", "secondary"):
            if phase == "openalex":
                candidates = openalex_candidates
            elif phase == "unpaywall":
                candidates = self._unpaywall_candidates(document.doi)
            else:
                candidates = self._secondary_candidates(document)
            for candidate in candidates:
                clean_url = candidate.url.split("?", 1)[0]
                if clean_url in seen_urls:
                    continue
                seen_urls.add(clean_url)
                try:
                    result = downloader.download(
                        document.external_id,
                        candidate,
                        doi=document.doi,
                        title=document.title,
                        allow_unknown_license=True,
                        storage_key=(
                            f"corpus/openalex/{document.external_id}/paper.pdf"
                        ),
                        metadata_key=(
                            f"corpus/openalex/{document.external_id}/metadata.json"
                        ),
                        metadata_payload=document.metadata_payload,
                    )
                    self._mark_staged(item, document, result, candidate.provider)
                    return
                except PdfDownloadError as exc:
                    error = {
                        "source": candidate.provider,
                        "code": exc.code,
                        "retryable": exc.retryable,
                    }
                    if exc.cause_type:
                        error["cause_type"] = exc.cause_type
                    if exc.cause_errno is not None:
                        error["cause_errno"] = exc.cause_errno
                    errors.append(error)
        retryable = item.attempts < self.settings.harvest_max_attempts and any(
            row["retryable"] for row in errors
        )
        item.status = "pending" if retryable else "no_source"
        item.error_code = errors[-1]["code"] if errors else "NO_DOWNLOAD_SOURCE"
        item.error_message = "No candidate returned a valid PDF."
        item.details = {
            **dict(item.details or {}),
            "source_attempts": errors,
            "source_availability": self._source_availability(document),
            "content_eligible": bool(document.content_url),
            "content_status": (
                "not_requested" if document.content_url else "not_eligible"
            ),
            "content_reason": (
                "free_sources_exhausted" if document.content_url else "content_unavailable"
            ),
        }
        item.next_attempt_at = datetime.now(UTC) + timedelta(minutes=10) if retryable else None
        self.reconcile_job(job)
        self.db.commit()

    def estimate_content_downloads(
        self,
        job: HarvestJob,
        *,
        selection: str,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        eligible = self.db.scalars(
            self._content_items_statement(
                job, selection=selection, item_ids=item_ids
            )
        ).all()
        quota = self.credentials.quota_summary(self.db)
        credential_costs = [
            item.get("content_cost_usd")
            for item in quota["items"]
            if item.get("status") == "active" and item.get("content_cost_usd")
        ]
        unit_cost = min(credential_costs) if credential_costs else 0.01
        requested = len(item_ids or []) if selection == "items" else len(eligible)
        return {
            "selection": selection,
            "requested_items": requested,
            "eligible_items": len(eligible),
            "excluded_items": max(0, requested - len(eligible)),
            "unit_cost_usd": unit_cost,
            "estimated_cost_usd": round(len(eligible) * unit_cost, 6),
            "daily_remaining_usd": quota["daily_remaining_usd"],
            "downloadable_now": quota["estimated_content_files"],
            "would_wait_for_quota": max(
                0, len(eligible) - quota["estimated_content_files"]
            ),
            "content_enabled": self.settings.harvest_allow_openalex_content,
            "credential_configured": bool(quota["items"]),
        }

    def request_content_downloads(
        self,
        job: HarvestJob,
        *,
        selection: str,
        item_ids: list[str] | None = None,
    ) -> list[str]:
        if not self.settings.harvest_allow_openalex_content:
            raise HarvestError(
                "OPENALEX_CONTENT_DISABLED",
                "OpenAlex Content downloads are disabled by configuration.",
                403,
            )
        if job.status in {"cancelled", "failed"}:
            raise HarvestError(
                "HARVEST_JOB_NOT_CONTENT_ELIGIBLE",
                "This Harvest job cannot start Content downloads.",
                409,
            )
        quota = self.credentials.quota_summary(self.db)
        if not quota["items"]:
            raise HarvestError(
                "OPENALEX_CONTENT_CREDENTIAL_REQUIRED",
                "A managed OpenAlex Content credential is required before download.",
                409,
            )
        rows = self.db.scalars(
            self._content_items_statement(
                job, selection=selection, item_ids=item_ids
            ).with_for_update(skip_locked=True)
        ).all()
        if not rows:
            raise HarvestError(
                "NO_CONTENT_ELIGIBLE_ITEMS",
                "No selected no_source items have an OpenAlex Content PDF.",
                409,
            )
        requested_at = datetime.now(UTC).isoformat()
        for item in rows:
            item.status = "resolving"
            item.error_code = None
            item.error_message = None
            item.next_attempt_at = None
            item.details = {
                **dict(item.details or {}),
                "phase": "content",
                "content_eligible": True,
                "content_status": "queued",
                "content_reason": "explicit_request",
                "content_requested_at": requested_at,
            }
        job.status = "downloading"
        job.next_retry_at = None
        self.db.commit()
        return [item.item_id for item in rows]

    def estimate_free_downloads(
        self,
        job: HarvestJob,
        *,
        selection: str,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        rows = self.db.scalars(
            self._free_download_items_statement(
                job, selection=selection, item_ids=item_ids
            )
        ).all()
        requested = len(item_ids or []) if selection == "items" else len(rows)
        return {
            "selection": selection,
            "requested_items": requested,
            "eligible_items": len(rows),
            "excluded_items": max(0, requested - len(rows)),
            "uses_openalex_content": False,
            "uses_openalex_content_credit": False,
            "source_classification": "free_candidate",
        }

    def request_free_downloads(
        self,
        job: HarvestJob,
        *,
        selection: str,
        item_ids: list[str] | None = None,
    ) -> int:
        if job.status in {"cancelled", "failed"}:
            raise HarvestError(
                "HARVEST_JOB_NOT_DOWNLOAD_ELIGIBLE",
                "This Harvest job cannot start free downloads.",
                409,
            )
        rows = self.db.scalars(
            self._free_download_items_statement(
                job, selection=selection, item_ids=item_ids
            ).with_for_update(skip_locked=True)
        ).all()
        if not rows:
            raise HarvestError(
                "NO_FREE_DOWNLOAD_ELIGIBLE_ITEMS",
                "No selected metadata records have a free PDF candidate.",
                409,
            )
        requested_at = datetime.now(UTC).isoformat()
        for item in rows:
            item.status = "pending"
            item.error_code = None
            item.error_message = None
            item.next_attempt_at = None
            item.details = {
                **dict(item.details or {}),
                "free_download_status": "queued",
                "free_download_requested_at": requested_at,
            }
        job.status = "downloading"
        job.next_retry_at = None
        self.db.commit()
        return len(rows)

    def _content_items_statement(
        self,
        job: HarvestJob,
        *,
        selection: str,
        item_ids: list[str] | None = None,
    ):
        statement = (
            select(HarvestJobItem)
            .join(CorpusDocument, CorpusDocument.document_id == HarvestJobItem.document_id)
            .where(
                HarvestJobItem.job_id == job.job_id,
                HarvestJobItem.status == "no_source",
                CorpusDocument.content_url.is_not(None),
                CorpusDocument.pdf_storage_key.is_(None),
            )
            .order_by(HarvestJobItem.created_at)
        )
        if selection == "items":
            statement = statement.where(HarvestJobItem.item_id.in_(item_ids or []))
        return statement

    def _free_download_items_statement(
        self,
        job: HarvestJob,
        *,
        selection: str,
        item_ids: list[str] | None = None,
    ):
        statement = (
            select(HarvestJobItem)
            .join(CorpusDocument, CorpusDocument.document_id == HarvestJobItem.document_id)
            .where(
                HarvestJobItem.job_id == job.job_id,
                HarvestJobItem.status == "metadata_ready",
                HarvestJobItem.details["source_availability"]["classification"].astext
                == "free_candidate",
                CorpusDocument.pdf_storage_key.is_(None),
            )
            .order_by(HarvestJobItem.created_at)
        )
        if selection == "items":
            statement = statement.where(HarvestJobItem.item_id.in_(item_ids or []))
        return statement

    def download_content_batch(self, items: list[HarvestJobItem]) -> int:
        if not items:
            return 0
        credential, api_key, _, units = self.credentials.lease_many(
            self.db, "content", len(items)
        )
        selected = items[:units]
        for item in items[units:]:
            item.status = "resolving"
        documents = [
            self.db.get(CorpusDocument, item.document_id) for item in selected
        ]
        valid_documents = [document for document in documents if document]
        results = self._download_openalex_content_batch(valid_documents, api_key)
        processed = 0
        for item, document in zip(selected, documents, strict=True):
            job = self.db.get(HarvestJob, item.job_id)
            if not document:
                item.status = "failed"
                item.error_code = "CORPUS_DOCUMENT_NOT_FOUND"
                self.reconcile_job(job)
                continue
            item.credential_id = credential.credential_id
            result = results.get(document.external_id)
            if result:
                self._mark_staged(item, document, result, "openalex_content")
            else:
                item.status = "no_source"
                item.error_code = "OPENALEX_CONTENT_UNAVAILABLE"
                item.error_message = "OpenAlex Content API did not return a valid PDF."
                item.details = {
                    **dict(item.details or {}),
                    "content_eligible": True,
                    "content_status": "unavailable",
                    "content_reason": "content_download_failed",
                }
                self.reconcile_job(job)
            processed += 1
        self.db.commit()
        return processed

    def import_staged(
        self, job: HarvestJob, item_ids: list[str] | None = None
    ) -> ImportBatch:
        statement = (
            select(HarvestJobItem)
            .where(
                HarvestJobItem.job_id == job.job_id,
                HarvestJobItem.status == "staged",
            )
            .order_by(HarvestJobItem.created_at)
            .with_for_update(skip_locked=True)
        )
        if item_ids:
            statement = statement.where(HarvestJobItem.item_id.in_(item_ids))
        items = self.db.scalars(statement).all()
        if not items:
            raise HarvestError("NO_STAGED_ITEMS", "No staged documents were selected.", 409)
        pipeline = PipelineBatchService(self.db)
        batch = pipeline.create_staged_batch(source="harvest", priority=50)
        batch.details = {
            **dict(batch.details or {}),
            "harvest_job_id": job.job_id,
        }
        self.db.flush()
        entries = []
        for item in items:
            document = self.db.get(CorpusDocument, item.document_id)
            artifact = (
                self.db.get(StagedArtifact, document.staged_artifact_id)
                if document and document.staged_artifact_id
                else None
            )
            if not document or not artifact:
                raise HarvestError(
                    "STAGED_ARTIFACT_NOT_FOUND",
                    "The staged Harvest PDF is missing its artifact record.",
                    409,
                )
            entries.append(
                {
                    "artifact": artifact,
                    "source_key": f"harvest:{document.external_id}",
                    "metadata_payload": document.metadata_payload,
                }
            )
        pipeline.append_staged_artifacts(batch, entries)
        batch.intake_closed = True
        batch.expansion_status = "completed"
        self.db.flush()
        for item in items:
            document = self.db.get(CorpusDocument, item.document_id)
            batch_item = self.db.scalar(
                select(ImportBatchItem).where(
                    ImportBatchItem.batch_id == batch.batch_id,
                    ImportBatchItem.source_key == f"harvest:{document.external_id}",
                )
            )
            if not batch_item:
                raise HarvestError(
                    "IMPORT_BATCH_ITEM_NOT_CREATED",
                    "The Harvest import item could not be created.",
                    500,
                    retryable=True,
                )
            item.import_batch_id = batch.batch_id
            item.import_batch_item_id = batch_item.item_id
            item.status = "registering"
        self.db.commit()
        return batch

    def reconcile_job(self, job: HarvestJob | None) -> None:
        if not job:
            return
        counts = dict(
            self.db.execute(
                select(HarvestJobItem.status, func.count())
                .where(HarvestJobItem.job_id == job.job_id)
                .group_by(HarvestJobItem.status)
            ).all()
        )
        job.enumerated_items = sum(int(value) for value in counts.values())
        job.staged_items = sum(
            int(counts.get(status, 0))
            for status in ("staged", "registering", "registered")
        )
        job.no_source_items = int(counts.get("no_source", 0))
        job.failed_items = int(counts.get("failed", 0))
        job.registered_items = int(counts.get("registered", 0))
        active = sum(int(counts.get(status, 0)) for status in ACTIVE_ITEM_STATUSES)
        if job.enumeration_complete and active == 0:
            job.status = (
                "completed_with_errors"
                if job.no_source_items or job.failed_items
                else "completed"
            )
        elif job.status not in {"paused", "waiting_quota", "cancelled", "failed"}:
            job.status = "downloading" if job.enumeration_complete else "enumerating"


    def _openalex_get(self, path: str, params: dict, *, operation: str) -> dict:
        credential, api_key, _ = self.credentials.lease(self.db, operation)
        try:
            response = self.session.get(
                f"{OPENALEX_API_URL}{path}",
                params={**params, "api_key": api_key},
                headers={"User-Agent": self.settings.discovery_user_agent},
                timeout=self.settings.discovery_search_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise HarvestError(
                "OPENALEX_UNAVAILABLE",
                "OpenAlex API is unavailable.",
                502,
                retryable=True,
            ) from exc
        if response.status_code == 429:
            if hasattr(credential, "status"):
                credential.status = "daily_exhausted"
            raise HarvestError(
                "OPENALEX_FREE_QUOTA_EXHAUSTED",
                "OpenAlex free allowance is exhausted.",
                429,
                retryable=True,
            )
        if response.status_code >= 400:
            raise HarvestError(
                "OPENALEX_API_ERROR",
                f"OpenAlex API returned HTTP {response.status_code}.",
                502,
                retryable=response.status_code >= 500,
            )
        return response.json()

    def _upsert_document(self, work: dict) -> CorpusDocument:
        external_id = self._short_id(work.get("id"))
        document = self.db.scalar(
            select(CorpusDocument).where(CorpusDocument.external_id == external_id)
        )
        candidates = self._openalex_candidates(work)
        if document:
            document.metadata_payload = work
            document.candidate_urls = [item.model_dump() for item in candidates]
            document.content_url = self._content_pdf_url(work)
            return document
        document = CorpusDocument(
            document_id=new_id("corpus"),
            provider="openalex",
            external_id=external_id,
            doi=self._normalize_doi(work.get("doi")),
            title=work.get("title"),
            year=work.get("publication_year"),
            publication_type=work.get("type"),
            language=work.get("language"),
            metadata_payload=work,
            candidate_urls=[item.model_dump() for item in candidates],
            content_url=self._content_pdf_url(work),
        )
        self.db.add(document)
        self.db.flush()
        return document

    def _download_openalex_content(self, document: CorpusDocument, api_key: str):
        results = self._download_openalex_content_batch([document], api_key)
        result = results.get(document.external_id)
        if not result:
            raise PdfDownloadError(
                "PDF_UPSTREAM_ERROR",
                "OpenAlex Content API did not return a valid PDF.",
                502,
            )
        return result

    def _download_openalex_content_batch(
        self, documents: list[CorpusDocument], api_key: str
    ) -> dict[str, Any]:
        results = {}
        binary = shutil.which("openalex") if self.settings.harvest_cli_enabled else None
        if binary and documents:
            with tempfile.TemporaryDirectory(prefix="openalex-harvest-") as directory:
                result = subprocess.run(
                    [
                        binary,
                        "download",
                        "--output",
                        directory,
                        "--stdin",
                        "--content",
                        "pdf",
                        "--nested",
                        "--resume",
                        "--quiet",
                        "--workers",
                        str(self.settings.harvest_cli_workers),
                    ],
                    input="".join(
                        f"{document.external_id}\n" for document in documents
                    ),
                    text=True,
                    capture_output=True,
                    env={
                        **__import__("os").environ,
                        "OPENALEX_API_KEY": api_key,
                    },
                    timeout=max(60, self.settings.discovery_request_timeout_seconds * 4),
                )
                if result.returncode == 0:
                    for document in documents:
                        pdf = next(
                            Path(directory).rglob(
                                f"{document.external_id}.pdf"
                            ),
                            None,
                        )
                        if pdf:
                            try:
                                results[document.external_id] = (
                                    self._store_cli_pdf(document, pdf)
                                )
                            except PdfDownloadError:
                                pass
        for document in documents:
            if document.external_id in results:
                continue
            candidate = DiscoveryDownloadCandidate(
                provider="openalex_content",
                url=(
                    f"{OPENALEX_CONTENT_URL}/{document.external_id}.pdf"
                    f"?api_key={api_key}"
                ),
                access="unknown",
                is_oa=False,
            )
            try:
                downloaded = PdfDownloadService(
                    self.settings
                ).download(
                    document.external_id,
                    candidate,
                    doi=document.doi,
                    title=document.title,
                    allow_unknown_license=True,
                    storage_key=(
                        f"corpus/openalex/{document.external_id}/paper.pdf"
                    ),
                    metadata_key=(
                        f"corpus/openalex/{document.external_id}/metadata.json"
                    ),
                    metadata_payload=document.metadata_payload,
                )
                results[document.external_id] = downloaded
            except PdfDownloadError:
                pass
        return results

    def _store_cli_pdf(self, document: CorpusDocument, path: Path):
        class CliResult:
            pass

        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            if not stream.read(5).startswith(b"%PDF-"):
                raise PdfDownloadError("INVALID_PDF", "CLI output is not a PDF.", 502)
            stream.seek(0)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        storage_key = f"corpus/openalex/{document.external_id}/paper.pdf"
        metadata_key = f"corpus/openalex/{document.external_id}/metadata.json"
        DiscoveryDownloadStorage(self.settings).store(
            path,
            storage_key,
            metadata_key,
            {
                "external_id": document.external_id,
                "doi": document.doi,
                "title": document.title,
                "source_provider": "openalex_content",
                "source_url": f"{OPENALEX_CONTENT_URL}/{document.external_id}.pdf",
                "sha256": digest.hexdigest(),
                "size_bytes": size,
                "openalex": document.metadata_payload,
            },
        )
        result = CliResult()
        result.storage_key = storage_key
        result.metadata_key = metadata_key
        result.sha256 = digest.hexdigest()
        result.size_bytes = size
        result.source_url = f"{OPENALEX_CONTENT_URL}/{document.external_id}.pdf"
        return result

    def _mark_staged(self, item, document, result, source: str) -> None:
        artifact = StagedArtifactService(self.db, self.settings).from_download(
            result,
            source_type="openalex_harvest",
            source_external_id=document.external_id,
            metadata_payload=document.metadata_payload,
        )
        document.pdf_storage_key = result.storage_key
        document.metadata_storage_key = result.metadata_key
        document.pdf_sha256 = result.sha256
        document.pdf_size_bytes = result.size_bytes
        document.download_source = source
        document.source_url = result.source_url
        document.staged_at = datetime.now(UTC)
        document.staged_artifact_id = artifact.artifact_id
        item.status = "staged"
        item.selected_source = source
        item.error_code = None
        item.error_message = None
        if source == "openalex_content":
            details = dict(item.details or {})
            details.pop("phase", None)
            item.details = {
                **details,
                "content_eligible": True,
                "content_status": "staged",
                "content_reason": "content_downloaded",
            }
        self.reconcile_job(self.db.get(HarvestJob, item.job_id))
        self.db.commit()

    def _unpaywall_candidates(self, doi: str | None):
        if not doi or not self.settings.unpaywall_email:
            return []
        try:
            response = self.session.get(
                f"{UNPAYWALL_API_URL}/{doi}",
                params={"email": self.settings.unpaywall_email},
                headers={"User-Agent": self.settings.discovery_user_agent},
                timeout=self.settings.discovery_search_timeout_seconds,
            )
            if response.status_code != 200:
                return []
            return DiscoveryService(self.settings)._unpaywall_candidates(response.json())
        except (requests.RequestException, ValueError):
            return []

    def _secondary_candidates(self, document: CorpusDocument):
        discovery = DiscoveryService(self.settings, session=self.session)
        candidates = []
        ids = (document.metadata_payload or {}).get("ids") or {}
        arxiv_id = ids.get("arxiv")
        if not arxiv_id and (document.doi or "").startswith("10.48550/arxiv."):
            arxiv_id = document.doi.removeprefix("10.48550/arxiv.")
        if arxiv_id:
            try:
                paper = discovery.provider("arxiv").resolve(external_id=arxiv_id)
                candidates.extend(discovery.provider("arxiv").download_candidates(paper))
            except Exception:
                pass
        if document.doi and self.settings.core_api_key:
            try:
                paper = discovery.provider("core").resolve(doi=document.doi)
                candidates.extend(discovery.provider("core").download_candidates(paper))
            except Exception:
                pass
        return candidates

    @staticmethod
    def _openalex_candidates(work: dict):
        locations = []
        for value in (
            work.get("best_oa_location"),
            work.get("primary_location"),
            *(work.get("locations") or []),
        ):
            if value and value.get("pdf_url"):
                source = value.get("source") or {}
                locations.append(
                    DiscoveryDownloadCandidate(
                        provider="openalex",
                        url=value["pdf_url"],
                        landing_page_url=value.get("landing_page_url"),
                        version=value.get("version"),
                        license=value.get("license"),
                        access="unknown",
                        is_oa=bool(value.get("is_oa")),
                        host_type=value.get("type"),
                        source_name=source.get("display_name"),
                    )
                )
        return HarvestService._deduplicate_candidates(locations)

    @staticmethod
    def _source_availability(document: CorpusDocument) -> dict[str, Any]:
        candidates = document.candidate_urls or []
        direct_count = sum(1 for item in candidates if item.get("url"))
        oa_count = sum(1 for item in candidates if item.get("url") and item.get("is_oa"))
        ids = (document.metadata_payload or {}).get("ids") or {}
        arxiv_id = ids.get("arxiv")
        if not arxiv_id and (document.doi or "").startswith("10.48550/arxiv."):
            arxiv_id = document.doi.removeprefix("10.48550/arxiv.")
        if oa_count or arxiv_id:
            classification = "free_candidate"
        elif direct_count:
            classification = "direct_candidate_unknown"
        elif document.content_url:
            classification = "content_only"
        else:
            classification = "no_pdf_candidate"
        return {
            "classification": classification,
            "openalex_pdf_url_count": direct_count,
            "openalex_oa_pdf_url_count": oa_count,
            "arxiv_id": arxiv_id,
            "arxiv_candidate": bool(arxiv_id),
            "content_url_available": bool(document.content_url),
            "metadata_only": True,
            "download_verified": False,
        }

    @staticmethod
    def _deduplicate_candidates(candidates):
        seen = set()
        result = []
        for item in candidates:
            clean = item.url.split("?", 1)[0]
            if clean not in seen:
                seen.add(clean)
                result.append(item)
        return result

    @staticmethod
    def _works_filter(scope: dict, filters: dict) -> str:
        level = scope["level"]
        clauses = [f"topics.{level}.id:{HarvestService._short_id(scope['openalex_id'])}"]
        if level == "topic":
            clauses[0] = f"topics.id:{HarvestService._short_id(scope['openalex_id'])}"
        if filters.get("year_from"):
            clauses.append(f"from_publication_date:{int(filters['year_from'])}-01-01")
        if filters.get("year_to"):
            clauses.append(f"to_publication_date:{int(filters['year_to'])}-12-31")
        if filters.get("types"):
            clauses.append("type:" + "|".join(filters["types"]))
        if filters.get("languages"):
            clauses.append("language:" + "|".join(filters["languages"]))
        return ",".join(clauses)

    @staticmethod
    def _taxonomy_item(level: str, row: dict) -> dict:
        return {
            "level": level,
            "id": HarvestService._short_id(row.get("id")),
            "display_name": row.get("display_name") or "",
            "description": row.get("description"),
            "works_count": int(row.get("works_count") or 0),
            "domain": row.get("domain"),
            "field": row.get("field"),
            "subfield": row.get("subfield"),
        }

    @staticmethod
    def _short_id(value: str | None) -> str:
        return str(value or "").rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _normalize_doi(value: str | None) -> str | None:
        if not value:
            return None
        return str(value).lower().removeprefix("https://doi.org/").removeprefix("doi:")

    @staticmethod
    def _content_pdf_url(work: dict) -> str | None:
        return (
            ((work.get("content_urls") or {}).get("pdf"))
            or work.get("content_url")
        )

    @staticmethod
    def _ingestion_metadata(work: dict) -> dict:
        return {
            "title": work.get("title"),
            "doi": HarvestService._normalize_doi(work.get("doi")),
            "openalex_id": HarvestService._short_id(work.get("id")),
            "year": work.get("publication_year"),
            "publication_date": work.get("publication_date"),
            "publication_type": work.get("type"),
            "language": work.get("language"),
            "authors": [
                {
                    "name": ((row.get("author") or {}).get("display_name")),
                    "orcid": ((row.get("author") or {}).get("orcid")),
                }
                for row in work.get("authorships") or []
            ],
            "license": ((work.get("best_oa_location") or {}).get("license")),
            "open_access": ((work.get("open_access") or {}).get("is_oa")),
            "source_url": ((work.get("primary_location") or {}).get("landing_page_url")),
        }

    def _reconcile_import_batch(self, batch_id: str | None):
        if not batch_id:
            return
        batch = self.db.get(ImportBatch, batch_id)
        if not batch:
            return
        completed, failed = self.db.execute(
            select(
                func.count().filter(ImportBatchItem.status == "succeeded"),
                func.count().filter(ImportBatchItem.status == "failed"),
            ).where(ImportBatchItem.batch_id == batch_id)
        ).one()
        batch.completed_items = int(completed or 0)
        batch.failed_items = int(failed or 0)
        if batch.completed_items + batch.failed_items >= batch.total_items:
            batch.status = "completed_with_errors" if batch.failed_items else "completed"


def credential_payload(row: HarvestCredential) -> dict:
    return {
        "credential_id": row.credential_id,
        "provider": row.provider,
        "alias": row.alias,
        "key_suffix": row.key_suffix,
        "status": row.status,
        "weight": row.weight,
        "daily_budget_usd": row.daily_budget_usd,
        "daily_used_usd": row.daily_used_usd,
        "daily_remaining_usd": row.daily_remaining_usd,
        "prepaid_remaining_usd": row.prepaid_remaining_usd,
        "content_cost_usd": row.content_cost_usd,
        "resets_at": row.resets_at,
        "last_checked_at": row.last_checked_at,
        "last_error_code": row.last_error_code,
        "last_success_at": row.last_success_at,
        "last_failure_at": row.last_failure_at,
        "created_at": row.created_at,
    }


def job_payload(job: HarvestJob) -> dict:
    return {
        "job_id": job.job_id,
        "name": job.name,
        "status": job.status,
        "scope": job.scope,
        "filters": job.filters,
        "billing_mode": job.billing_mode,
        "ingest_mode": job.ingest_mode,
        "estimated_items": job.estimated_items,
        "enumerated_items": job.enumerated_items,
        "staged_items": job.staged_items,
        "no_source_items": job.no_source_items,
        "failed_items": job.failed_items,
        "registered_items": job.registered_items,
        "enumeration_complete": job.enumeration_complete,
        "next_retry_at": job.next_retry_at,
        "next_poll_after_seconds": 30 if job.status == "waiting_quota" else 2,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
