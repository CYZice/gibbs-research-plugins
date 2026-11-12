from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import redis
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from rq import get_current_job
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import ModelEndpointConfig, Settings
from app.core.ids import new_id
from app.db.session import SessionLocal
from app.models.operational_control import (
    ProviderCallEvent,
    ProviderConfig,
    ProviderCredential,
    ProviderMetricHourly,
)
from app.services.trace import get_current_trace


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        role: str,
        provider: str,
        credential_id: str | None = None,
        http_status: int | None = None,
        error_code: str = "PROVIDER_UNAVAILABLE",
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.role = role
        self.provider = provider
        self.credential_id = credential_id
        self.http_status = http_status
        self.error_code = error_code
        self.retryable = retryable
        self.retry_after = retry_after


class SecretCipher:
    def __init__(self, encoded_key: str | None) -> None:
        self.key = base64.b64decode(encoded_key, validate=True) if encoded_key else None
        if self.key is not None and len(self.key) != 32:
            raise ValueError("ADMIN_SECRETS_MASTER_KEY must decode to exactly 32 bytes.")

    def encrypt(self, value: str) -> str:
        if self.key is None:
            raise ValueError("ADMIN_SECRETS_MASTER_KEY is required to store provider keys.")
        nonce = os.urandom(12)
        encrypted = AESGCM(self.key).encrypt(nonce, value.encode(), None)
        return base64.b64encode(nonce + encrypted).decode()

    def decrypt(self, value: str) -> str:
        if self.key is None:
            raise ValueError("ADMIN_SECRETS_MASTER_KEY is required to use stored provider keys.")
        raw = base64.b64decode(value)
        return AESGCM(self.key).decrypt(raw[:12], raw[12:], None).decode()


class ProviderControlService:
    """Small shared control plane for dynamic provider config, keys, and limits."""

    ACQUIRE_SCRIPT = """
    local now = tonumber(ARGV[1])
    local expires = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    local token = ARGV[4]
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
    if redis.call('ZCARD', KEYS[1]) >= limit then return 0 end
    redis.call('ZADD', KEYS[1], expires, token)
    redis.call('EXPIRE', KEYS[1], math.ceil((expires-now)/1000)+5)
    return 1
    """
    BUDGET_SCRIPT = """
    local current = tonumber(redis.call('GET', KEYS[1]) or '0')
    local amount = tonumber(ARGV[1])
    local limit = tonumber(ARGV[2])
    if current + amount > limit then return 0 end
    redis.call('INCRBY', KEYS[1], amount)
    redis.call('EXPIRE', KEYS[1], 70)
    return 1
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(settings.redis_url)
        self.cipher = SecretCipher(settings.admin_secrets_master_key)

    def resolve_config(self, role: str) -> ModelEndpointConfig:
        db = SessionLocal()
        try:
            config = db.scalar(select(ProviderConfig).where(ProviderConfig.role == role))
            if config is None:
                raise ProviderError(
                    f"Provider configuration is required for {role}.",
                    role=role,
                    provider="database",
                    error_code="PROVIDER_CONFIGURATION_REQUIRED",
                    retryable=False,
                )
            return ModelEndpointConfig(
                role=role,
                provider=config.provider,
                base_url=config.base_url,
                model=config.model,
                enabled=config.enabled,
                timeout_seconds=config.timeout_seconds,
                rpm_limit=config.rpm_limit,
                tpm_limit=config.tpm_limit,
                breaker_threshold=config.breaker_threshold,
                breaker_cooldown_seconds=config.breaker_cooldown_seconds,
            )
        except SQLAlchemyError as exc:
            db.rollback()
            raise ProviderError(
                f"Provider configuration is unavailable for {role}.",
                role=role,
                provider="database",
                error_code="PROVIDER_CONFIGURATION_UNAVAILABLE",
                retryable=True,
                retry_after=60,
            ) from exc
        finally:
            db.close()

    def resolve_endpoint(self, role: str, exclude: set[str] | None = None) -> ModelEndpointConfig:
        db = SessionLocal()
        try:
            config = db.scalar(select(ProviderConfig).where(ProviderConfig.role == role))
            if config is None:
                raise ProviderError(
                    f"Provider configuration is required for {role}.",
                    role=role,
                    provider="database",
                    error_code="PROVIDER_CONFIGURATION_REQUIRED",
                    retryable=False,
                )
            endpoint = ModelEndpointConfig(
                role=role,
                provider=config.provider,
                base_url=config.base_url,
                model=config.model,
                enabled=config.enabled,
                timeout_seconds=config.timeout_seconds,
                rpm_limit=config.rpm_limit,
                tpm_limit=config.tpm_limit,
                breaker_threshold=config.breaker_threshold,
                breaker_cooldown_seconds=config.breaker_cooldown_seconds,
            )
            credentials = db.scalars(
                select(ProviderCredential)
                .where(
                    ProviderCredential.provider_config_id == config.provider_config_id,
                    ProviderCredential.status == "active",
                )
                .order_by(
                    ProviderCredential.cooldown_until.asc().nullsfirst(),
                    ProviderCredential.last_success_at.asc().nullsfirst(),
                )
            ).all()
            now = datetime.now(UTC)
            eligible = []
            active_leases: dict[str, int] = {}
            now_ms = int(time.time() * 1000)
            for credential in credentials:
                if credential.credential_id in (exclude or set()) or (
                    credential.cooldown_until
                    and credential.cooldown_until > now
                ):
                    continue
                lease_key = f"provider:key-leases:{credential.credential_id}"
                self.redis.zremrangebyscore(lease_key, "-inf", now_ms)
                active = int(self.redis.zcard(lease_key))
                active_leases[credential.credential_id] = active
                if active < credential.max_concurrency:
                    eligible.append(credential)
            if eligible:
                credential = min(
                    eligible,
                    key=lambda item: (
                        active_leases[item.credential_id]
                        / max(1, item.max_concurrency),
                        active_leases[item.credential_id] / item.weight,
                        item.last_success_at or datetime.min.replace(tzinfo=UTC),
                    ),
                )
                endpoint.api_key = self.cipher.decrypt(credential.encrypted_api_key)
                endpoint.credential_id = credential.credential_id
                endpoint.max_concurrency = credential.max_concurrency
                return endpoint
            raise ProviderError(
                f"No healthy credential is available for {role}.",
                role=role,
                provider=endpoint.provider,
                error_code="PROVIDER_CAPACITY_WAIT",
                retry_after=60,
            )
        except SQLAlchemyError as exc:
            db.rollback()
            raise ProviderError(
                f"Provider configuration is unavailable for {role}.",
                role=role,
                provider="database",
                error_code="PROVIDER_CONFIGURATION_UNAVAILABLE",
                retryable=True,
                retry_after=60,
            ) from exc
        finally:
            db.close()

    @contextmanager
    def lease(self, endpoint: ModelEndpointConfig, estimated_tokens: int = 0):
        breaker = self.redis.get(f"provider:breaker:{endpoint.role}")
        if breaker:
            raise ProviderError(
                f"Provider circuit is open for {endpoint.role}.",
                role=endpoint.role,
                provider=endpoint.provider,
                credential_id=endpoint.credential_id,
                error_code="PROVIDER_CIRCUIT_OPEN",
                retry_after=float(self.redis.ttl(f"provider:breaker:{endpoint.role}") or 60),
            )
        token = uuid.uuid4().hex
        if not endpoint.credential_id:
            raise ProviderError(
                f"Provider credential is required for {endpoint.role}.",
                role=endpoint.role,
                provider=endpoint.provider,
                error_code="PROVIDER_CONFIGURATION_REQUIRED",
                retryable=False,
            )
        credential_key = f"provider:key-leases:{endpoint.credential_id}"
        now_ms = int(time.time() * 1000)
        expires_ms = now_ms + max(30, endpoint.timeout_seconds + 30) * 1000
        acquired = self.redis.eval(
            self.ACQUIRE_SCRIPT,
            1,
            credential_key,
            now_ms,
            expires_ms,
            endpoint.max_concurrency,
            token,
        )
        if not acquired:
            raise ProviderError(
                f"Provider concurrency is exhausted for {endpoint.role}.",
                role=endpoint.role,
                provider=endpoint.provider,
                credential_id=endpoint.credential_id,
                error_code="PROVIDER_CAPACITY_WAIT",
                retry_after=1,
            )
        minute = int(time.time() // 60)
        rpm_ok = not endpoint.rpm_limit or self.redis.eval(
            self.BUDGET_SCRIPT, 1, f"provider:rpm:{endpoint.role}:{minute}", 1, endpoint.rpm_limit
        )
        tpm_ok = not endpoint.tpm_limit or self.redis.eval(
            self.BUDGET_SCRIPT,
            1,
            f"provider:tpm:{endpoint.role}:{minute}",
            max(1, estimated_tokens),
            endpoint.tpm_limit,
        )
        if not rpm_ok or not tpm_ok:
            self.redis.zrem(credential_key, token)
            raise ProviderError(
                f"Provider minute budget is exhausted for {endpoint.role}.",
                role=endpoint.role,
                provider=endpoint.provider,
                credential_id=endpoint.credential_id,
                error_code="PROVIDER_CAPACITY_WAIT",
                retry_after=max(1, 60 - int(time.time()) % 60),
            )
        try:
            yield
        finally:
            self.redis.zrem(credential_key, token)

    @contextmanager
    def lease_endpoint(
        self,
        role: str,
        *,
        estimated_tokens: int = 0,
        preferred: ModelEndpointConfig | None = None,
        exclude: set[str] | None = None,
    ):
        """Lease one healthy credential, trying another key after a capacity race."""
        attempted = set(exclude or set())
        candidate = (
            preferred
            if preferred
            and preferred.credential_id
            and preferred.credential_id not in attempted
            else None
        )
        while True:
            if candidate is None:
                candidate = self.resolve_endpoint(role, attempted)
            acquired = False
            try:
                with self.lease(candidate, estimated_tokens):
                    acquired = True
                    yield candidate
                return
            except ProviderError as exc:
                if acquired or exc.error_code != "PROVIDER_CAPACITY_WAIT":
                    raise
                if candidate.credential_id:
                    attempted.add(candidate.credential_id)
                candidate = None

    def report_success(self, endpoint: ModelEndpointConfig) -> None:
        self.redis.delete(f"provider:failures:{endpoint.role}")
        if endpoint.credential_id:
            db = SessionLocal()
            try:
                credential = db.get(ProviderCredential, endpoint.credential_id)
                if credential:
                    credential.consecutive_failures = 0
                    credential.last_error_code = None
                    credential.last_success_at = datetime.now(UTC)
                    credential.cooldown_until = None
                    db.commit()
            finally:
                db.close()

    def report_failure(self, endpoint: ModelEndpointConfig, error: ProviderError) -> None:
        now = datetime.now(UTC)
        failures_key = f"provider:failures:{endpoint.role}"
        failures = int(self.redis.incr(failures_key))
        self.redis.expire(failures_key, 60)
        if failures >= endpoint.breaker_threshold:
            self.redis.setex(
                f"provider:breaker:{endpoint.role}", endpoint.breaker_cooldown_seconds, "1"
            )
        delay = error.retry_after or (60 if error.http_status == 429 else 0)
        db = SessionLocal()
        try:
            credential = (
                db.get(ProviderCredential, endpoint.credential_id)
                if endpoint.credential_id
                else None
            )
            if credential:
                credential.consecutive_failures += 1
                credential.last_failure_at = now
                credential.last_error_code = error.error_code
                if error.http_status == 401:
                    credential.status = "disabled"
                elif delay:
                    credential.cooldown_until = now + timedelta(seconds=delay)
                db.commit()
        finally:
            db.close()

    def record_event(
        self,
        endpoint: ModelEndpointConfig,
        operation: str,
        status: str,
        latency_ms: int,
        attempt: int,
        error: ProviderError | None = None,
    ) -> None:
        trace = get_current_trace()
        job = get_current_job()
        owner_id = str(job.args[0]) if job and job.args else None
        function_name = str(job.func_name) if job else ""
        db = SessionLocal()
        try:
            db.add(
                ProviderCallEvent(
                    event_id=new_id("pevent"),
                    role=endpoint.role,
                    provider=endpoint.provider,
                    model=endpoint.model,
                    credential_id=endpoint.credential_id,
                    operation=operation,
                    status=status,
                    error_code=error.error_code if error else None,
                    http_status=error.http_status if error else None,
                    latency_ms=latency_ms,
                    attempt=attempt,
                    request_id=trace.request_id if trace else None,
                    trace_id=trace.trace_id if trace else None,
                    task_id=owner_id if function_name.endswith("execute_task") else None,
                    run_id=owner_id if function_name.endswith("execute_workflow") else None,
                )
            )
            db.commit()
        except SQLAlchemyError:
            db.rollback()
        finally:
            db.close()

    @staticmethod
    def fingerprint(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()


def aggregate_provider_metrics() -> dict:
    settings = __import__("app.core.config", fromlist=["get_settings"]).get_settings()
    db = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=settings.provider_event_retention_days)
        bucket = func.date_trunc("hour", ProviderCallEvent.created_at).label("bucket")
        rows = db.execute(
            select(
                bucket,
                ProviderCallEvent.role,
                ProviderCallEvent.provider,
                ProviderCallEvent.model,
                func.count(),
                func.count().filter(ProviderCallEvent.status == "success"),
                func.count().filter(ProviderCallEvent.error_code == "PROVIDER_RATE_LIMITED"),
                func.count().filter(ProviderCallEvent.error_code == "PROVIDER_TIMEOUT"),
                func.count().filter(ProviderCallEvent.status == "failure"),
                func.sum(ProviderCallEvent.latency_ms),
                func.max(ProviderCallEvent.latency_ms),
            )
            .where(ProviderCallEvent.created_at >= cutoff)
            .group_by(
                bucket, ProviderCallEvent.role, ProviderCallEvent.provider, ProviderCallEvent.model
            )
        ).all()
        for row in rows:
            values = {
                "metric_id": f"metric_{hashlib.sha256(f'{row[0]}:{row[1]}:{row[2]}:{row[3]}'.encode()).hexdigest()[:24]}",
                "bucket_start": row[0],
                "role": row[1],
                "provider": row[2],
                "model": row[3],
                "total_calls": row[4],
                "success_calls": row[5],
                "rate_limited_calls": row[6],
                "timeout_calls": row[7],
                "failed_calls": row[8],
                "latency_sum_ms": row[9] or 0,
                "latency_max_ms": row[10] or 0,
            }
            existing = db.scalar(
                select(ProviderMetricHourly).where(
                    ProviderMetricHourly.bucket_start == values["bucket_start"],
                    ProviderMetricHourly.role == values["role"],
                    ProviderMetricHourly.provider == values["provider"],
                    ProviderMetricHourly.model.is_(None)
                    if values["model"] is None
                    else ProviderMetricHourly.model == values["model"],
                )
            )
            if existing:
                for key, value in values.items():
                    if key not in {"metric_id", "bucket_start", "role", "provider", "model"}:
                        setattr(existing, key, value)
            else:
                db.add(ProviderMetricHourly(**values))
        deleted = db.execute(
            delete(ProviderCallEvent).where(ProviderCallEvent.created_at < cutoff)
        ).rowcount
        db.commit()
        return {"buckets": len(rows), "deleted_events": deleted}
    finally:
        db.close()
