"""Add recoverable task state and the local operations control plane."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_operational_maturity"
down_revision: str | Sequence[str] | None = "0010_consolidate_provider_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_tasks", sa.Column("stage", sa.String(50)))
    op.add_column("paper_tasks", sa.Column("last_completed_stage", sa.String(50)))
    op.add_column("paper_tasks", sa.Column("error_code", sa.String(64)))
    op.add_column("paper_tasks", sa.Column("error_domain", sa.String(20)))
    op.add_column("paper_tasks", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("paper_tasks", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="4"))
    op.add_column("paper_tasks", sa.Column("next_retry_at", sa.DateTime(timezone=True)))
    op.add_column("paper_tasks", sa.Column("root_task_id", sa.String(64)))
    for column in ("stage", "error_code", "error_domain", "root_task_id"):
        op.create_index(f"ix_paper_tasks_{column}", "paper_tasks", [column])
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("UPDATE paper_tasks SET stage = COALESCE(details->>'stage', status), root_task_id = task_id"))
    else:
        connection.execute(sa.text("UPDATE paper_tasks SET stage = status, root_task_id = task_id"))
    op.create_table(
        "operational_metric_hourly",
        sa.Column("metric_id", sa.String(64), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("metric", sa.String(64), nullable=False, index=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("good", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_sum_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_max_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details", postgresql.JSONB()),
        sa.UniqueConstraint("bucket_start", "metric", name="uq_operational_metric_hour"),
    )
    op.create_table(
        "operational_alerts",
        sa.Column("alert_id", sa.String(64), primary_key=True),
        sa.Column("fingerprint", sa.String(128), nullable=False, index=True),
        sa.Column("rule", sa.String(64), nullable=False, index=True),
        sa.Column("severity", sa.String(20), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active", index=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB()),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("acknowledged_note", sa.Text()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True), index=True),
    )


def downgrade() -> None:
    op.drop_table("operational_alerts")
    op.drop_table("operational_metric_hourly")
    for column in ("root_task_id", "error_domain", "error_code", "stage"):
        op.drop_index(f"ix_paper_tasks_{column}", table_name="paper_tasks")
    for column in ("root_task_id", "next_retry_at", "max_attempts", "attempt_count", "error_domain", "error_code", "last_completed_stage", "stage"):
        op.drop_column("paper_tasks", column)
