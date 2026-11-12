import pytest

from app.core.config import Settings


def test_production_requires_all_authentication_secrets():
    settings = Settings(
        app_env="production",
        api_auth_token=None,
        admin_api_token=None,
        molyte_api_token=None,
        admin_secrets_master_key=None,
    )

    with pytest.raises(
        ValueError,
        match=(
            "API_AUTH_TOKEN, ADMIN_API_TOKEN, MOLYTE_API_TOKEN, "
            "ADMIN_SECRETS_MASTER_KEY"
        ),
    ):
        settings.validate_runtime_config()


def test_production_accepts_complete_authentication_configuration():
    settings = Settings(
        app_env="production",
        api_auth_token="api-token",
        admin_api_token="admin-token",
        molyte_api_token="molyte-token",
        admin_secrets_master_key="master-key",
    )

    settings.validate_runtime_config()
