"""Add isolated OpenAlex corpus harvest state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_openalex_harvest"
down_revision: str | Sequence[str] | None = "0012_pipeline_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "harvest_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="openalex"),
        sa.Column("status", sa.String(32), nullable=False, server_default="enumerating"),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column("taxonomy_snapshot", postgresql.JSONB()),
        sa.Column(
            "billing_mode", sa.String(32), nullable=False, server_default="free_credit_only"
        ),
        sa.Column("ingest_mode", sa.String(32), nullable=False, server_default="stage_only"),
        sa.Column("next_cursor", sa.Text(), server_default="*"),
        sa.Column(
            "enumeration_complete", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("estimated_items", sa.Integer()),
        sa.Column("enumerated_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("staged_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("no_source_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("imported_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("details", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_harvest_jobs_status", "harvest_jobs", ["status"])
    op.create_index("ix_harvest_jobs_next_retry_at", "harvest_jobs", ["next_retry_at"])

    op.create_table(
        "corpus_documents",
        sa.Column("document_id", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="openalex"),
        sa.Column("external_id", sa.String(100), nullable=False, unique=True),
        sa.Column("doi", sa.String(255)),
        sa.Column("title", sa.Text()),
        sa.Column("year", sa.Integer()),
        sa.Column("publication_type", sa.String(50)),
        sa.Column("language", sa.String(20)),
        sa.Column("metadata_payload", postgresql.JSONB(), nullable=False),
        sa.Column("candidate_urls", postgresql.JSONB()),
        sa.Column("content_url", sa.Text()),
        sa.Column("pdf_storage_key", sa.Text()),
        sa.Column("metadata_storage_key", sa.Text()),
        sa.Column("pdf_sha256", sa.String(64)),
        sa.Column("pdf_size_bytes", sa.Integer()),
        sa.Column("download_source", sa.String(50)),
        sa.Column("source_url", sa.Text()),
        sa.Column("staged_at", sa.DateTime(timezone=True)),
        sa.Column("imported_paper_id", sa.String(64), sa.ForeignKey("papers.paper_id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for column in (
        "doi",
        "year",
        "publication_type",
        "language",
        "pdf_sha256",
        "imported_paper_id",
    ):
        op.create_index(f"ix_corpus_documents_{column}", "corpus_documents", [column])

    op.create_table(
        "harvest_credentials",
        sa.Column("credential_id", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="openalex"),
        sa.Column("alias", sa.String(100), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("key_suffix", sa.String(8), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
        sa.Column("daily_budget_usd", sa.Float()),
        sa.Column("daily_used_usd", sa.Float()),
        sa.Column("daily_remaining_usd", sa.Float()),
        sa.Column("prepaid_remaining_usd", sa.Float()),
        sa.Column("content_cost_usd", sa.Float()),
        sa.Column("resets_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "alias", name="uq_harvest_credential_alias"),
    )
    op.create_index("ix_harvest_credentials_key_fingerprint", "harvest_credentials", ["key_fingerprint"])
    op.create_index("ix_harvest_credentials_status", "harvest_credentials", ["status"])
    op.create_index("ix_harvest_credentials_resets_at", "harvest_credentials", ["resets_at"])

    op.create_table(
        "harvest_job_items",
        sa.Column("item_id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("harvest_jobs.job_id"), nullable=False),
        sa.Column(
            "document_id",
            sa.String(64),
            sa.ForeignKey("corpus_documents.document_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_source", sa.String(50)),
        sa.Column("credential_id", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("import_batch_id", sa.String(64), sa.ForeignKey("import_batches.batch_id")),
        sa.Column("imported_paper_id", sa.String(64), sa.ForeignKey("papers.paper_id")),
        sa.Column("details", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", "document_id", name="uq_harvest_job_document"),
    )
    for column in (
        "job_id",
        "document_id",
        "status",
        "credential_id",
        "error_code",
        "next_attempt_at",
        "import_batch_id",
        "imported_paper_id",
    ):
        op.create_index(f"ix_harvest_job_items_{column}", "harvest_job_items", [column])


def downgrade() -> None:
    op.drop_table("harvest_job_items")
    op.drop_table("harvest_credentials")
    op.drop_table("corpus_documents")
    op.drop_table("harvest_jobs")
