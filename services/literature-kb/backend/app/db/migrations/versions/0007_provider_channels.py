"""Add shared provider channels and role bindings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_provider_channels"
down_revision: str | Sequence[str] | None = "0006_operational_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_channels",
        sa.Column("channel_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "provider_role_bindings",
        sa.Column("binding_id", sa.String(64), primary_key=True),
        sa.Column(
            "channel_id",
            sa.String(64),
            sa.ForeignKey("provider_channels.channel_id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(32), nullable=False, unique=True),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rpm_limit", sa.Integer()),
        sa.Column("tpm_limit", sa.Integer()),
        sa.Column("breaker_threshold", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("breaker_cooldown_seconds", sa.Integer(), nullable=False, server_default="60"),
    )
    op.create_index("ix_provider_role_bindings_channel", "provider_role_bindings", ["channel_id"])
    op.create_index(
        "ix_provider_role_bindings_role", "provider_role_bindings", ["role"], unique=True
    )
    op.add_column("provider_credentials", sa.Column("channel_id", sa.String(64)))
    op.create_foreign_key(
        "fk_provider_credentials_channel",
        "provider_credentials",
        "provider_channels",
        ["channel_id"],
        ["channel_id"],
    )
    op.create_index("ix_provider_credentials_channel", "provider_credentials", ["channel_id"])
    op.alter_column("provider_credentials", "provider_config_id", nullable=True)

    connection = op.get_bind()
    configs = connection.execute(sa.text("SELECT * FROM provider_configs")).mappings().all()
    for config in configs:
        channel_id = f"channel_{config['provider_config_id']}"
        connection.execute(
            sa.text("""
            INSERT INTO provider_channels (
                channel_id, name, provider, base_url, enabled, config_version
            )
            VALUES (:channel_id, :name, :provider, :base_url, :enabled, :version)
        """),
            {
                "channel_id": channel_id,
                "name": f"Legacy {config['role']}",
                "provider": config["provider"],
                "base_url": config["base_url"],
                "enabled": config["enabled"],
                "version": config["config_version"],
            },
        )
        connection.execute(
            sa.text("""
            INSERT INTO provider_role_bindings (
                binding_id, channel_id, role, model, enabled, timeout_seconds,
                max_concurrency, rpm_limit, tpm_limit, breaker_threshold,
                breaker_cooldown_seconds
            ) VALUES (
                :binding_id, :channel_id, :role, :model, :enabled, :timeout,
                :concurrency, :rpm, :tpm, :threshold, :cooldown
            )
        """),
            {
                "binding_id": f"binding_{config['provider_config_id']}",
                "channel_id": channel_id,
                "role": config["role"],
                "model": config["model"],
                "enabled": config["enabled"],
                "timeout": config["timeout_seconds"],
                "concurrency": config["max_concurrency"],
                "rpm": config["rpm_limit"],
                "tpm": config["tpm_limit"],
                "threshold": config["breaker_threshold"],
                "cooldown": config["breaker_cooldown_seconds"],
            },
        )
        connection.execute(
            sa.text(
                "UPDATE provider_credentials SET channel_id=:channel_id "
                "WHERE provider_config_id=:config_id"
            ),
            {"channel_id": channel_id, "config_id": config["provider_config_id"]},
        )
    op.create_unique_constraint(
        "uq_provider_credential_channel_alias", "provider_credentials", ["channel_id", "alias"]
    )


def downgrade() -> None:
    op.execute("DELETE FROM provider_credentials WHERE provider_config_id IS NULL")
    op.alter_column("provider_credentials", "provider_config_id", nullable=False)
    op.drop_constraint(
        "uq_provider_credential_channel_alias", "provider_credentials", type_="unique"
    )
    op.drop_index("ix_provider_credentials_channel", table_name="provider_credentials")
    op.drop_constraint(
        "fk_provider_credentials_channel", "provider_credentials", type_="foreignkey"
    )
    op.drop_column("provider_credentials", "channel_id")
    op.drop_table("provider_role_bindings")
    op.drop_table("provider_channels")
