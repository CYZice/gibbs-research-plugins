"""Index evidence representation text for lexical retrieval."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_evidence_fulltext_index"
down_revision: str | Sequence[str] | None = "0004_metasearch_and_batch_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidence_representations",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            nullable=False,
            server_default=sa.text("''::tsvector"),
        ),
    )
    op.execute(
        "UPDATE evidence_representations "
        "SET search_vector = to_tsvector('simple', text)"
    )
    op.execute("""
        CREATE FUNCTION evidence_representation_search_vector_update() RETURNS trigger AS $$
        BEGIN
          NEW.search_vector := to_tsvector('simple', NEW.text);
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER evidence_representation_search_vector_trigger
        BEFORE INSERT OR UPDATE OF text ON evidence_representations
        FOR EACH ROW EXECUTE FUNCTION evidence_representation_search_vector_update()
    """)
    op.create_index(
        "ix_evidence_representations_search_vector",
        "evidence_representations",
        ["search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_representations_search_vector", table_name="evidence_representations")
    op.execute("DROP TRIGGER evidence_representation_search_vector_trigger ON evidence_representations")
    op.execute("DROP FUNCTION evidence_representation_search_vector_update()")
    op.drop_column("evidence_representations", "search_vector")
