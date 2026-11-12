"""Add durable Pipeline V2 task and batch state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_pipeline_v2"
down_revision: str | Sequence[str] | None = "0011_operational_maturity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("index_status", sa.String(50)))
    op.execute("UPDATE papers SET index_status = 'not_started' WHERE index_status IS NULL")
    op.alter_column(
        "papers", "index_status", nullable=False, server_default="not_started"
    )
    op.create_index("ix_papers_index_status", "papers", ["index_status"])

    task_columns = (
        sa.Column("batch_id", sa.String(64), sa.ForeignKey("import_batches.batch_id")),
        sa.Column(
            "batch_item_id",
            sa.String(64),
            sa.ForeignKey("import_batch_items.item_id"),
        ),
        sa.Column("queue_name", sa.String(100)),
        sa.Column("delivery_status", sa.String(20)),
        sa.Column(
            "delivery_generation", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("delivery_lease_until", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True)),
        sa.Column("next_action", sa.String(20)),
        sa.Column("run_token", sa.String(64)),
        sa.Column("run_lease_until", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(200)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("pipeline_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "pipeline_generation", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("idempotency_key", sa.String(255)),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("input_fingerprint", sa.String(64)),
        sa.Column("output_fingerprint", sa.String(64)),
    )
    for column in task_columns:
        op.add_column("paper_tasks", column)
    for column in ("batch_id", "batch_item_id", "delivery_status", "run_token"):
        op.create_index(f"ix_paper_tasks_{column}", "paper_tasks", [column])
    op.create_index(
        "ix_paper_tasks_batch_status", "paper_tasks", ["batch_id", "status"]
    )
    op.create_index(
        "ix_paper_tasks_item_operation",
        "paper_tasks",
        ["batch_item_id", "operation"],
    )
    op.create_index(
        "ix_paper_tasks_root_generation",
        "paper_tasks",
        ["root_task_id", "pipeline_generation"],
    )
    op.create_index(
        "ix_paper_tasks_paper_operation_status",
        "paper_tasks",
        ["paper_id", "operation", "status"],
    )
    op.create_index(
        "ix_paper_tasks_status_run_lease",
        "paper_tasks",
        ["status", "run_lease_until"],
    )
    op.create_index(
        "ix_paper_tasks_dispatch",
        "paper_tasks",
        ["queue_name", "available_at", sa.text("priority DESC"), "created_at"],
        postgresql_where=sa.text("delivery_status = 'pending'"),
    )
    op.create_index(
        "uq_pipeline_stage_per_root",
        "paper_tasks",
        ["root_task_id", "pipeline_version", "operation"],
        unique=True,
        postgresql_where=sa.text(
            "root_task_id IS NOT NULL AND operation IN "
            "('remote_import', 'parse', 'evidence', 'resource_context', "
            "'vectorize', 'index')"
        ),
    )
    op.create_index(
        "uq_paper_tasks_idempotency",
        "paper_tasks",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    for column in (
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column(
            "intake_closed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("manifest_storage_key", sa.Text()),
        sa.Column("expansion_cursor", sa.Text()),
        sa.Column("expanded_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expansion_status", sa.String(32)),
        sa.Column("expansion_error", sa.Text()),
    ):
        op.add_column("import_batches", column)

    for column in (
        sa.Column("pipeline_task_id", sa.String(64)),
        sa.Column("current_stage", sa.String(50)),
        sa.Column("pipeline_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "duplicate_of_paper_id",
            sa.String(64),
            sa.ForeignKey("papers.paper_id"),
        ),
        sa.Column("idempotency_key", sa.String(255)),
    ):
        op.add_column("import_batch_items", column)
    op.create_index(
        "ix_import_batch_items_pipeline_task_id",
        "import_batch_items",
        ["pipeline_task_id"],
    )
    op.create_index(
        "ix_import_batch_items_current_stage",
        "import_batch_items",
        ["current_stage"],
    )
    op.create_index(
        "uq_import_batch_items_idempotency",
        "import_batch_items",
        ["batch_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    for column in (
        sa.Column("run_token", sa.String(64)),
        sa.Column("worker_id", sa.String(200)),
        sa.Column("input_fingerprint", sa.String(64)),
        sa.Column("output_fingerprint", sa.String(64)),
        sa.Column("details", postgresql.JSONB()),
    ):
        op.add_column("execution_attempts", column)

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                DELETE FROM evidence_embeddings older
                USING evidence_embeddings newer
                WHERE older.representation_id = newer.representation_id
                  AND older.embedding_model = newer.embedding_model
                  AND older.id < newer.id
                """
            )
        )
    op.create_unique_constraint(
        "uq_evidence_embeddings_representation_model",
        "evidence_embeddings",
        ["representation_id", "embedding_model"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_evidence_embeddings_representation_model",
        "evidence_embeddings",
        type_="unique",
    )
    for column in (
        "details",
        "output_fingerprint",
        "input_fingerprint",
        "worker_id",
        "run_token",
    ):
        op.drop_column("execution_attempts", column)

    op.drop_index(
        "uq_import_batch_items_idempotency", table_name="import_batch_items"
    )
    op.drop_index(
        "ix_import_batch_items_current_stage", table_name="import_batch_items"
    )
    op.drop_index(
        "ix_import_batch_items_pipeline_task_id", table_name="import_batch_items"
    )
    for column in (
        "idempotency_key",
        "duplicate_of_paper_id",
        "finished_at",
        "pipeline_version",
        "current_stage",
        "pipeline_task_id",
    ):
        op.drop_column("import_batch_items", column)

    for column in (
        "expansion_error",
        "expansion_status",
        "expanded_items",
        "expansion_cursor",
        "manifest_storage_key",
        "intake_closed",
        "priority",
    ):
        op.drop_column("import_batches", column)

    for index in (
        "uq_paper_tasks_idempotency",
        "uq_pipeline_stage_per_root",
        "ix_paper_tasks_dispatch",
        "ix_paper_tasks_status_run_lease",
        "ix_paper_tasks_paper_operation_status",
        "ix_paper_tasks_root_generation",
        "ix_paper_tasks_item_operation",
        "ix_paper_tasks_batch_status",
        "ix_paper_tasks_run_token",
        "ix_paper_tasks_delivery_status",
        "ix_paper_tasks_batch_item_id",
        "ix_paper_tasks_batch_id",
    ):
        op.drop_index(index, table_name="paper_tasks")
    for column in (
        "output_fingerprint",
        "input_fingerprint",
        "priority",
        "idempotency_key",
        "pipeline_generation",
        "pipeline_version",
        "heartbeat_at",
        "worker_id",
        "run_lease_until",
        "run_token",
        "next_action",
        "available_at",
        "delivery_lease_until",
        "delivery_generation",
        "delivery_status",
        "queue_name",
        "batch_item_id",
        "batch_id",
    ):
        op.drop_column("paper_tasks", column)

    op.drop_index("ix_papers_index_status", table_name="papers")
    op.drop_column("papers", "index_status")
