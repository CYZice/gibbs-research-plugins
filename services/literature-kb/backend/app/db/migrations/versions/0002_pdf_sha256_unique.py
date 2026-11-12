"""Make uploaded PDF hashes unique."""

from collections.abc import Sequence

from alembic import op


revision: str = "0002_pdf_sha256_unique"
down_revision: str | None = "0001_v1_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        WITH ranked AS (
            SELECT paper_id, row_number() OVER (PARTITION BY pdf_sha256 ORDER BY created_at, paper_id) AS row_num
            FROM papers WHERE pdf_sha256 IS NOT NULL
        ), duplicates AS (SELECT paper_id FROM ranked WHERE row_num > 1)
        DELETE FROM citation_contexts
        WHERE paper_id IN (SELECT paper_id FROM duplicates)
           OR evidence_id IN (SELECT evidence_id FROM evidence_units WHERE paper_id IN (SELECT paper_id FROM duplicates))
    """)
    op.execute("""
        WITH ranked AS (
            SELECT paper_id, row_number() OVER (PARTITION BY pdf_sha256 ORDER BY created_at, paper_id) AS row_num
            FROM papers WHERE pdf_sha256 IS NOT NULL
        ), duplicates AS (SELECT paper_id FROM ranked WHERE row_num > 1)
        UPDATE paper_references SET target_paper_id = NULL WHERE target_paper_id IN (SELECT paper_id FROM duplicates)
    """)
    for table, column in (("evidence_embeddings", "representation_id"), ("evidence_representations", "evidence_id")):
        source = "SELECT representation_id FROM evidence_representations WHERE evidence_id IN (SELECT evidence_id FROM evidence_units WHERE paper_id IN (SELECT paper_id FROM duplicates))" if table == "evidence_embeddings" else "SELECT evidence_id FROM evidence_units WHERE paper_id IN (SELECT paper_id FROM duplicates)"
        op.execute(f"""
            WITH ranked AS (
                SELECT paper_id, row_number() OVER (PARTITION BY pdf_sha256 ORDER BY created_at, paper_id) AS row_num
                FROM papers WHERE pdf_sha256 IS NOT NULL
            ), duplicates AS (SELECT paper_id FROM ranked WHERE row_num > 1)
            DELETE FROM {table} WHERE {column} IN ({source})
        """)
    for table in ("resource_assets", "paper_contents", "paper_references", "paper_tasks", "evidence_units", "papers"):
        op.execute(f"""
            WITH ranked AS (
                SELECT paper_id, row_number() OVER (PARTITION BY pdf_sha256 ORDER BY created_at, paper_id) AS row_num
                FROM papers WHERE pdf_sha256 IS NOT NULL
            ), duplicates AS (SELECT paper_id FROM ranked WHERE row_num > 1)
            DELETE FROM {table} WHERE paper_id IN (SELECT paper_id FROM duplicates)
        """)
    op.create_unique_constraint("uq_papers_pdf_sha256", "papers", ["pdf_sha256"])


def downgrade() -> None:
    op.drop_constraint("uq_papers_pdf_sha256", "papers", type_="unique")
