from __future__ import annotations

from datetime import UTC, datetime, timedelta
import redis
from rq import Queue, Worker
from rq.registry import ScheduledJobRegistry
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.ids import new_id
from app.db.session import SessionLocal
from app.models.operational_control import OperationalAlert, OperationalMetricHourly, ProviderConfig, ProviderCredential
from app.models.paper_task import PaperTask
from app.services.task_queue_constants import QUEUE_NAMES


def record_api_request(path: str, status_code: int, latency_ms: int) -> None:
    if not path.startswith("/api/v1/") or path.startswith("/api/v1/admin/") or status_code == 401:
        return
    bucket = datetime.now(UTC).strftime("%Y%m%d%H")
    try:
        client = redis.Redis.from_url(get_settings().redis_url)
        key = f"operations:api:{bucket}"
        client.hincrby(key, "total", 1)
        client.hincrby(key, "good" if status_code < 500 else "failed", 1)
        client.hincrby(key, "latency_sum_ms", latency_ms)
        client.hset(key, "latency_max_ms", max(latency_ms, int(client.hget(key, "latency_max_ms") or 0)))
        client.expire(key, 7200)
    except redis.RedisError:
        pass  # Metrics must never affect the request path.


def flush_api_metrics() -> None:
    client = redis.Redis.from_url(get_settings().redis_url)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    key = f"operations:api:{now.strftime('%Y%m%d%H')}"
    values = {name.decode(): int(value) for name, value in client.hgetall(key).items()}
    if not values:
        return
    db = SessionLocal()
    try:
        row = db.scalar(select(OperationalMetricHourly).where(OperationalMetricHourly.bucket_start == now, OperationalMetricHourly.metric == "read_api"))
        if row is None:
            row = OperationalMetricHourly(metric_id=new_id("metric"), bucket_start=now, metric="read_api")
            db.add(row)
        # The hash is cumulative. Store its latest value, not an incremental sum.
        row.total = values.get("total", 0)
        row.good = values.get("good", 0)
        row.failed = values.get("failed", 0)
        row.latency_sum_ms = values.get("latency_sum_ms", 0)
        row.latency_max_ms = values.get("latency_max_ms", 0)
        db.commit()
    finally:
        db.close()


def slo_summary(hours: int) -> dict:
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        start = now - timedelta(hours=hours)
        metrics = db.execute(select(func.sum(OperationalMetricHourly.total), func.sum(OperationalMetricHourly.good), func.sum(OperationalMetricHourly.failed), func.sum(OperationalMetricHourly.latency_sum_ms), func.max(OperationalMetricHourly.latency_max_ms)).where(OperationalMetricHourly.metric == "read_api", OperationalMetricHourly.bucket_start >= start)).one()
        total, good, failed = (int(metrics[0] or 0), int(metrics[1] or 0), int(metrics[2] or 0))
        api_value = round(100 * good / total, 3) if total else None
        roots = db.scalars(select(PaperTask).where(PaperTask.operation == "process", PaperTask.root_task_id == PaperTask.task_id, PaperTask.created_at >= start)).all()
        success = external_failures = platform_failures = completed = 0
        durations: list[float] = []
        for root in roots:
            chain = db.scalars(select(PaperTask).where(PaperTask.root_task_id == root.task_id).order_by(PaperTask.created_at)).all()
            winner = next((item for item in chain if item.status == "succeeded" and item.finished_at and item.finished_at <= root.created_at + timedelta(hours=24)), None)
            terminal = any(item.status in {"failed", "cancelled", "succeeded"} for item in chain) or root.created_at <= now - timedelta(hours=24)
            if winner:
                success += 1
                completed += 1
                durations.append((winner.finished_at - root.created_at).total_seconds())
            elif terminal:
                completed += 1
                last = chain[-1] if chain else root
                if last.error_domain == "external":
                    external_failures += 1
                else:
                    platform_failures += 1
        task_value = round(100 * success / completed, 3) if completed else None
        def objective(name: str, target: float, value: float | None, denominator: int, failures: int, extra: dict | None = None) -> dict:
            allowed = denominator * (1 - target / 100)
            budget = 100 if not denominator or not allowed else round(max(0, (allowed - failures) / allowed * 100), 1)
            return {"key": name, "target": target, "value": value, "total": denominator, "good": denominator - failures, "failed": failures, "error_budget_remaining": budget, "status": "no_data" if value is None else "healthy" if value >= target else "breached", **(extra or {})}
        durations.sort()
        percentile = lambda p: round(durations[min(len(durations) - 1, int(len(durations) * p))] / 60, 1) if durations else None
        return {"window_hours": hours, "generated_at": now.isoformat(), "objectives": [objective("read_api_availability", 99.9, api_value, total, failed, {"latency_avg_ms": round(int(metrics[3] or 0) / total) if total else None, "latency_max_ms": int(metrics[4] or 0)}), objective("paper_processing_24h", 99.0, task_value, completed, completed - success, {"external_failures": external_failures, "platform_failures": platform_failures, "p50_minutes": percentile(.5), "p95_minutes": percentile(.95)})]}
    finally:
        db.close()


def evaluate_alerts() -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        seen: dict[str, tuple[str, str, str, dict]] = {}
        client = redis.Redis.from_url(settings.redis_url)
        try:
            client.ping()
        except redis.RedisError as exc:
            seen["redis_unavailable"] = ("dependency_unavailable", "critical", "Redis is unavailable", {"error": str(exc)})
            client = None
        if client:
            workers = Worker.all(connection=client)
            if not any(worker.last_heartbeat and worker.last_heartbeat >= now - timedelta(seconds=90) for worker in workers):
                seen["worker_unavailable"] = ("worker_unavailable", "critical", "No healthy RQ worker heartbeat", {})
            oldest = 0
            for name in QUEUE_NAMES:
                ids = Queue(name, connection=client).get_job_ids(offset=0, length=1)
                if ids:
                    job = Queue(name, connection=client).fetch_job(ids[0])
                    if job and job.enqueued_at:
                        oldest = max(oldest, int((now - job.enqueued_at).total_seconds()))
                if ScheduledJobRegistry(name, connection=client).get_expired_job_ids():
                    seen["scheduler_overdue"] = ("scheduler_overdue", "critical", "RQ scheduled jobs are overdue", {})
            if oldest >= 600:
                seen["queue_oldest"] = ("queue_delay", "critical" if oldest >= 1800 else "warning", "The oldest queued job is delayed", {"age_seconds": oldest})
        cutoff = now - timedelta(minutes=30)
        failures = db.scalars(select(PaperTask).where(PaperTask.operation == "process", PaperTask.status == "failed", PaperTask.finished_at >= cutoff)).all()
        grouped: dict[tuple[str, str], int] = {}
        for task in failures:
            key = (task.stage or "unknown", task.error_code or "TASK_EXECUTION_FAILED")
            grouped[key] = grouped.get(key, 0) + 1
        for (stage, code), count in grouped.items():
            if count >= 3:
                seen[f"task_failures:{stage}:{code}"] = ("repeated_task_failure", "warning", f"{count} paper tasks failed at {stage}", {"stage": stage, "error_code": code, "count": count})
        for config in db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True))).all():
            healthy = db.scalar(select(func.count()).select_from(ProviderCredential).where(ProviderCredential.provider_config_id == config.provider_config_id, ProviderCredential.status == "active"))
            if not healthy:
                seen[f"provider_missing:{config.role}"] = ("provider_unavailable", "critical", f"No active credential for {config.role}", {"role": config.role})
            if client and client.get(f"provider:breaker:{config.role}"):
                seen[f"provider_breaker:{config.role}"] = ("provider_circuit_open", "warning", f"Provider circuit is open for {config.role}", {"role": config.role, "ttl": client.ttl(f"provider:breaker:{config.role}")})
        for objective in slo_summary(24)["objectives"]:
            if objective["value"] is not None and objective["error_budget_remaining"] <= 50:
                severity = "critical" if objective["error_budget_remaining"] == 0 else "warning"
                seen[f"slo:{objective['key']}"] = ("slo_error_budget", severity, f"SLO error budget is low for {objective['key']}", objective)
        active = db.scalars(select(OperationalAlert).where(OperationalAlert.status.in_(("active", "acknowledged")))).all()
        by_fingerprint = {alert.fingerprint: alert for alert in active}
        for fingerprint, (rule, severity, summary, details) in seen.items():
            alert = by_fingerprint.pop(fingerprint, None)
            if alert:
                alert.severity, alert.summary, alert.details = severity, summary, {**details, "healthy_checks": 0}
                alert.last_seen_at, alert.occurrence_count = now, alert.occurrence_count + 1
            else:
                db.add(OperationalAlert(alert_id=new_id("alert"), fingerprint=fingerprint, rule=rule, severity=severity, status="active", summary=summary, details={**details, "healthy_checks": 0}, first_seen_at=now, last_seen_at=now))
        for alert in by_fingerprint.values():
            details = dict(alert.details or {})
            checks = int(details.get("healthy_checks", 0)) + 1
            if checks >= 2:
                alert.status, alert.resolved_at = "resolved", now
            else:
                alert.details = {**details, "healthy_checks": checks}
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
