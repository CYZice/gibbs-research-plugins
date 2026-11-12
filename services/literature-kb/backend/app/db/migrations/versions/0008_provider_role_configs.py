"""Migrate shared provider channels to per-role provider configs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_provider_role_configs"
down_revision: str | Sequence[str] | None = "0007_provider_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.drop_constraint(
            "provider_credentials_key_fingerprint_key",
            "provider_credentials",
            type_="unique",
        )
    bindings = connection.execute(
        sa.text(
            """
            SELECT binding.*, channel.provider, channel.base_url, channel.enabled AS channel_enabled
            FROM provider_role_bindings AS binding
            JOIN provider_channels AS channel ON channel.channel_id = binding.channel_id
            ORDER BY binding.role
            """
        )
    ).mappings().all()
    credentials_by_channel = {
        channel_id: connection.execute(
            sa.text("SELECT * FROM provider_credentials WHERE channel_id=:channel_id"),
            {"channel_id": channel_id},
        ).mappings().all()
        for channel_id in {binding["channel_id"] for binding in bindings}
    }
    connection.execute(sa.text("DELETE FROM provider_credentials WHERE channel_id IS NOT NULL"))
    for binding in bindings:
        config = connection.execute(
            sa.text("SELECT provider_config_id FROM provider_configs WHERE role=:role"),
            {"role": binding["role"]},
        ).mappings().first()
        config_id = config["provider_config_id"] if config else f"provider_{binding['binding_id']}"
        values = {
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
        }
        if config:
            connection.execute(
                sa.text(
                    """
                    UPDATE provider_configs SET provider=:provider, base_url=:base_url, model=:model,
                    enabled=:enabled, timeout_seconds=:timeout_seconds, max_concurrency=:max_concurrency,
                    rpm_limit=:rpm_limit, tpm_limit=:tpm_limit, breaker_threshold=:breaker_threshold,
                    breaker_cooldown_seconds=:breaker_cooldown_seconds, config_version=config_version + 1
                    WHERE provider_config_id=:config_id
                    """
                ),
                {**values, "config_id": config_id},
            )
        else:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO provider_configs (
                        provider_config_id, role, provider, base_url, model, enabled, timeout_seconds,
                        max_concurrency, rpm_limit, tpm_limit, breaker_threshold, breaker_cooldown_seconds,
                        config_version
                    ) VALUES (
                        :config_id, :role, :provider, :base_url, :model, :enabled, :timeout_seconds,
                        :max_concurrency, :rpm_limit, :tpm_limit, :breaker_threshold,
                        :breaker_cooldown_seconds, 1
                    )
                    """
                ),
                {**values, "config_id": config_id, "role": binding["role"]},
            )
        for index, credential in enumerate(credentials_by_channel[binding["channel_id"]]):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO provider_credentials (
                        credential_id, provider_config_id, channel_id, alias, encrypted_api_key, key_suffix,
                        key_fingerprint, status, weight, cooldown_until, consecutive_failures,
                        last_error_code, last_success_at, last_failure_at
                    ) VALUES (
                        :credential_id, :config_id, NULL, :alias, :encrypted_api_key, :key_suffix,
                        :key_fingerprint, :status, :weight, :cooldown_until, :consecutive_failures,
                        :last_error_code, :last_success_at, :last_failure_at
                    )
                    """
                ),
                {
                    **credential,
                    "credential_id": f"credential_{binding['role']}_{index}_{credential['credential_id'][-32:]}",
                    "config_id": config_id,
                },
            )


def downgrade() -> None:
    raise NotImplementedError("Provider role config migration is intentionally irreversible.")
