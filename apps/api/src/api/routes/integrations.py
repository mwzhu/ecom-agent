from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from api.auth.tenant import TenantContext, get_current_tenant
from api.config import Settings, get_settings
from api.integrations import (
    IntegrationProvider,
    IntegrationRepository,
    webhook_external_account_id,
    webhook_identity_metadata_keys,
)
from api.integrations.base import JsonValue
from api.integrations.dependencies import get_integration_repository

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])


class ShopifyInstallResponse(BaseModel):
    install_url: str


class ProviderCredentialInstallRequest(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ProviderCredentialInstallResponse(BaseModel):
    provider: IntegrationProvider
    status: str
    webhook_source_external_account_id: str


@router.get("/shopify/install", response_model=ShopifyInstallResponse)
async def shopify_install(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    settings: Annotated[Settings, Depends(get_settings)],
    shop: str = Query(min_length=3),
) -> ShopifyInstallResponse:
    _require_shopify_oauth_settings(settings)
    shop_domain = _normalize_shop(shop)
    state = sign_shopify_state(settings, tenant.merchant_id)
    query = urlencode(
        {
            "client_id": settings.shopify_client_id,
            "scope": settings.shopify_oauth_scopes,
            "redirect_uri": f"{settings.api_base_url}/v1/integrations/shopify/callback",
            "state": state,
        }
    )
    return ShopifyInstallResponse(
        install_url=f"https://{shop_domain}/admin/oauth/authorize?{query}"
    )


@router.get("/gorgias/install")
async def gorgias_install(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    settings: Annotated[Settings, Depends(get_settings)],
    account: str = Query(min_length=1),
) -> RedirectResponse:
    _require_gorgias_oauth_settings(settings)
    account_domain = _normalize_gorgias_account(account)
    state = sign_gorgias_state(settings, tenant.merchant_id, account_domain)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.gorgias_client_id,
            "redirect_uri": _gorgias_redirect_uri(settings),
            "scope": settings.gorgias_oauth_scopes,
            "state": state,
            "nonce": uuid4().hex,
        }
    )
    return RedirectResponse(
        url=f"https://{account_domain}/oauth/authorize?{query}",
        status_code=307,
    )


@router.post("/{provider}/install", response_model=ProviderCredentialInstallResponse)
async def provider_install(
    provider: IntegrationProvider,
    request: ProviderCredentialInstallRequest,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
) -> ProviderCredentialInstallResponse:
    external_account_id = webhook_external_account_id(provider, request.metadata)
    if external_account_id is None:
        keys = ", ".join(webhook_identity_metadata_keys(provider))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Credential metadata for {provider.value} must include one of: {keys}."
            ),
        )

    installed_by = request.metadata.get("installed_by")
    metadata: dict[str, JsonValue] = {
        **request.metadata,
        "installed_by": installed_by if isinstance(installed_by, str) else "provider_install_api",
    }
    await repository.upsert_credential(
        tenant.merchant_id,
        provider,
        access_token=request.access_token,
        refresh_token=request.refresh_token,
        expires_at=request.expires_at,
        metadata=metadata,
    )
    return ProviderCredentialInstallResponse(
        provider=provider,
        status="installed",
        webhook_source_external_account_id=external_account_id,
    )


@router.get("/shopify/callback")
async def shopify_callback(
    request: Request,
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    shop: str = Query(min_length=3),
    code: str = Query(min_length=1),
    state: str = Query(min_length=1),
) -> RedirectResponse:
    _require_shopify_oauth_settings(settings)
    if not verify_shopify_query_hmac(settings, dict(request.query_params)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Shopify HMAC.",
        )

    merchant_id = verify_shopify_state(settings, state)
    await repository.set_merchant_scope(merchant_id)
    shop_domain = _normalize_shop(shop)
    token_payload = await _exchange_shopify_code(settings, shop_domain, code)
    access_token = token_payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Shopify did not return an access token.",
        )

    scope = token_payload.get("scope")
    await repository.upsert_credential(
        merchant_id,
        IntegrationProvider.SHOPIFY,
        access_token=access_token,
        metadata={
            "shop_domain": shop_domain,
            "scope": scope if isinstance(scope, str) else None,
            "installed_by": "shopify_oauth",
        },
    )
    return RedirectResponse(url=f"{settings.api_base_url}/v1/me", status_code=303)


@router.get("/gorgias/callback")
async def gorgias_callback(
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    code: str = Query(min_length=1),
    state: str = Query(min_length=1),
) -> RedirectResponse:
    _require_gorgias_oauth_settings(settings)
    state_payload = verify_gorgias_state(settings, state)
    merchant_id = UUID(str(state_payload["merchant_id"]))
    account_domain = _normalize_gorgias_account(str(state_payload["account_domain"]))
    await repository.set_merchant_scope(merchant_id)
    token_payload = await _exchange_gorgias_code(settings, account_domain, code)
    access_token = token_payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gorgias did not return an access token.",
        )

    refresh_token = token_payload.get("refresh_token")
    scope = token_payload.get("scope")
    await repository.upsert_credential(
        merchant_id,
        IntegrationProvider.GORGIAS,
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_at=_oauth_expires_at(token_payload.get("expires_in")),
        metadata={
            "gorgias_domain": account_domain,
            "account_domain": account_domain,
            "scope": scope if isinstance(scope, str) else None,
            "installed_by": "gorgias_oauth",
        },
    )
    return RedirectResponse(url=f"{settings.api_base_url}/v1/me", status_code=303)


def sign_shopify_state(settings: Settings, merchant_id: UUID) -> str:
    if settings.shopify_client_secret is None:
        raise RuntimeError("SHOPIFY_CLIENT_SECRET is required to sign OAuth state.")
    return _sign_oauth_state(
        settings.shopify_client_secret,
        {
            "merchant_id": str(merchant_id),
            "nonce": uuid4().hex,
        },
    )


def verify_shopify_state(settings: Settings, state: str) -> UUID:
    if settings.shopify_client_secret is None:
        raise RuntimeError("SHOPIFY_CLIENT_SECRET is required to verify OAuth state.")
    try:
        payload = _verify_oauth_state(settings.shopify_client_secret, state)
        return UUID(str(payload["merchant_id"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OAuth state.",
        ) from exc


def sign_gorgias_state(settings: Settings, merchant_id: UUID, account_domain: str) -> str:
    if settings.gorgias_client_secret is None:
        raise RuntimeError("GORGIAS_CLIENT_SECRET is required to sign OAuth state.")
    return _sign_oauth_state(
        settings.gorgias_client_secret,
        {
            "merchant_id": str(merchant_id),
            "account_domain": _normalize_gorgias_account(account_domain),
            "nonce": uuid4().hex,
        },
    )


def verify_gorgias_state(settings: Settings, state: str) -> dict[str, str]:
    if settings.gorgias_client_secret is None:
        raise RuntimeError("GORGIAS_CLIENT_SECRET is required to verify OAuth state.")
    try:
        payload = _verify_oauth_state(settings.gorgias_client_secret, state)
        merchant_id = str(payload["merchant_id"])
        account_domain = _normalize_gorgias_account(str(payload["account_domain"]))
        UUID(merchant_id)
        return {"merchant_id": merchant_id, "account_domain": account_domain}
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OAuth state.",
        ) from exc


def _sign_oauth_state(secret: str, body: dict[str, str]) -> str:
    encoded_body = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"),
        encoded_body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded_body}.{signature}"


def _verify_oauth_state(secret: str, state: str) -> dict[str, object]:
    encoded_body, separator, signature = state.partition(".")
    if not separator:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid OAuth state.")
    expected = hmac.new(
        secret.encode("utf-8"),
        encoded_body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid OAuth state.")
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded_body.encode("utf-8")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OAuth state.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid OAuth state.")
    return payload


def verify_shopify_query_hmac(settings: Settings, params: dict[str, str]) -> bool:
    if settings.shopify_client_secret is None:
        raise RuntimeError("SHOPIFY_CLIENT_SECRET is required to verify Shopify HMAC.")
    signature = params.pop("hmac", None)
    params.pop("signature", None)
    if signature is None:
        return False
    message = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    expected = hmac.new(
        settings.shopify_client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


async def _exchange_shopify_code(
    settings: Settings,
    shop_domain: str,
    code: str,
) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=settings.integration_http_timeout_seconds) as client:
        response = await client.post(
            f"https://{shop_domain}/admin/oauth/access_token",
            json={
                "client_id": settings.shopify_client_id,
                "client_secret": settings.shopify_client_secret,
                "code": code,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Shopify returned an invalid OAuth payload.",
            )
        return payload


async def _exchange_gorgias_code(
    settings: Settings,
    account_domain: str,
    code: str,
) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=settings.integration_http_timeout_seconds) as client:
        response = await client.post(
            f"https://{account_domain}/oauth/token",
            auth=(settings.gorgias_client_id or "", settings.gorgias_client_secret or ""),
            data={
                "grant_type": "authorization_code",
                "redirect_uri": _gorgias_redirect_uri(settings),
                "code": code,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gorgias returned an invalid OAuth payload.",
            )
        return payload


def _normalize_shop(shop: str) -> str:
    normalized = shop.removeprefix("https://").removeprefix("http://").strip("/")
    if "/" in normalized or not normalized.endswith(".myshopify.com"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shop must be a *.myshopify.com domain.",
        )
    return normalized


def _normalize_gorgias_account(account: str) -> str:
    normalized = account.removeprefix("https://").removeprefix("http://").strip("/")
    if "/" in normalized or not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gorgias account must be a subdomain or *.gorgias.com domain.",
        )
    if "." not in normalized:
        normalized = f"{normalized}.gorgias.com"
    if not normalized.endswith(".gorgias.com"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gorgias account must be a subdomain or *.gorgias.com domain.",
        )
    return normalized.lower()


def _gorgias_redirect_uri(settings: Settings) -> str:
    return f"{settings.api_base_url}/v1/integrations/gorgias/callback"


def _oauth_expires_at(expires_in: object) -> datetime | None:
    if isinstance(expires_in, bool):
        return None
    if isinstance(expires_in, int | float) and expires_in > 0:
        return datetime.now(UTC) + timedelta(seconds=float(expires_in))
    return None


def _require_shopify_oauth_settings(settings: Settings) -> None:
    if settings.shopify_client_id is None or settings.shopify_client_secret is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Shopify OAuth is not configured.",
        )


def _require_gorgias_oauth_settings(settings: Settings) -> None:
    if settings.gorgias_client_id is None or settings.gorgias_client_secret is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gorgias OAuth is not configured.",
        )
