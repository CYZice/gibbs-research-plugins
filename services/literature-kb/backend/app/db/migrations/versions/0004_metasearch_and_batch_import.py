"""Add local metasearch documents and resumable import batches."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision: str = "0004_metasearch_and_batch_import"
down_revision: str | None = "0003_workflow_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.add_column("papers", sa.Column("publication_date", sa.Date()))
    op.add_column("papers", sa.Column("license", sa.String(255)))
    op.add_column("papers", sa.Column("open_access", sa.Boolean()))
    op.add_column("papers", sa.Column("parse_version", sa.String(100)))

    op.create_table(
        "paper_identifiers",
        sa.Column("identifier_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id"), nullable=False),
        sa.Column("identifier_type", sa.String(32), nullable=False),
        sa.Column("normalized_value", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("identifier_type", "normalized_value", name="uq_paper_identifiers_type_value"),
    )
    op.create_index("ix_paper_identifiers_paper_id", "paper_identifiers", ["paper_id"])
    op.execute("""
        INSERT INTO paper_identifiers (paper_id, identifier_type, normalized_value)
        SELECT paper_id, 'doi', lower(regexp_replace(doi, '^https?://(dx\\.)?doi\\.org/', ''))
        FROM papers WHERE doi IS NOT NULL AND doi <> ''
        ON CONFLICT (identifier_type, normalized_value) DO NOTHING
    """)
    op.execute("""
        INSERT INTO paper_identifiers (paper_id, identifier_type, normalized_value)
        SELECT paper_id, 'arxiv', lower(regexp_replace(arxiv_id, '^arxiv:', ''))
        FROM papers WHERE arxiv_id IS NOT NULL AND arxiv_id <> ''
        ON CONFLICT (identifier_type, normalized_value) DO NOTHING
    """)

    op.create_table(
        "paper_search_documents",
        sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id"), primary_key=True),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=False, server_default=sa.text("''::tsvector")),
        sa.Column("embedding", Vector(1024)),
        sa.Column("embedding_model", sa.Text()),
        sa.Column("search_index_version", sa.Text(), nullable=False),
        sa.Column("search_tier", sa.Text(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_paper_search_documents_vector", "paper_search_documents", ["search_vector"], postgresql_using="gin")
    op.execute("CREATE INDEX ix_paper_search_documents_text_trgm ON paper_search_documents USING gin (search_text gin_trgm_ops)")
    op.execute("CREATE INDEX ix_paper_search_documents_embedding_hnsw ON paper_search_documents USING hnsw (embedding vector_l2_ops)")
    op.execute("CREATE INDEX ix_evidence_embeddings_embedding_hnsw ON evidence_embeddings USING hnsw (embedding vector_l2_ops)")

    op.create_table(
        "import_batches",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_import_batches_status", "import_batches", ["status"])
    op.create_table(
        "import_batch_items",
        sa.Column("item_id", sa.String(64), primary_key=True),
        sa.Column("batch_id", sa.String(64), sa.ForeignKey("import_batches.batch_id"), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id")),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text()),
        sa.Column("metadata_payload", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("batch_id", "source_key", name="uq_import_batch_items_source"),
    )
    op.create_index("ix_import_batch_items_batch_id", "import_batch_items", ["batch_id"])
    op.create_index("ix_import_batch_items_paper_id", "import_batch_items", ["paper_id"])
    op.create_index("ix_import_batch_items_status", "import_batch_items", ["status"])


def downgrade() -> None:
    op.drop_table("import_batch_items")
    op.drop_table("import_batches")
    op.execute("DROP INDEX IF EXISTS ix_evidence_embeddings_embedding_hnsw")
    op.drop_table("paper_search_documents")
    op.drop_table("paper_identifiers")
    op.drop_column("papers", "parse_version")
    op.drop_column("papers", "open_access")
    op.drop_column("papers", "license")
    op.drop_column("papers", "publication_date")
