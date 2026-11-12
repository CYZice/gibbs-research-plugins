"""Bind durable workflow runs to their literature access scope."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_workflow_scope_binding"
down_revision: str | Sequence[str] | None = "0016_paper_access_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("access_scope", sa.String(255)))
    op.add_column("workflow_runs", sa.Column("scope_digest", sa.String(64)))
    op.create_index(
        "ix_workflow_runs_access_scope",
        "workflow_runs",
        ["access_scope"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_runs_access_scope", table_name="workflow_runs")
    op.drop_column("workflow_runs", "scope_digest")
    op.drop_column("workflow_runs", "access_scope")
