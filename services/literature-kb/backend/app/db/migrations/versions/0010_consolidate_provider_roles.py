"""Consolidate legacy provider channels into role-specific configurations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_consolidate_provider_roles"
down_revision: str | Sequence[str] | None = "0009_task_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    bindings = connection.execute(
        sa.text(
            """
            SELECT binding.*, channel.provider, channel.base_url,
                   channel.enabled AS channel_enabled
            FROM provider_role_bindings AS binding
            JOIN provider_channels AS channel ON channel.channel_id = binding.channel_id
            ORDER BY binding.role
            """
        )
    ).mappings().all()
    for binding in bindings:
        config = connection.execute(
            sa.text("SELECT * FROM provider_configs WHERE role=:role"),
            {"role": binding["role"]},
        ).mappings().first()
        config_id = config["provider_config_id"] if config else f"provider_{binding['binding_id']}"
        if config is None:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO provider_configs (
                        provider_config_id, role, provider, base_url, model, enabled,
                        timeout_seconds, max_concurrency, rpm_limit, tpm_limit,
                        breaker_threshold, breaker_cooldown_seconds, config_version
                    ) VALUES (
                        :config_id, :role, :provider, :base_url, :model, :enabled,
                        :timeout_seconds, :max_concurrency, :rpm_limit, :tpm_limit,
                        :breaker_threshold, :breaker_cooldown_seconds, 1
                    )
                    """
                ),
                {
                    "config_id": config_id,
                    "role": binding["role"],
                    "provider": binding["provider"],
                    "base_url": binding["base_url"],
                    "model": binding["model"],
                    "enabled": bool(binding["channel_enabled"] and binding["enabled"]),
                    "timeout_seconds": binding["timeout_seconds"],
                    "max_concurrency": binding["max_concurrency"],
                    "rpm_limit": binding["rpm_limit"],
                    "tpm_limit": binding["tpm_limit"],
                    "breaker_threshold": binding["breaker_threshold"],
                    "breaker_cooldown_seconds": binding["breaker_cooldown_seconds"],
                },
            )
        credentials = connection.execute(
            sa.text("SELECT * FROM provider_credentials WHERE channel_id=:channel_id"),
            {"channel_id": binding["channel_id"]},
        ).mappings().all()
        existing = connection.execute(
            sa.text(
                "SELECT alias, key_fingerprint FROM provider_credentials "
                "WHERE provider_config_id=:config_id"
            ),
            {"config_id": config_id},
        ).mappings().all()
        aliases = {row["alias"] for row in existing}
        fingerprints = {row["key_fingerprint"] for row in existing}
        for credential in credentials:
            if credential["key_fingerprint"] in fingerprints:
                continue
            alias = credential["alias"]
            if alias in aliases:
                base = f"migrated-{binding['channel_id'][-12:]}-{alias}"
                alias = base[:100]
                suffix = 2
                while alias in aliases:
                    alias = f"{base[:96]}-{suffix}"
                    suffix += 1
            connection.execute(
                sa.text(
                    """
                    INSERT INTO provider_credentials (
                        credential_id, provider_config_id, alias, encrypted_api_key,
                        key_suffix, key_fingerprint, status, weight, cooldown_until,
                        consecutive_failures, last_error_code, last_success_at, last_failure_at
                    ) VALUES (
                        :credential_id, :config_id, :alias, :encrypted_api_key,
                        :key_suffix, :key_fingerprint, :status, :weight, :cooldown_until,
                        :consecutive_failures, :last_error_code, :last_success_at, :last_failure_at
                    )
                    """
                ),
                {
                    "credential_id": (
                        f"credential_{binding['role']}_{credential['credential_id'][-32:]}"
                    ),
                    "config_id": config_id,
                    "alias": alias,
                    "encrypted_api_key": credential["encrypted_api_key"],
                    "key_suffix": credential["key_suffix"],
                    "key_fingerprint": credential["key_fingerprint"],
                    "status": credential["status"],
                    "weight": credential["weight"],
                    "cooldown_until": credential["cooldown_until"],
                    "consecutive_failures": credential["consecutive_failures"],
                    "last_error_code": credential["last_error_code"],
                    "last_success_at": credential["last_success_at"],
                    "last_failure_at": credential["last_failure_at"],
                },
            )
            aliases.add(alias)
            fingerprints.add(credential["key_fingerprint"])

    connection.execute(sa.text("DELETE FROM provider_credentials WHERE channel_id IS NOT NULL"))
    connection.execute(sa.text("DELETE FROM provider_credentials WHERE provider_config_id IS NULL"))
    op.drop_constraint(
        "uq_provider_credential_channel_alias", "provider_credentials", type_="unique"
    )
    op.drop_constraint(
        "fk_provider_credentials_channel", "provider_credentials", type_="foreignkey"
    )
    op.drop_index("ix_provider_credentials_channel", table_name="provider_credentials")
    op.drop_column("provider_credentials", "channel_id")
    op.alter_column("provider_credentials", "provider_config_id", nullable=False)
    op.drop_table("provider_role_bindings")
    op.drop_table("provider_channels")


def downgrade() -> None:
    raise NotImplementedError("Provider channel consolidation is intentionally irreversible.")
