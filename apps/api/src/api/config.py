from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    environment: str = "development"
    api_base_url: str = "http://localhost:8000"
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
    stripe_webhook_secret: str | None = None
    shopify_client_id: str | None = None
    shopify_client_secret: str | None = None
    shopify_admin_api_version: str = "2025-10"
    shopify_oauth_scopes: str = (
        "read_orders,write_orders,read_customers,read_fulfillments,write_fulfillments,"
        "read_order_edits,write_order_edits"
    )
    shopify_webhook_secret: str | None = None
    gorgias_client_id: str | None = None
    gorgias_client_secret: str | None = None
    gorgias_webhook_secret: str | None = None
    shipbob_webhook_secret: str | None = None
    shipstation_webhook_secret: str | None = None
    gmail_webhook_secret: str | None = None
    langgraph_assistant_id: str = "order-exception"
    integration_http_timeout_seconds: float = 30.0
    app_kms_key_id: str = "local-dev-cmk"
    local_kms_master_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_environment_safety(self) -> Settings:
        if self.environment != "development":
            if self.clerk_allow_unverified_jwt:
                msg = "CLERK_ALLOW_UNVERIFIED_JWT must be false outside development."
                raise ValueError(msg)
            if self.local_kms_master_key is None:
                msg = "LOCAL_KMS_MASTER_KEY is required outside development until AWS KMS is wired."
                raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
