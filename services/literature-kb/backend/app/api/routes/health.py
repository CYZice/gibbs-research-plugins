from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from time import perf_counter

import redis
import requests
from fastapi import APIRouter
from rq.registry import ScheduledJobRegistry
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.models.operational_control import (
    ProviderCallEvent,
    ProviderConfig,
    ProviderCredential,
)
from app.schemas.v1 import HealthResponse, SystemHealthResponse
from app.services.task_queue import QUEUE_NAMES

router = APIRouter(tags=["System"])


@router.get("/health", response_model=HealthResponse)
def health_check() -> dict[str, str]:
    return {"status": "ok"}


def _component(name: str, check) -> dict:
    started = perf_counter()
    try:
        detail = check()
        return {
            "name": name,
            "status": "healthy",
            "latency_ms": round((perf_counter() - started) * 1000),
            "detail": detail,
        }
    except Exception as exc:  # Health reporting must never take the API down.
        return {
            "name": name,
            "status": "unavailable",
            "latency_ms": round((perf_counter() - started) * 1000),
            "detail": str(exc),
        }


@router.get("/api/v1/system/health", response_model=SystemHealthResponse)
def system_health() -> dict:
    """Return dependency readiness without exposing connection strings or secrets."""
    settings = get_settings()
    def postgres_check():
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar()

    checks = [
        {"name": "api", "status": "healthy", "latency_ms": 0, "detail": "FastAPI reachable"},
    ]

    def remote(name: str, url: str, enabled: bool = True) -> dict:
        if not enabled:
            return {"name": name, "status": "not_configured", "latency_ms": 0, "detail": "disabled"}
        return _component(name, lambda: requests.get(url, timeout=2).status_code)

    jobs = {
        "postgres": lambda: _component("postgres", postgres_check),
        "redis": lambda: _component("redis", lambda: redis.Redis.from_url(settings.redis_url, socket_timeout=2).ping()),
        "mineru": lambda: remote("mineru", settings.mineru_base_url, bool(settings.mineru_api_token)),
    }
    executor = ThreadPoolExecutor(max_workers=len(jobs))
    futures = {executor.submit(job): name for name, job in jobs.items()}
    try:
        for future in as_completed(futures, timeout=2.5):
            checks.append(future.result())
    except TimeoutError:
        pass
    finally:
        pending = {name for future, name in futures.items() if not future.done()}
        checks.extend({"name": name, "status": "unavailable", "latency_ms": 2500, "detail": "health check timeout"} for name in pending)
        executor.shutdown(wait=False, cancel_futures=True)
    def model_gateway_check() -> dict:
        required_roles = {"embedding", "structured", "llm", "vision"}
        db = SessionLocal()
        try:
            configs = {
                row.role: row
                for row in db.scalars(select(ProviderConfig)).all()
            }
            required_roles.update(
                role for role, row in configs.items() if role == "rerank" and row.enabled
            )
            missing = []
            for role in sorted(required_roles):
                config = configs.get(role)
                key_count = (
                    db.scalar(
                        select(func.count())
                        .select_from(ProviderCredential)
                        .where(
                            ProviderCredential.provider_config_id == config.provider_config_id,
                            ProviderCredential.status == "active",
                        )
                    )
                    if config
                    else 0
                )
                if not config or not config.enabled or not config.base_url or not key_count:
                    missing.append(role)
            return {"configured": not missing, "missing_roles": missing}
        finally:
            db.close()

    gateway = _component("model_gateway", model_gateway_check)
    if gateway["status"] == "healthy":
        gateway["status"] = "healthy" if gateway["detail"]["configured"] else "not_configured"
        gateway["detail"] = (
            "all required roles have an enabled database configuration and active key"
            if gateway["status"] == "healthy"
            else f"database configuration required for: {', '.join(gateway['detail']['missing_roles'])}"
        )
    checks.append(gateway)

    def discovery_components() -> list[dict]:
        db = SessionLocal()
        try:
            values = []
            for provider, configured, note in (
                (
                    "openalex",
                    bool(settings.openalex_api_key or settings.discovery_contact_email),
                    "anonymous access remains available when production identity is absent",
                ),
                (
                    "unpaywall",
                    bool(settings.unpaywall_email),
                    "contact email is required for DOI enrichment",
                ),
            ):
                latest = db.scalar(
                    select(ProviderCallEvent)
                    .where(
                        ProviderCallEvent.role == "discovery",
                        ProviderCallEvent.provider == provider,
                    )
                    .order_by(ProviderCallEvent.created_at.desc())
                    .limit(1)
                )
                recent_failure = bool(
                    latest
                    and latest.status == "failure"
                    and latest.created_at >= datetime.now(UTC) - timedelta(hours=1)
                )
                component_status = (
                    "not_configured"
                    if provider == "unpaywall" and not configured
                    else "unavailable"
                    if recent_failure
                    else "healthy"
                )
                values.append(
                    {
                        "name": provider,
                        "status": component_status,
                        "latency_ms": latest.latency_ms if latest else 0,
                        "detail": {
                            "configured": configured,
                            "last_status": latest.status if latest else None,
                            "last_error_code": latest.error_code if latest else None,
                            "last_checked_at": latest.created_at if latest else None,
                            "note": note,
                        },
                    }
                )
            required_cos = (
                settings.cos_bucket,
                settings.cos_region,
                settings.cos_secret_id,
                settings.cos_secret_key,
            )
            values.append(
                {
                    "name": "discovery_storage",
                    "status": "healthy"
                    if settings.discovery_storage_provider == "local" or all(required_cos)
                    else "not_configured",
                    "latency_ms": 0,
                    "detail": {"provider": settings.discovery_storage_provider},
                }
            )
            return values
        finally:
            db.close()

    discovery = _component("discovery", discovery_components)
    if discovery["status"] == "healthy":
        checks.extend(discovery["detail"])
    else:
        checks.append(discovery)
    connection = redis.Redis.from_url(settings.redis_url)
    overdue = sum(len(ScheduledJobRegistry(name, connection=connection).get_expired_job_ids()) for name in QUEUE_NAMES)
    scheduled = sum(len(ScheduledJobRegistry(name, connection=connection).get_job_ids()) for name in QUEUE_NAMES)
    checks.append({
        "name": "rq_scheduler",
        "status": "unavailable" if overdue else "healthy",
        "latency_ms": 0,
        "detail": {"scheduled": scheduled, "overdue": overdue},
    })
    unavailable = any(item["status"] == "unavailable" for item in checks)
    degraded = unavailable or any(item["status"] == "not_configured" for item in checks)
    return {
        "status": "degraded" if degraded else "healthy",
        "checked_at": datetime.now(UTC).isoformat(),
        "components": checks,
        "capabilities": [
            "paper_catalog",
            "literature_federation",
            "remote_fetch",
            "agent_api",
            "pipeline",
        ],
    }
