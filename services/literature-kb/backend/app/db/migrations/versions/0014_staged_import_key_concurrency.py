"""Move Harvest registration into Pipeline V2 and make concurrency per key."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_staged_import_keys"
down_revision: str | Sequence[str] | None = "0013_openalex_harvest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_credentials",
        sa.Column(
            "max_concurrency",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
    op.drop_column("provider_configs", "max_concurrency")

    op.alter_column(
        "harvest_jobs",
        "imported_items",
        new_column_name="registered_items",
    )
    op.alter_column(
        "corpus_documents",
        "imported_paper_id",
        new_column_name="registered_paper_id",
    )
    op.alter_column(
        "harvest_job_items",
        "imported_paper_id",
        new_column_name="registered_paper_id",
    )
    op.add_column(
        "harvest_job_items",
        sa.Column("import_batch_item_id", sa.String(64)),
    )
    op.create_foreign_key(
        "fk_harvest_job_items_import_batch_item",
        "harvest_job_items",
        "import_batch_items",
        ["import_batch_item_id"],
        ["item_id"],
    )
    op.create_index(
        "ix_harvest_job_items_import_batch_item_id",
        "harvest_job_items",
        ["import_batch_item_id"],
    )

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                ALTER INDEX ix_corpus_documents_imported_paper_id
                RENAME TO ix_corpus_documents_registered_paper_id
                """
            )
        )
        connection.execute(
            sa.text(
                """
                ALTER INDEX ix_harvest_job_items_imported_paper_id
                RENAME TO ix_harvest_job_items_registered_paper_id
                """
            )
        )
        connection.execute(
            sa.text(
                """
                ALTER TABLE corpus_documents
                RENAME CONSTRAINT corpus_documents_imported_paper_id_fkey
                TO corpus_documents_registered_paper_id_fkey
                """
            )
        )
        connection.execute(
            sa.text(
                """
                ALTER TABLE harvest_job_items
                RENAME CONSTRAINT harvest_job_items_imported_paper_id_fkey
                TO harvest_job_items_registered_paper_id_fkey
                """
            )
        )
    else:
        op.drop_index(
            "ix_corpus_documents_imported_paper_id",
            table_name="corpus_documents",
        )
        op.create_index(
            "ix_corpus_documents_registered_paper_id",
            "corpus_documents",
            ["registered_paper_id"],
        )
        op.drop_index(
            "ix_harvest_job_items_imported_paper_id",
            table_name="harvest_job_items",
        )
        op.create_index(
            "ix_harvest_job_items_registered_paper_id",
            "harvest_job_items",
            ["registered_paper_id"],
        )

    op.execute(
        """
        UPDATE harvest_job_items
        SET status = CASE
            WHEN status = 'importing' THEN 'registering'
            WHEN status = 'imported' THEN 'registered'
            ELSE status
        END
        """
    )
    op.execute(
        """
        UPDATE harvest_job_items
        SET status = 'registered'
        WHERE status = 'registering'
          AND registered_paper_id IS NOT NULL
        """
    )

    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                UPDATE harvest_job_items AS harvest_item
                SET import_batch_item_id = batch_item.item_id
                FROM corpus_documents AS document,
                     import_batch_items AS batch_item
                WHERE document.document_id = harvest_item.document_id
                  AND batch_item.batch_id = harvest_item.import_batch_id
                  AND batch_item.source_key = 'harvest:' || document.external_id
                  AND harvest_item.import_batch_item_id IS NULL
                """
            )
        )
        op.drop_index(
            "uq_pipeline_stage_per_root", table_name="paper_tasks"
        )
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
        connection.execute(
            sa.text(
                """
                INSERT INTO paper_tasks (
                    task_id, paper_id, operation, provider, status, progress,
                    details, stage, root_task_id, batch_id, batch_item_id,
                    delivery_generation, pipeline_version, pipeline_generation,
                    priority, created_at, updated_at
                )
                SELECT
                    'task_' || md5(harvest_item.item_id || '-staged-root'),
                    NULL,
                    'pipeline',
                    'openalex_harvest',
                    'pending',
                    0,
                    jsonb_build_object(
                        'pipeline_version', 2,
                        'skipped_stages', jsonb_build_array('remote_import'),
                        'harvest_item_id', harvest_item.item_id,
                        'corpus_document_id', harvest_item.document_id
                    ),
                    'staged_import',
                    'task_' || md5(harvest_item.item_id || '-staged-root'),
                    harvest_item.import_batch_id,
                    harvest_item.import_batch_item_id,
                    0, 2, 1, 50, now(), now()
                FROM harvest_job_items AS harvest_item
                JOIN import_batch_items AS batch_item
                  ON batch_item.item_id = harvest_item.import_batch_item_id
                WHERE harvest_item.status = 'registering'
                  AND harvest_item.registered_paper_id IS NULL
                  AND batch_item.pipeline_task_id IS NULL
                ON CONFLICT (task_id) DO NOTHING
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO paper_tasks (
                    task_id, paper_id, operation, provider, status, progress,
                    details, stage, root_task_id, batch_id, batch_item_id,
                    queue_name, delivery_status, delivery_generation,
                    available_at, next_action, pipeline_version,
                    pipeline_generation, idempotency_key, priority,
                    created_at, updated_at
                )
                SELECT
                    'task_' || md5(harvest_item.item_id || '-staged-stage'),
                    NULL,
                    'staged_import',
                    'openalex_harvest',
                    'pending',
                    0,
                    jsonb_build_object(
                        'pipeline_version', 2,
                        'skipped_stages', jsonb_build_array('remote_import'),
                        'harvest_item_id', harvest_item.item_id,
                        'corpus_document_id', harvest_item.document_id
                    ),
                    'staged_import',
                    'task_' || md5(harvest_item.item_id || '-staged-root'),
                    harvest_item.import_batch_id,
                    harvest_item.import_batch_item_id,
                    'literature-ingest',
                    'pending',
                    0,
                    now(),
                    'execute',
                    2,
                    1,
                    'task_' || md5(harvest_item.item_id || '-staged-root')
                        || '-1-staged_import',
                    50,
                    now(),
                    now()
                FROM harvest_job_items AS harvest_item
                JOIN import_batch_items AS batch_item
                  ON batch_item.item_id = harvest_item.import_batch_item_id
                WHERE harvest_item.status = 'registering'
                  AND harvest_item.registered_paper_id IS NULL
                  AND batch_item.pipeline_task_id IS NULL
                ON CONFLICT (task_id) DO NOTHING
                """
            )
        )
        connection.execute(
            sa.text(
                """
                UPDATE import_batch_items AS batch_item
                SET pipeline_task_id =
                        'task_' || md5(harvest_item.item_id || '-staged-root'),
                    current_stage = 'staged_import',
                    status = 'pending',
                    details = COALESCE(batch_item.details, '{}'::jsonb)
                        || jsonb_build_object('stage', 'staged_import')
                FROM harvest_job_items AS harvest_item
                WHERE harvest_item.import_batch_item_id = batch_item.item_id
                  AND harvest_item.status = 'registering'
                  AND harvest_item.registered_paper_id IS NULL
                  AND batch_item.pipeline_task_id IS NULL
                """
            )
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                DELETE FROM paper_tasks
                WHERE provider = 'openalex_harvest'
                  AND operation IN ('pipeline', 'staged_import')
                  AND details ? 'harvest_item_id'
                  AND paper_id IS NULL
                """
            )
        )
        op.drop_index(
            "uq_pipeline_stage_per_root", table_name="paper_tasks"
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

    op.execute(
        """
        UPDATE harvest_job_items
        SET status = CASE
            WHEN status = 'registering' THEN 'importing'
            WHEN status = 'registered' THEN 'imported'
            ELSE status
        END
        """
    )
    op.drop_index(
        "ix_harvest_job_items_import_batch_item_id",
        table_name="harvest_job_items",
    )
    op.drop_constraint(
        "fk_harvest_job_items_import_batch_item",
        "harvest_job_items",
        type_="foreignkey",
    )
    op.drop_column("harvest_job_items", "import_batch_item_id")

    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                ALTER INDEX ix_corpus_documents_registered_paper_id
                RENAME TO ix_corpus_documents_imported_paper_id
                """
            )
        )
        connection.execute(
            sa.text(
                """
                ALTER INDEX ix_harvest_job_items_registered_paper_id
                RENAME TO ix_harvest_job_items_imported_paper_id
                """
            )
        )
        connection.execute(
            sa.text(
                """
                ALTER TABLE corpus_documents
                RENAME CONSTRAINT corpus_documents_registered_paper_id_fkey
                TO corpus_documents_imported_paper_id_fkey
                """
            )
        )
        connection.execute(
            sa.text(
                """
                ALTER TABLE harvest_job_items
                RENAME CONSTRAINT harvest_job_items_registered_paper_id_fkey
                TO harvest_job_items_imported_paper_id_fkey
                """
            )
        )
    else:
        op.drop_index(
            "ix_corpus_documents_registered_paper_id",
            table_name="corpus_documents",
        )
        op.create_index(
            "ix_corpus_documents_imported_paper_id",
            "corpus_documents",
            ["registered_paper_id"],
        )
        op.drop_index(
            "ix_harvest_job_items_registered_paper_id",
            table_name="harvest_job_items",
        )
        op.create_index(
            "ix_harvest_job_items_imported_paper_id",
            "harvest_job_items",
            ["registered_paper_id"],
        )

    op.alter_column(
        "harvest_job_items",
        "registered_paper_id",
        new_column_name="imported_paper_id",
    )
    op.alter_column(
        "corpus_documents",
        "registered_paper_id",
        new_column_name="imported_paper_id",
    )
    op.alter_column(
        "harvest_jobs",
        "registered_items",
        new_column_name="imported_items",
    )

    op.add_column(
        "provider_configs",
        sa.Column(
            "max_concurrency",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
    op.drop_column("provider_credentials", "max_concurrency")
