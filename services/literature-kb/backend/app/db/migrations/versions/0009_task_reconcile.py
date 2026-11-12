"""Mark legacy stale process tasks as historical audit records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_task_reconcile"
down_revision: str | Sequence[str] | None = "0008_provider_role_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                UPDATE paper_tasks
                SET status = 'superseded',
                    details = COALESCE(details, '{}'::jsonb)
                        || '{"stage":"superseded","superseded_reason":"legacy_stale_task"}'::jsonb
                WHERE operation = 'process' AND status = 'stale'
                """
            )
        )
    else:
        connection.execute(
            sa.text(
                "UPDATE paper_tasks SET status = 'superseded' "
                "WHERE operation = 'process' AND status = 'stale'"
            )
        )


def downgrade() -> None:
    raise NotImplementedError("Historical task reconciliation is intentionally irreversible.")
