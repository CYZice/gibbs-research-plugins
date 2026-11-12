"""Add access grants for shared and user-scoped papers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_paper_access_grants"
down_revision: str | Sequence[str] | None = "0015_import_pipeline_v3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_access_grants",
        sa.Column("grant_id", sa.Integer(), primary_key=True),
        sa.Column(
            "paper_id",
            sa.String(64),
            sa.ForeignKey("papers.paper_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope_key", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "paper_id",
            "scope_key",
            name="uq_paper_access_grants_paper_scope",
        ),
    )
    op.create_index(
        "ix_paper_access_grants_paper_id",
        "paper_access_grants",
        ["paper_id"],
    )
    op.create_index(
        "ix_paper_access_grants_scope_key",
        "paper_access_grants",
        ["scope_key"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO paper_access_grants (paper_id, scope_key)
            SELECT paper_id, 'shared' FROM papers
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paper_access_grants_scope_key",
        table_name="paper_access_grants",
    )
    op.drop_index(
        "ix_paper_access_grants_paper_id",
        table_name="paper_access_grants",
    )
    op.drop_table("paper_access_grants")
