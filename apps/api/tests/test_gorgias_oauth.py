from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from api.auth.tenant import TenantContext, get_current_tenant
from api.config import Settings, get_settings
from api.integrations import IntegrationProvider
from api.integrations.dependencies import get_integration_repository
from api.main import app
from api.routes import integrations
from api.routes.integrations import sign_gorgias_state, verify_gorgias_state


@dataclass
class InMemoryGorgiasOAuthRepository:
    scopes: list[UUID] = field(default_factory=list)
    upserts: list[dict[str, object]] = field(default_factory=list)

    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        self.scopes.append(merchant_id)

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


def test_gorgias_oauth_state_round_trips_and_detects_tampering() -> None:
    settings = Settings(gorgias_client_secret="oauth-secret")
    merchant_id = uuid4()

    state = sign_gorgias_state(settings, merchant_id, "demo")

    assert verify_gorgias_state(settings, state) == {
        "merchant_id": str(merchant_id),
        "account_domain": "demo.gorgias.com",
    }
    assert state != sign_gorgias_state(settings, merchant_id, "demo")


def test_gorgias_install_redirects_to_account_authorize_url() -> None:
    merchant_id = uuid4()
    settings = _settings()
    client = _client(settings=settings, merchant_id=merchant_id)

    try:
        response = client.get(
            "/v1/integrations/gorgias/install?account=demo",
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 307
    location = response.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://demo.gorgias.com/oauth/authorize"
    )
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["gorgias-client-id"]
    assert query["redirect_uri"] == [
        "https://api.example.com/v1/integrations/gorgias/callback"
    ]
    assert query["scope"] == [settings.gorgias_oauth_scopes]
    assert verify_gorgias_state(settings, query["state"][0])["merchant_id"] == str(merchant_id)


def test_gorgias_install_rejects_placeholder_oauth_credentials() -> None:
    merchant_id = uuid4()
    settings = Settings(
        api_base_url="https://api.example.com",
        console_base_url="https://console.example.com",
        gorgias_client_id="...",
        gorgias_client_secret="...",
    )
    client = _client(settings=settings, merchant_id=merchant_id)

    try:
        response = client.get(
            "/v1/integrations/gorgias/install?account=demo",
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json() == {"detail": "Gorgias OAuth is not configured."}


def test_gorgias_callback_exchanges_code_and_stores_credential(
    monkeypatch: Any,
) -> None:
    settings = _settings()
    merchant_id = uuid4()
    repository = InMemoryGorgiasOAuthRepository()

    async def exchange_code(
        settings: Settings,
        account_domain: str,
        code: str,
    ) -> dict[str, object]:
        assert settings.gorgias_client_id == "gorgias-client-id"
        assert account_domain == "demo.gorgias.com"
        assert code == "code_123"
        return {
            "access_token": "access_123",
            "refresh_token": "refresh_123",
            "expires_in": 86_400,
            "scope": settings.gorgias_oauth_scopes,
            "token_type": "Bearer",
        }

    monkeypatch.setattr(integrations, "_exchange_gorgias_code", exchange_code)
    state = sign_gorgias_state(settings, merchant_id, "demo.gorgias.com")
    client = _client(settings=settings, merchant_id=merchant_id, repository=repository)

    try:
        response = client.get(
            f"/v1/integrations/gorgias/callback?code=code_123&state={state}",
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 303
    assert response.headers["location"] == "https://console.example.com/?setup=connected"
    assert repository.scopes == [merchant_id]
    assert repository.upserts == [
        {
            "merchant_id": merchant_id,
            "provider": IntegrationProvider.GORGIAS,
            "access_token": "access_123",
            "refresh_token": "refresh_123",
            "expires_at": repository.upserts[0]["expires_at"],
            "metadata": {
                "gorgias_domain": "demo.gorgias.com",
                "account_domain": "demo.gorgias.com",
                "scope": settings.gorgias_oauth_scopes,
                "installed_by": "gorgias_oauth",
            },
        }
    ]
    assert repository.upserts[0]["expires_at"] is not None


def _settings() -> Settings:
    return Settings(
        api_base_url="https://api.example.com",
        console_base_url="https://console.example.com",
        gorgias_client_id="gorgias-client-id",
        gorgias_client_secret="gorgias-client-secret",
    )


def _client(
    *,
    settings: Settings,
    merchant_id: UUID,
    repository: InMemoryGorgiasOAuthRepository | None = None,
) -> TestClient:
    tenant = TenantContext(
        merchant_id=merchant_id,
        clerk_org_id="org_a",
        merchant_name="Merchant A",
        tier="pilot",
        actor_user_id="user_123",
        actor_email="ops@example.com",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    if repository is not None:
        app.dependency_overrides[get_integration_repository] = lambda: repository
    return TestClient(app)
