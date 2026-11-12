"""Add operational control, provider metrics, and execution history."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006_operational_control_plane"
down_revision: str | Sequence[str] | None = "0005_evidence_fulltext_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_tasks", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
    op.add_column("import_batch_items", sa.Column("details", postgresql.JSONB()))
    op.add_column("papers", sa.Column("metadata_provenance", postgresql.JSONB()))

    op.create_table(
        "provider_configs",
        sa.Column("provider_config_id", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rpm_limit", sa.Integer()),
        sa.Column("tpm_limit", sa.Integer()),
        sa.Column("breaker_threshold", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("breaker_cooldown_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("role", name="uq_provider_configs_role"),
    )
    op.create_index("ix_provider_configs_role", "provider_configs", ["role"], unique=True)
    op.create_table(
        "provider_credentials",
        sa.Column("credential_id", sa.String(64), primary_key=True),
        sa.Column("provider_config_id", sa.String(64), sa.ForeignKey("provider_configs.provider_config_id"), nullable=False),
        sa.Column("alias", sa.String(100), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("key_suffix", sa.String(8), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("provider_config_id", "alias", name="uq_provider_credential_alias"),
    )
    op.create_index("ix_provider_credentials_config", "provider_credentials", ["provider_config_id"])
    op.create_index("ix_provider_credentials_status", "provider_credentials", ["status"])
    op.create_table(
        "provider_call_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200)),
        sa.Column("credential_id", sa.String(64)),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("request_id", sa.String(100)),
        sa.Column("trace_id", sa.String(100)),
        sa.Column("task_id", sa.String(64)),
        sa.Column("run_id", sa.String(64)),
        sa.Column("tokens_in", sa.Integer()),
        sa.Column("tokens_out", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for column in ("role", "provider", "credential_id", "status", "error_code", "request_id", "trace_id", "task_id", "run_id", "created_at"):
        op.create_index(f"ix_provider_call_events_{column}", "provider_call_events", [column])
    op.create_table(
        "provider_metric_hourly",
        sa.Column("metric_id", sa.String(64), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200)),
        sa.Column("total_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate_limited_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timeout_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_sum_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_max_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("bucket_start", "role", "provider", "model", name="uq_provider_metric_bucket"),
    )
    op.create_index("ix_provider_metric_hourly_bucket", "provider_metric_hourly", ["bucket_start"])
    op.create_table(
        "execution_attempts",
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("owner_type", sa.String(20), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50)),
        sa.Column("credential_id", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner_type", "owner_id", "attempt", name="uq_execution_attempt_owner"),
    )
    for column in ("owner_type", "owner_id", "status"):
        op.create_index(f"ix_execution_attempts_{column}", "execution_attempts", [column])
    op.create_table(
        "config_audit_logs",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(50), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("changes", postgresql.JSONB()),
        sa.Column("request_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_config_audit_logs_action", "config_audit_logs", ["action"])
    op.create_index("ix_config_audit_logs_created_at", "config_audit_logs", ["created_at"])
    op.create_table(
        "evidence_judgements",
        sa.Column("judgement_id", sa.String(64), primary_key=True),
        sa.Column("cache_key", sa.String(64), nullable=False, unique=True),
        sa.Column("claim_hash", sa.String(64), nullable=False),
        sa.Column("evidence_id", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_evidence_judgements_claim_hash", "evidence_judgements", ["claim_hash"])
    op.create_index("ix_evidence_judgements_evidence_id", "evidence_judgements", ["evidence_id"])


def downgrade() -> None:
    op.drop_table("evidence_judgements")
    op.drop_table("config_audit_logs")
    op.drop_table("execution_attempts")
    op.drop_table("provider_metric_hourly")
    op.drop_table("provider_call_events")
    op.drop_table("provider_credentials")
    op.drop_table("provider_configs")
    op.drop_column("import_batch_items", "details")
    op.drop_column("paper_tasks", "cancel_requested_at")
    op.drop_column("papers", "metadata_provenance")
