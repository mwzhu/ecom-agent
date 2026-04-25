from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from api.auth.tenant import TenantContext, get_current_tenant
from api.integrations import IntegrationProvider
from api.integrations.dependencies import get_integration_repository
from api.main import app


@dataclass
class InMemoryCredentialRepository:
    upserts: list[dict[str, object]] = field(default_factory=list)

    async def upsert_credential(
        self,
        merchant_id: UUID,
        provider: IntegrationProvider,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.upserts.append(
            {
                "merchant_id": merchant_id,
                "provider": provider,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "metadata": metadata or {},
            }
        )


@pytest.mark.parametrize(
    ("provider", "metadata", "external_account_id"),
    [
        ("shopify", {"shop_domain": "demo.myshopify.com"}, "demo.myshopify.com"),
        ("stripe", {"stripe_account_id": "acct_123"}, "acct_123"),
        ("gorgias", {"gorgias_domain": "demo.gorgias.com"}, "demo.gorgias.com"),
        ("shipbob", {"shipbob_merchant_id": 12345}, "12345"),
        ("shipstation", {"shipstation_account_id": "ss_123"}, "ss_123"),
        ("gmail", {"gmail_address": "ops@example.com"}, "ops@example.com"),
    ],
)
def test_provider_install_seeds_webhook_source_identity(
    provider: str,
    metadata: dict[str, Any],
    external_account_id: str,
) -> None:
    merchant_id = uuid4()
    repository = InMemoryCredentialRepository()
    client = _client(repository, merchant_id)

    try:
        response = client.post(
            f"/v1/integrations/{provider}/install",
            json={"access_token": "token_123", "metadata": metadata},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["webhook_source_external_account_id"] == external_account_id
    assert repository.upserts[0]["merchant_id"] == merchant_id
    assert repository.upserts[0]["provider"] == IntegrationProvider(provider)
    assert repository.upserts[0]["metadata"] == {
        **metadata,
        "installed_by": "provider_install_api",
    }


def test_provider_install_rejects_credentials_without_webhook_identity() -> None:
    client = _client(InMemoryCredentialRepository(), uuid4())

    try:
        response = client.post(
            "/v1/integrations/stripe/install",
            json={"access_token": "token_123", "metadata": {"nickname": "primary"}},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "stripe_account_id" in response.json()["detail"]


def _client(repository: InMemoryCredentialRepository, merchant_id: UUID) -> TestClient:
    tenant = TenantContext(
        merchant_id=merchant_id,
        clerk_org_id="org_a",
        merchant_name="Merchant A",
        tier="starter",
        actor_user_id="user_123",
        actor_email="ops@example.com",
    )
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    app.dependency_overrides[get_integration_repository] = lambda: repository
    return TestClient(app)
