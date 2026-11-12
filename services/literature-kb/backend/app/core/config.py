from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelEndpointConfig(BaseModel):
    role: str
    provider: str = "fake"
    base_url: str | None = None
    api_key: str | None = None
    model: str
    enabled: bool = True
    timeout_seconds: int = 60
    credential_id: str | None = None
    # Selected credential capacity. ProviderConfig has no global concurrency cap.
    max_concurrency: int = 5
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    breaker_threshold: int = 5
    breaker_cooldown_seconds: int = 60


class Settings(BaseSettings):
    # Model API keys are deliberately not settings.  Retain ``extra=ignore`` so
    # existing local .env files with retired *_API_KEY entries keep working but
    # cannot affect runtime provider authentication.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Literature KB"
    app_version: str = "0.1.0"
    api_auth_token: str | None = None
    admin_api_token: str | None = None
    molyte_api_token: str | None = None
    admin_secrets_master_key: str | None = None
    max_upload_bytes: int = 50 * 1024 * 1024
    database_url: str = Field(
        default="postgresql+psycopg://literature:literature@localhost:5432/literature_kb"
    )
    redis_url: str = "redis://localhost:6379/0"
    storage_root: str = "../storage"
    discovery_download_root: str = "../storage/downloads"
    discovery_storage_provider: str = "local"
    discovery_max_download_bytes: int = 50 * 1024 * 1024
    discovery_request_timeout_seconds: int = 30
    discovery_search_timeout_seconds: float = 8.0
    discovery_cache_fresh_seconds: int = 300
    discovery_cache_stale_seconds: int = 86400
    literature_search_session_seconds: int = 900
    discovery_user_agent: str = "Literature-KB/0.1 (external literature discovery)"
    discovery_allow_fake_ip_dns: bool = False
    discovery_contact_email: str | None = None
    openalex_api_key: str | None = None
    core_api_key: str | None = None
    unpaywall_email: str | None = None
    cos_bucket: str | None = None
    cos_region: str | None = None
    cos_endpoint: str | None = None
    cos_secret_id: str | None = None
    cos_secret_key: str | None = None
    cos_prefix: str = "literature-kb"
    mineru_api_token: str | None = None
    mineru_base_url: str = "https://mineru.net"
    mineru_model_version: str = "vlm"
    mineru_language: str = "ch"
    mineru_enable_table: bool = True
    mineru_enable_formula: bool = True
    mineru_is_ocr: bool = False

    app_env: str = "development"
    embedding_provider: str = "dashscope"
    embedding_base_url: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1024
    embedding_timeout_seconds: int = 60
    paper_search_semantic_timeout_seconds: int = 1
    model_retry_max_attempts: int = 2
    workflow_job_timeout_seconds: int = 900
    ingest_job_timeout_seconds: int = 7200

    structured_provider: str = "dashscope"
    structured_base_url: str | None = None
    structured_model: str = "qwen3.7-plus"
    structured_timeout_seconds: int = 180
    structured_stream: bool = True

    llm_provider: str = "dashscope"
    llm_base_url: str | None = None
    llm_model: str = "qwen3.7-plus"
    llm_timeout_seconds: int = 180
    llm_stream: bool = True

    vision_provider: str = "dashscope"
    vision_base_url: str | None = None
    vision_model: str = "gemini-3.5-flash"

    rerank_provider: str = "dashscope"
    rerank_base_url: str | None = None
    rerank_model: str = "qwen3-rerank"
    rerank_enabled: bool = False
    rerank_timeout_seconds: int = 60
    embedding_batch_size: int = 32
    embedding_max_input_chars: int = Field(default=12000, ge=1000, le=100000)
    embedding_batch_max_chars: int = Field(default=24000, ge=1000, le=500000)
    batch_active_window: int = 10
    task_orphan_grace_seconds: int = 300
    provider_event_retention_days: int = 30
    pipeline_enabled: bool = False
    pipeline_expand_chunk_size: int = Field(default=500, ge=1, le=5000)
    pipeline_dispatch_interval_seconds: int = Field(default=2, ge=1, le=60)
    pipeline_delivery_lease_seconds: int = Field(default=60, ge=10, le=600)
    pipeline_redis_buffer_multiplier: int = Field(default=3, ge=1, le=10)
    pipeline_overdue_seconds: int = Field(default=900, ge=60)
    harvest_enabled: bool = False
    harvest_expand_buffer_items: int = Field(default=32, ge=8, le=10000)
    harvest_dispatch_interval_seconds: int = Field(default=10, ge=2, le=300)
    harvest_max_attempts: int = Field(default=3, ge=1, le=10)
    harvest_cli_enabled: bool = True
    harvest_cli_workers: int = Field(default=20, ge=1, le=200)
    harvest_allow_openalex_content: bool = False

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        """Keep only the local administrator credential deterministic.

        A developer shell can retain an ``ADMIN_API_TOKEN`` from an older
        launch.  Values explicitly recorded in ``backend/.env`` take
        precedence for that one setting, while every other setting retains the
        standard process-environment-over-dotenv precedence.
        """
        def dotenv_admin_token():
            values = dotenv_settings()
            return (
                {"admin_api_token": values["admin_api_token"]}
                if "admin_api_token" in values
                else {}
            )

        return (
            init_settings,
            dotenv_admin_token,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    @property
    def embedding_endpoint(self) -> ModelEndpointConfig:
        return ModelEndpointConfig(
            role="embedding",
            provider=self.embedding_provider,
            base_url=self.embedding_base_url,
            model=self.embedding_model,
            timeout_seconds=self.embedding_timeout_seconds,
        )

    @property
    def structured_endpoint(self) -> ModelEndpointConfig:
        return ModelEndpointConfig(
            role="structured",
            provider=self.structured_provider,
            base_url=self.structured_base_url,
            model=self.structured_model,
            timeout_seconds=self.structured_timeout_seconds,
        )

    @property
    def llm_endpoint(self) -> ModelEndpointConfig:
        return ModelEndpointConfig(
            role="llm",
            provider=self.llm_provider,
            base_url=self.llm_base_url,
            model=self.llm_model,
            timeout_seconds=self.llm_timeout_seconds,
        )

    @property
    def vision_endpoint(self) -> ModelEndpointConfig:
        return ModelEndpointConfig(
            role="vision",
            provider=self.vision_provider,
            base_url=self.vision_base_url,
            model=self.vision_model,
            timeout_seconds=self.llm_timeout_seconds,
        )

    @property
    def rerank_endpoint(self) -> ModelEndpointConfig:
        return ModelEndpointConfig(
            role="rerank",
            provider=self.rerank_provider,
            base_url=self.rerank_base_url,
            model=self.rerank_model,
            enabled=self.rerank_enabled,
            timeout_seconds=self.rerank_timeout_seconds,
        )

    @property
    def model_endpoints(self) -> list[ModelEndpointConfig]:
        return [
            self.embedding_endpoint,
            self.structured_endpoint,
            self.llm_endpoint,
            self.vision_endpoint,
            self.rerank_endpoint,
        ]

    def validate_runtime_config(self) -> None:
        """Validate static runtime settings; provider credentials live in the database."""
        if self.embedding_dimension != 1024:
            raise ValueError("EMBEDDING_DIMENSION must be 1024 for the current pgvector schema.")
        if self.app_env == "production":
            missing = [
                name
                for name, value in (
                    ("API_AUTH_TOKEN", self.api_auth_token),
                    ("ADMIN_API_TOKEN", self.admin_api_token),
                    ("MOLYTE_API_TOKEN", self.molyte_api_token),
                    ("ADMIN_SECRETS_MASTER_KEY", self.admin_secrets_master_key),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"Production requires: {', '.join(missing)}."
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()
