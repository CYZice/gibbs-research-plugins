import base64
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.routes import admin
from app.core.config import ModelEndpointConfig, Settings
from app.models.operational_control import (
    ProviderCallEvent,
    ProviderConfig,
    ProviderCredential,
)
from app.services import provider_control_service
from app.services.provider_control_service import ProviderControlService, ProviderError


class RedisStub:
    def incr(self, _key):
        return 1

    def get(self, _key):
        return None


class LeaseRedisStub:
    def __init__(self):
        self.leases = {}

    def get(self, _key):
        return None

    def eval(self, _script, _keys, key, now, expires, limit, token):
        values = self.leases.setdefault(key, {})
        values = {
            item: expiry
            for item, expiry in values.items()
            if int(expiry) > int(now)
        }
        self.leases[key] = values
        if len(values) >= int(limit):
            return 0
        values[token] = int(expires)
        return 1

    def zrem(self, key, token):
        self.leases.setdefault(key, {}).pop(token, None)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    for table in (
        ProviderConfig.__table__,
        ProviderCredential.__table__,
        ProviderCallEvent.__table__,
    ):
        table.create(engine)
    with Session(engine) as session:
        yield session


def settings(master_key: bool = True) -> Settings:
    key = base64.b64encode(b"k" * 32).decode() if master_key else None
    return Settings(app_env="test", admin_secrets_master_key=key)


def test_provider_channel_routes_are_removed():
    source = (Path(__file__).parents[1] / "app" / "api" / "routes" / "admin.py").read_text(
        encoding="utf-8"
    )

    assert "/provider-channels" not in source


def test_provider_credentials_require_a_role_config():
    assert ProviderCredential.__table__.c.provider_config_id.nullable is False


def test_unconfigured_roles_never_report_or_read_environment_keys(db, monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-retired-environment-key")
    monkeypatch.setattr(admin, "_connection", RedisStub)

    provider = next(item for item in admin.providers(db)["items"] if item["role"] == "embedding")
    keys = admin.provider_keys("embedding", db)

    assert provider["source"] == "unconfigured"
    assert provider["key_mode"] == "none"
    assert provider["key_count"] == 0
    assert "env_key_configured" not in provider
    assert keys == {"items": [], "source": "unconfigured"}


def test_resolve_endpoint_requires_a_database_configuration(monkeypatch):
    class EmptyDb:
        def scalar(self, _query):
            return None

        def close(self):
            pass

    class RedisStub:
        pass

    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-retired-environment-key")
    monkeypatch.setattr(provider_control_service, "SessionLocal", EmptyDb)
    monkeypatch.setattr(provider_control_service.redis.Redis, "from_url", lambda _url: RedisStub())
    service = ProviderControlService(Settings(app_env="test"))

    with pytest.raises(ProviderError) as exc:
        service.resolve_endpoint("embedding")

    assert exc.value.error_code == "PROVIDER_CONFIGURATION_REQUIRED"
    assert exc.value.retryable is False


def test_resolve_config_reads_model_without_requiring_keys(db, monkeypatch):
    db.add(
        ProviderConfig(
            provider_config_id="provider_embedding",
            role="embedding",
            provider="dashscope",
            base_url="https://example.test/v1",
            model="admin-embed-model",
            timeout_seconds=60,
            breaker_threshold=5,
            breaker_cooldown_seconds=60,
        )
    )
    db.commit()

    monkeypatch.setattr(provider_control_service, "SessionLocal", lambda: db)
    monkeypatch.setattr(provider_control_service.redis.Redis, "from_url", lambda _url: RedisStub())
    service = ProviderControlService(Settings(app_env="test"))

    config = service.resolve_config("embedding")

    assert config.model == "admin-embed-model"
    assert config.api_key is None


def test_same_key_can_be_added_to_independent_role_pools(db, monkeypatch):
    monkeypatch.setattr(admin, "get_settings", settings)
    monkeypatch.setattr(admin, "_connection", RedisStub)
    monkeypatch.setattr(admin, "_audit", lambda *_args, **_kwargs: None)
    request = SimpleNamespace(headers={})
    body = admin.ProviderPatch(
        provider="dashscope", base_url="https://example.test/v1", model="model", timeout_seconds=60,
        breaker_threshold=5, breaker_cooldown_seconds=60,
    )
    admin.update_provider("embedding", body, request, db)
    admin.update_provider("rerank", body, request, db)

    first = admin.add_provider_key("embedding", admin.CredentialCreate(alias="primary", api_key="sk-shared-key"), request, db)
    second = admin.add_provider_key("rerank", admin.CredentialCreate(alias="primary", api_key="sk-shared-key"), request, db)

    assert first["key_suffix"] == second["key_suffix"]


def test_provider_setup_creates_config_and_multiple_keys_atomically(db, monkeypatch):
    monkeypatch.setattr(admin, "get_settings", settings)
    monkeypatch.setattr(admin, "_connection", RedisStub)
    monkeypatch.setattr(admin, "_audit", lambda *_args, **_kwargs: None)
    body = admin.ProviderSetup(
        provider="dashscope",
        base_url="https://example.test/v1",
        model="embed-model",
        timeout_seconds=60,
        keys=[
            admin.CredentialCreate(
                alias="primary",
                api_key="sk-primary-key",
                max_concurrency=5,
            ),
            admin.CredentialCreate(
                alias="backup",
                api_key="sk-backup-key",
                max_concurrency=5,
            ),
        ],
    )

    result = admin.setup_provider("embedding", body, SimpleNamespace(headers={}), db)

    config = db.scalar(select(ProviderConfig).where(ProviderConfig.role == "embedding"))
    credentials = db.scalars(
        select(ProviderCredential).where(ProviderCredential.provider_config_id == config.provider_config_id)
    ).all()
    assert result["key_count_added"] == 2
    assert {credential.alias for credential in credentials} == {"primary", "backup"}
    assert sum(credential.max_concurrency for credential in credentials) == 10
    assert all("sk-" not in credential.encrypted_api_key for credential in credentials)


def test_manual_key_probe_updates_health_state(db, monkeypatch):
    monkeypatch.setattr(admin, "get_settings", settings)
    monkeypatch.setattr(admin, "_connection", RedisStub)
    monkeypatch.setattr(admin, "_audit", lambda *_args, **_kwargs: None)
    request = SimpleNamespace(headers={})
    body = admin.ProviderPatch(
        provider="dashscope", base_url="https://example.test/v1", model="model", timeout_seconds=60,
        breaker_threshold=5, breaker_cooldown_seconds=60,
    )
    admin.update_provider("embedding", body, request, db)
    key = admin.add_provider_key("embedding", admin.CredentialCreate(alias="primary", api_key="sk-health-key"), request, db)

    monkeypatch.setattr(admin, "_provider_probe", lambda *_args: (SimpleNamespace(status_code=200), "embedding"))
    result = admin.test_provider_key(key["credential_id"], db)
    assert result["healthy"] is True
    assert result["credential"]["health"] == "healthy"

    monkeypatch.setattr(admin, "_provider_probe", lambda *_args: (SimpleNamespace(status_code=401), "embedding"))
    result = admin.test_provider_key(key["credential_id"], db)
    assert result["healthy"] is False
    assert result["credential"]["health"] == "disabled"


def test_key_capacity_is_independent_and_capacity_race_tries_next_key(
    monkeypatch,
):
    redis_stub = LeaseRedisStub()
    monkeypatch.setattr(
        provider_control_service.redis.Redis,
        "from_url",
        lambda _url: redis_stub,
    )
    service = ProviderControlService(Settings(app_env="test"))
    first = ModelEndpointConfig(
        role="embedding",
        provider="dashscope",
        model="embed",
        credential_id="key_1",
        max_concurrency=5,
    )
    second = first.model_copy(
        update={"credential_id": "key_2", "max_concurrency": 5}
    )

    with ExitStack() as stack:
        for _ in range(5):
            stack.enter_context(service.lease(first))
        for _ in range(5):
            stack.enter_context(service.lease(second))
        with pytest.raises(ProviderError) as first_full:
            stack.enter_context(service.lease(first))
        assert first_full.value.error_code == "PROVIDER_CAPACITY_WAIT"

    with ExitStack() as stack:
        for _ in range(5):
            stack.enter_context(service.lease(first))
        monkeypatch.setattr(
            service,
            "resolve_endpoint",
            lambda role, exclude=None: second,
        )
        with service.lease_endpoint(
            "embedding", preferred=first
        ) as selected:
            assert selected.credential_id == "key_2"
