import os

import pytest

# This is set while pytest imports the app; runtime configurations never permit fake providers.
os.environ.setdefault("APP_ENV", "test")
# Keep developer .env authentication settings from changing unauthenticated API contracts in tests.
os.environ.setdefault("API_AUTH_TOKEN", "")


@pytest.fixture(autouse=True)
def isolate_model_env(monkeypatch):
    for name in (
        "EMBEDDING_PROVIDER",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSION",
        "STRUCTURED_PROVIDER",
        "STRUCTURED_BASE_URL",
        "STRUCTURED_MODEL",
        "STRUCTURED_TIMEOUT_SECONDS",
        "STRUCTURED_STREAM",
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_STREAM",
        "VISION_PROVIDER",
        "VISION_BASE_URL",
        "VISION_MODEL",
        "RERANK_PROVIDER",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
        "RERANK_ENABLED",
        "RERANK_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
