"""Unify import intake around staged artifacts and Pipeline V3."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_import_pipeline_v3"
down_revision: str | Sequence[str] | None = "0014_staged_import_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM paper_tasks
                        WHERE pipeline_version = 2
                          AND operation IN (
                              'pipeline', 'remote_import', 'staged_import',
                              'parse', 'evidence', 'resource_context',
                              'vectorize', 'index'
                          )
                          AND (
                              status IN (
                                  'pending', 'running', 'waiting_external',
                                  'retrying', 'retry_wait'
                              )
                              OR delivery_status IN (
                                  'pending', 'dispatching', 'enqueued'
                              )
                          )
                    ) THEN
                        RAISE EXCEPTION
                            'Pipeline V2 import tasks must be drained before 0015';
                    END IF;
                END $$
                """
            )
        )

    op.create_table(
        "staged_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column(
            "storage_provider",
            sa.String(20),
            nullable=False,
            server_default="configured",
        ),
        sa.Column("storage_key", sa.Text(), nullable=False, unique=True),
        sa.Column("metadata_key", sa.Text()),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "mime_type",
            sa.String(100),
            nullable=False,
            server_default="application/pdf",
        ),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("source_external_id", sa.String(255)),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="available"
        ),
        sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id")),
        sa.Column("metadata_payload", postgresql.JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_staged_artifacts_sha256", "staged_artifacts", ["sha256"])
    op.create_index(
        "ix_staged_artifacts_source_type", "staged_artifacts", ["source_type"]
    )
    op.create_index(
        "ix_staged_artifacts_source_external_id",
        "staged_artifacts",
        ["source_external_id"],
    )
    op.create_index("ix_staged_artifacts_status", "staged_artifacts", ["status"])
    op.create_index("ix_staged_artifacts_paper_id", "staged_artifacts", ["paper_id"])

    op.add_column(
        "import_batch_items", sa.Column("staged_artifact_id", sa.String(64))
    )
    op.create_foreign_key(
        "fk_import_batch_items_staged_artifact",
        "import_batch_items",
        "staged_artifacts",
        ["staged_artifact_id"],
        ["artifact_id"],
    )
    op.create_index(
        "ix_import_batch_items_staged_artifact_id",
        "import_batch_items",
        ["staged_artifact_id"],
    )
    op.add_column(
        "corpus_documents", sa.Column("staged_artifact_id", sa.String(64))
    )
    op.create_foreign_key(
        "fk_corpus_documents_staged_artifact",
        "corpus_documents",
        "staged_artifacts",
        ["staged_artifact_id"],
        ["artifact_id"],
    )
    op.create_index(
        "ix_corpus_documents_staged_artifact_id",
        "corpus_documents",
        ["staged_artifact_id"],
    )

    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                INSERT INTO staged_artifacts (
                    artifact_id, storage_provider, storage_key, metadata_key,
                    sha256, size_bytes, source_type, source_external_id,
                    status, paper_id, metadata_payload, created_at, updated_at
                )
                SELECT
                    'artifact_' || md5(document.document_id || '_pdf'),
                    'configured',
                    document.pdf_storage_key,
                    document.metadata_storage_key,
                    COALESCE(
                        document.pdf_sha256,
                        md5(document.pdf_storage_key)
                            || md5(document.pdf_storage_key || '_pdf')
                    ),
                    COALESCE(document.pdf_size_bytes, 0),
                    'openalex_harvest',
                    document.external_id,
                    'available',
                    document.registered_paper_id,
                    document.metadata_payload,
                    COALESCE(document.staged_at, document.created_at, now()),
                    now()
                FROM corpus_documents AS document
                WHERE document.pdf_storage_key IS NOT NULL
                ON CONFLICT (storage_key) DO NOTHING
                """
            )
        )
        connection.execute(
            sa.text(
                """
                UPDATE corpus_documents AS document
                SET staged_artifact_id = artifact.artifact_id
                FROM staged_artifacts AS artifact
                WHERE artifact.storage_key = document.pdf_storage_key
                  AND document.staged_artifact_id IS NULL
                """
            )
        )
        connection.execute(
            sa.text(
                """
                UPDATE import_batch_items AS batch_item
                SET staged_artifact_id = document.staged_artifact_id
                FROM harvest_job_items AS harvest_item,
                     corpus_documents AS document
                WHERE harvest_item.import_batch_item_id = batch_item.item_id
                  AND harvest_item.document_id = document.document_id
                  AND batch_item.staged_artifact_id IS NULL
                """
            )
        )
        op.drop_index("uq_pipeline_stage_per_root", table_name="paper_tasks")
        op.create_index(
            "uq_pipeline_stage_per_root",
            "paper_tasks",
            ["root_task_id", "pipeline_version", "operation"],
            unique=True,
            postgresql_where=sa.text(
                "root_task_id IS NOT NULL AND operation IN "
                "('remote_import', 'staged_import', 'remote_fetch', 'register', "
                "'parse', 'evidence', 'resource_context', 'vectorize', 'index')"
            ),
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.drop_index("uq_pipeline_stage_per_root", table_name="paper_tasks")
        op.create_index(
            "uq_pipeline_stage_per_root",
            "paper_tasks",
            ["root_task_id", "pipeline_version", "operation"],
            unique=True,
            postgresql_where=sa.text(
                "root_task_id IS NOT NULL AND operation IN "
                "('remote_import', 'staged_import', 'parse', 'evidence', "
                "'resource_context', 'vectorize', 'index')"
            ),
        )
    op.drop_index(
        "ix_corpus_documents_staged_artifact_id", table_name="corpus_documents"
    )
    op.drop_constraint(
        "fk_corpus_documents_staged_artifact",
        "corpus_documents",
        type_="foreignkey",
    )
    op.drop_column("corpus_documents", "staged_artifact_id")
    op.drop_index(
        "ix_import_batch_items_staged_artifact_id", table_name="import_batch_items"
    )
    op.drop_constraint(
        "fk_import_batch_items_staged_artifact",
        "import_batch_items",
        type_="foreignkey",
    )
    op.drop_column("import_batch_items", "staged_artifact_id")
    op.drop_table("staged_artifacts")
