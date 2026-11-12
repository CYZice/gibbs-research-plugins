"""v1 contract reset baseline."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision: str = "0001_v1_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps(*names: str) -> list[sa.Column]:
    return [sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")) for name in names]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "papers",
        sa.Column("paper_id", sa.String(64), primary_key=True),
        sa.Column("title", sa.Text()), sa.Column("authors", postgresql.JSONB()), sa.Column("year", sa.Integer()),
        sa.Column("doi", sa.String(255)), sa.Column("arxiv_id", sa.String(100)), sa.Column("venue", sa.Text()),
        sa.Column("abstract", sa.Text()), sa.Column("source_provenance", sa.String(50)), sa.Column("source_url", sa.Text()),
        sa.Column("pdf_storage_key", sa.Text()), sa.Column("pdf_sha256", sa.String(64)), sa.Column("publication_type", sa.String(50)),
        sa.Column("language", sa.String(20)), sa.Column("keywords", postgresql.JSONB()),
        sa.Column("parse_status", sa.String(50), nullable=False, server_default="not_started"),
        sa.Column("evidence_status", sa.String(50), nullable=False, server_default="not_started"),
        sa.Column("embedding_status", sa.String(50), nullable=False, server_default="not_started"),
        sa.Column("multimodal_status", sa.String(50), nullable=False, server_default="not_started"),
        sa.Column("paper_quality_score", sa.Float()), sa.Column("paper_quality_signals", postgresql.JSONB()),
        *_timestamps("created_at", "updated_at"),
    )
    for name, column in (("ix_papers_doi", "doi"), ("ix_papers_arxiv_id", "arxiv_id"), ("ix_papers_source_provenance", "source_provenance"), ("ix_papers_publication_type", "publication_type"), ("ix_papers_language", "language"), ("ix_papers_parse_status", "parse_status"), ("ix_papers_evidence_status", "evidence_status"), ("ix_papers_embedding_status", "embedding_status"), ("ix_papers_multimodal_status", "multimodal_status")):
        op.create_index(name, "papers", [column])
    op.create_table("paper_contents", sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id"), primary_key=True), sa.Column("text", sa.Text(), nullable=False), sa.Column("content_sha256", sa.String(64), nullable=False), sa.Column("char_length", sa.Integer(), nullable=False), *_timestamps("created_at", "updated_at"))
    op.create_table("evidence_units", sa.Column("evidence_id", sa.String(96), primary_key=True), sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id"), nullable=False), sa.Column("page", sa.Integer()), sa.Column("char_start", sa.Integer()), sa.Column("char_end", sa.Integer()), sa.Column("section_title", sa.Text()), sa.Column("section_type", sa.String(50)), sa.Column("modality", sa.String(50), nullable=False), sa.Column("block_type", sa.String(50)), sa.Column("block_order", sa.Integer()), sa.Column("exclude_from_retrieval", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("raw_content", sa.Text(), nullable=False), sa.Column("bbox", postgresql.JSONB()), sa.Column("coordinate_space", sa.String(32)), sa.Column("asset_ids", postgresql.JSONB()), sa.Column("parse_source", sa.String(50)), sa.Column("confidence", sa.Float()), sa.Column("metadata", postgresql.JSONB()), *_timestamps("created_at"))
    for name, column in (("ix_evidence_units_paper_id", "paper_id"), ("ix_evidence_units_section_type", "section_type"), ("ix_evidence_units_modality", "modality"), ("ix_evidence_units_block_type", "block_type")):
        op.create_index(name, "evidence_units", [column])
    op.create_table("evidence_representations", sa.Column("representation_id", sa.String(128), primary_key=True), sa.Column("evidence_id", sa.String(96), sa.ForeignKey("evidence_units.evidence_id"), nullable=False), sa.Column("repr_type", sa.String(50), nullable=False), sa.Column("text", sa.Text(), nullable=False), sa.Column("model_name", sa.String(100)), sa.Column("prompt_version", sa.String(100)), sa.Column("text_hash", sa.String(64), nullable=False), sa.Column("status", sa.String(50), nullable=False, server_default="success"), sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("indexable", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("error_message", sa.Text()), sa.Column("metadata", postgresql.JSONB()), *_timestamps("created_at", "updated_at"))
    for name, column in (("ix_evidence_representations_evidence_id", "evidence_id"), ("ix_evidence_representations_repr_type", "repr_type"), ("ix_evidence_representations_text_hash", "text_hash"), ("ix_evidence_representations_status", "status"), ("ix_evidence_representations_is_active", "is_active"), ("ix_evidence_representations_indexable", "indexable")):
        op.create_index(name, "evidence_representations", [column])
    op.create_table("evidence_embeddings", sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True), sa.Column("representation_id", sa.String(128), sa.ForeignKey("evidence_representations.representation_id"), nullable=False), sa.Column("embedding_model", sa.String(100), nullable=False), sa.Column("text_hash", sa.String(64), nullable=False), sa.Column("embedding_dim", sa.Integer(), nullable=False), sa.Column("status", sa.String(50), nullable=False, server_default="success"), sa.Column("embedding", Vector(1024), nullable=False), sa.Column("error_message", sa.Text()), *_timestamps("created_at"))
    op.create_index("ix_evidence_embeddings_representation_id", "evidence_embeddings", ["representation_id"])
    op.create_index("ix_evidence_embeddings_text_hash", "evidence_embeddings", ["text_hash"])
    op.create_table("resource_assets", sa.Column("asset_id", sa.String(128), primary_key=True), sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id"), nullable=False), sa.Column("evidence_id", sa.String(96), sa.ForeignKey("evidence_units.evidence_id")), sa.Column("asset_type", sa.String(50), nullable=False), sa.Column("variant", sa.String(50), nullable=False), sa.Column("mime_type", sa.String(100), nullable=False), sa.Column("storage_key", sa.Text()), sa.Column("sha256", sa.String(64)), sa.Column("size_bytes", sa.Integer()), sa.Column("caption", sa.Text()), sa.Column("location", postgresql.JSONB()), sa.Column("created_from", sa.String(50)), *_timestamps("created_at", "updated_at"), sa.UniqueConstraint("paper_id", "evidence_id", "asset_type", "variant", name="uq_resource_assets_evidence_variant"))
    for name, column in (("ix_resource_assets_paper_id", "paper_id"), ("ix_resource_assets_evidence_id", "evidence_id"), ("ix_resource_assets_asset_type", "asset_type"), ("ix_resource_assets_variant", "variant")):
        op.create_index(name, "resource_assets", [column])
    op.create_table("paper_references", sa.Column("reference_id", sa.String(96), primary_key=True), sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id"), nullable=False), sa.Column("target_paper_id", sa.String(64), sa.ForeignKey("papers.paper_id")), sa.Column("order_index", sa.Integer(), nullable=False), sa.Column("raw_text", sa.Text()), sa.Column("title", sa.Text()), sa.Column("authors", postgresql.JSONB()), sa.Column("year", sa.Integer()), sa.Column("venue", sa.Text()), sa.Column("doi", sa.String(255)), sa.Column("arxiv_id", sa.String(100)), sa.Column("url", sa.Text()), sa.Column("confidence", sa.Float()), *_timestamps("created_at", "updated_at"))
    for name, column in (("ix_paper_references_paper_id", "paper_id"), ("ix_paper_references_target_paper_id", "target_paper_id"), ("ix_paper_references_doi", "doi"), ("ix_paper_references_arxiv_id", "arxiv_id")):
        op.create_index(name, "paper_references", [column])
    op.create_table("citation_contexts", sa.Column("context_id", sa.String(96), primary_key=True), sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id"), nullable=False), sa.Column("reference_id", sa.String(96), sa.ForeignKey("paper_references.reference_id")), sa.Column("evidence_id", sa.String(96), sa.ForeignKey("evidence_units.evidence_id")), sa.Column("page", sa.Integer()), sa.Column("char_start", sa.Integer()), sa.Column("char_end", sa.Integer()), sa.Column("section_title", sa.Text()), sa.Column("section_type", sa.String(50)), sa.Column("marker", sa.String(100)), sa.Column("quote", sa.Text(), nullable=False), *_timestamps("created_at"))
    for name, column in (("ix_citation_contexts_paper_id", "paper_id"), ("ix_citation_contexts_reference_id", "reference_id")):
        op.create_index(name, "citation_contexts", [column])
    op.create_table("paper_tasks", sa.Column("task_id", sa.String(64), primary_key=True), sa.Column("paper_id", sa.String(64), sa.ForeignKey("papers.paper_id")), sa.Column("operation", sa.String(50), nullable=False), sa.Column("provider", sa.String(50)), sa.Column("status", sa.String(50), nullable=False, server_default="queued"), sa.Column("progress", sa.Float(), nullable=False, server_default="0"), sa.Column("error_message", sa.Text()), sa.Column("details", postgresql.JSONB()), *_timestamps("created_at", "updated_at"), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)))
    for name, column in (("ix_paper_tasks_paper_id", "paper_id"), ("ix_paper_tasks_operation", "operation"), ("ix_paper_tasks_status", "status")):
        op.create_index(name, "paper_tasks", [column])


def downgrade() -> None:
    for table in ("paper_tasks", "citation_contexts", "paper_references", "resource_assets", "evidence_embeddings", "evidence_representations", "evidence_units", "paper_contents", "papers"):
        op.drop_table(table)
