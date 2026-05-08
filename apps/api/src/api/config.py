from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]
PRODUCTION_PROVIDER_WRITE_ACK = "I_UNDERSTAND_THIS_CAN_MUTATE_PRODUCTION_PROVIDERS"


class Settings(BaseSettings):
    environment: str = "local"
    api_base_url: str = "http://localhost:8000"
    console_base_url: str = "http://localhost:3000"
    database_url: str = "postgresql://ecom_agent:ecom_agent@localhost:5432/ecom_agent"
    redis_url: str = "redis://localhost:6379/0"
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "ecom-agent-dev"
    online_eval_webhook_secret: str | None = None
    langgraph_studio_url: str = "http://localhost:2024"
    anthropic_api_key: str | None = None
    doppler_project: str | None = None
    doppler_config: str | None = None
    clerk_secret_key: str | None = None
    clerk_issuer: str | None = None
    clerk_jwks_url: str | None = None
    clerk_audience: str | None = None
    clerk_allow_unverified_jwt: bool = False
    clerk_dev_jwt_secret: str | None = None
    stripe_secret_key: str | None = None
    stripe_connect_client_id: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_account_id: str | None = None
    shopify_client_id: str | None = None
    shopify_client_secret: str | None = None
    shopify_admin_api_version: str = "2025-10"
    shopify_oauth_scopes: str = (
        "read_orders,write_orders,read_customers,read_fulfillments,write_fulfillments,"
        "read_order_edits,write_order_edits"
    )
    shopify_webhook_secret: str | None = None
    shopify_previous_webhook_secret: str | None = None
    gorgias_client_id: str | None = None
    gorgias_client_secret: str | None = None
    gorgias_oauth_scopes: str = (
        "openid email profile offline tickets:read tickets:write customers:read "
        "integrations:read integrations:write"
    )
    gorgias_webhook_secret: str | None = None
    gorgias_previous_webhook_secret: str | None = None
    shipbob_webhook_secret: str | None = None
    shipstation_webhook_secret: str | None = None
    gmail_webhook_secret: str | None = None
    stripe_previous_webhook_secret: str | None = None
    langgraph_run_webhook_secret: str | None = None
    langgraph_assistant_id: str = "order-exception"
    integration_http_timeout_seconds: float = 30.0
    provider_webhook_registration_mode: str = "record_only"
    synthetic_execution_mode: str = "skip"
    enable_production_provider_writes: bool = False
    global_provider_write_disable: bool = False
    disabled_provider_writes: str = ""
    disabled_tools: str = ""
    production_provider_write_allowlist: str = ""
    production_provider_write_ack: str | None = None
    app_kms_provider: str = "local"
    app_kms_key_id: str = "local-dev-cmk"
    managed_kms_key_id: str | None = None
    local_kms_master_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_environment_safety(self) -> Settings:
        if self.synthetic_execution_mode not in {"skip", "real_provider"}:
            msg = "SYNTHETIC_EXECUTION_MODE must be one of: skip, real_provider."
            raise ValueError(msg)
        if self.provider_webhook_registration_mode not in {"record_only", "live"}:
            msg = "PROVIDER_WEBHOOK_REGISTRATION_MODE must be one of: record_only, live."
            raise ValueError(msg)
        if self.app_kms_provider not in {"local", "managed"}:
            msg = "APP_KMS_PROVIDER must be one of: local, managed."
            raise ValueError(msg)
        if (
            self.enable_production_provider_writes
            and self.production_provider_write_ack != PRODUCTION_PROVIDER_WRITE_ACK
        ):
            msg = (
                "PRODUCTION_PROVIDER_WRITE_ACK must be "
                f"{PRODUCTION_PROVIDER_WRITE_ACK!r} when production provider writes are enabled."
            )
            raise ValueError(msg)
        if self.environment not in {"development", "local", "test"}:
            if self.clerk_allow_unverified_jwt:
                msg = "CLERK_ALLOW_UNVERIFIED_JWT must be false outside local/test environments."
                raise ValueError(msg)
            if self.app_kms_provider != "managed":
                msg = "APP_KMS_PROVIDER=managed is required outside local/test environments."
                raise ValueError(msg)
            if self.managed_kms_key_id is None:
                msg = "MANAGED_KMS_KEY_ID is required outside local/test environments."
                raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
