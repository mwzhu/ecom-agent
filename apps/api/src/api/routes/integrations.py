from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
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
    ProviderCredential,
    webhook_external_account_id,
    webhook_identity_metadata_keys,
)
from api.integrations.base import JsonValue
from api.integrations.dependencies import get_integration_repository
from api.integrations.health import CredentialHealthResult, check_credential_health
from api.integrations.webhook_registry import ensure_provider_webhooks

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])
logger = logging.getLogger(__name__)


class ShopifyInstallResponse(BaseModel):
    install_url: str


class StripeConnectInstallResponse(BaseModel):
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
    health_status: str | None = None
    missing_scopes: list[str] = Field(default_factory=list)


class ProviderDisconnectResponse(BaseModel):
    provider: IntegrationProvider
    status: str


class ProviderHealthResponse(BaseModel):
    provider: IntegrationProvider
    status: str
    provider_account_id: str | None
    granted_scopes: list[str]
    missing_scopes: list[str]
    checked_at: str | None
    error: dict[str, object] | None


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


@router.get("/stripe/connect/install", response_model=StripeConnectInstallResponse)
async def stripe_connect_install(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StripeConnectInstallResponse:
    if settings.stripe_connect_client_id is None or settings.stripe_secret_key is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe Connect is not configured.",
        )
    state = _sign_oauth_state(
        settings.stripe_secret_key,
        {"merchant_id": str(tenant.merchant_id), "nonce": uuid4().hex},
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.stripe_connect_client_id,
            "scope": "read_write",
            "redirect_uri": f"{settings.api_base_url}/v1/integrations/stripe/connect/callback",
            "state": state,
        }
    )
    return StripeConnectInstallResponse(
        install_url=f"https://connect.stripe.com/oauth/authorize?{query}"
    )


@router.post("/{provider}/install", response_model=ProviderCredentialInstallResponse)
async def provider_install(
    provider: IntegrationProvider,
    request: ProviderCredentialInstallRequest,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
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
    health = await _run_post_install_tasks(
        repository=repository,
        settings=settings,
        merchant_id=tenant.merchant_id,
        provider=provider,
    )
    return ProviderCredentialInstallResponse(
        provider=provider,
        status="installed",
        webhook_source_external_account_id=external_account_id,
        health_status=health.status.value if health else None,
        missing_scopes=health.missing_scopes if health else [],
    )


@router.get("/health", response_model=list[ProviderHealthResponse])
async def integration_health(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
) -> list[ProviderHealthResponse]:
    snapshots = await repository.list_credential_health(tenant.merchant_id)
    return [
        ProviderHealthResponse(
            provider=snapshot.provider,
            status=snapshot.status,
            provider_account_id=snapshot.provider_account_id,
            granted_scopes=snapshot.granted_scopes,
            missing_scopes=snapshot.missing_scopes,
            checked_at=snapshot.checked_at.isoformat() if snapshot.checked_at else None,
            error=snapshot.error,
        )
        for snapshot in snapshots
    ]


@router.delete("/{provider}", response_model=ProviderDisconnectResponse)
async def provider_disconnect(
    provider: IntegrationProvider,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
) -> ProviderDisconnectResponse:
    await repository.disconnect_provider(
        merchant_id=tenant.merchant_id,
        provider=provider,
        actor_id=tenant.actor_email or tenant.actor_user_id,
    )
    return ProviderDisconnectResponse(provider=provider, status="disconnected")


@router.get("/shopify/callback")
async def shopify_callback(
    request: Request,
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    shop: str = Query(min_length=3),
    code: str = Query(min_length=1),
    state: str = Query(min_length=1),
) -> RedirectResponse:
    try:
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
        await _run_post_install_tasks(
            repository=repository,
            settings=settings,
            merchant_id=merchant_id,
            provider=IntegrationProvider.SHOPIFY,
        )
        return RedirectResponse(url=_console_success_url(settings), status_code=303)
    except HTTPException as exc:
        return _provider_setup_error_redirect(settings, IntegrationProvider.SHOPIFY, exc)
    except Exception:
        logger.exception("Shopify OAuth callback failed.")
        return _provider_setup_error_redirect(settings, IntegrationProvider.SHOPIFY)


@router.get("/gorgias/callback")
async def gorgias_callback(
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    code: str = Query(min_length=1),
    state: str = Query(min_length=1),
) -> RedirectResponse:
    try:
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
        await _run_post_install_tasks(
            repository=repository,
            settings=settings,
            merchant_id=merchant_id,
            provider=IntegrationProvider.GORGIAS,
        )
        return RedirectResponse(url=_console_success_url(settings), status_code=303)
    except HTTPException as exc:
        return _provider_setup_error_redirect(settings, IntegrationProvider.GORGIAS, exc)
    except Exception:
        logger.exception("Gorgias OAuth callback failed.")
        return _provider_setup_error_redirect(settings, IntegrationProvider.GORGIAS)


@router.get("/stripe/connect/callback")
async def stripe_connect_callback(
    repository: Annotated[IntegrationRepository, Depends(get_integration_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    code: str = Query(min_length=1),
    state: str = Query(min_length=1),
) -> RedirectResponse:
    try:
        if settings.stripe_secret_key is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stripe Connect is not configured.",
            )
        state_payload = _verify_oauth_state(settings.stripe_secret_key, state)
        merchant_id = UUID(str(state_payload["merchant_id"]))
        await repository.set_merchant_scope(merchant_id)
        token_payload = await _exchange_stripe_connect_code(settings, code)
        access_token = token_payload.get("access_token")
        stripe_user_id = token_payload.get("stripe_user_id")
        if not isinstance(access_token, str) or not access_token:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Stripe did not return an access token.",
            )
        if not isinstance(stripe_user_id, str) or not stripe_user_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Stripe did not return a connected account id.",
            )
        livemode = token_payload.get("livemode")
        await repository.upsert_credential(
            merchant_id,
            IntegrationProvider.STRIPE,
            access_token=access_token,
            metadata={
                "stripe_account_id": stripe_user_id,
                "scope": "charges:read,disputes:read,refunds:write",
                "mode": "live" if livemode is True else "test",
                "installed_by": "stripe_connect",
            },
        )
        await _run_post_install_tasks(
            repository=repository,
            settings=settings,
            merchant_id=merchant_id,
            provider=IntegrationProvider.STRIPE,
        )
        return RedirectResponse(url=_console_success_url(settings), status_code=303)
    except (KeyError, ValueError):
        return _provider_setup_error_redirect(
            settings,
            IntegrationProvider.STRIPE,
            HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid OAuth state."),
        )
    except HTTPException as exc:
        return _provider_setup_error_redirect(settings, IntegrationProvider.STRIPE, exc)
    except Exception:
        logger.exception("Stripe OAuth callback failed.")
        return _provider_setup_error_redirect(settings, IntegrationProvider.STRIPE)


async def _run_post_install_tasks(
    *,
    repository: IntegrationRepository,
    settings: Settings,
    merchant_id: UUID,
    provider: IntegrationProvider,
) -> CredentialHealthResult | None:
    try:
        credential = await repository.get_credential(merchant_id, provider)
    except Exception:
        return None
    health = await check_credential_health(credential, live=False)
    await repository.update_credential_health(
        merchant_id=merchant_id,
        provider=provider,
        status=health.status,
        provider_account_id=health.provider_account_id,
        granted_scopes=health.granted_scopes,
        error=health.details,
    )
    await ensure_provider_webhooks(
        merchant_id=merchant_id,
        provider=provider,
        credential=_credential_with_scopes(credential, health.granted_scopes),
        repository=repository,
        settings=settings,
    )
    return health


def _credential_with_scopes(
    credential: ProviderCredential,
    granted_scopes: list[str],
) -> ProviderCredential:
    metadata = dict(credential.metadata)
    metadata["granted_scopes"] = granted_scopes
    return ProviderCredential(
        provider=credential.provider,
        access_token=credential.access_token,
        refresh_token=credential.refresh_token,
        expires_at=credential.expires_at,
        metadata=metadata,
    )


def _console_success_url(settings: Settings) -> str:
    return f"{settings.console_base_url.rstrip('/')}/?setup=connected"


def _provider_setup_error_redirect(
    settings: Settings,
    provider: IntegrationProvider,
    exc: HTTPException | None = None,
) -> RedirectResponse:
    message = "Setup did not finish. Please try connecting again."
    if exc is not None and isinstance(exc.detail, str):
        message = exc.detail
    query = urlencode(
        {
            "setup": "error",
            "provider": provider.value,
            "message": message,
        }
    )
    return RedirectResponse(url=f"{settings.console_base_url.rstrip('/')}?{query}", status_code=303)


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


async def _exchange_stripe_connect_code(
    settings: Settings,
    code: str,
) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=settings.integration_http_timeout_seconds) as client:
        response = await client.post(
            "https://connect.stripe.com/oauth/token",
            auth=(settings.stripe_secret_key or "", ""),
            data={"grant_type": "authorization_code", "code": code},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Stripe returned an invalid OAuth payload.",
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
    if not _real_oauth_value(settings.shopify_client_id) or not _real_oauth_value(
        settings.shopify_client_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Shopify OAuth is not configured.",
        )


def _require_gorgias_oauth_settings(settings: Settings) -> None:
    if not _real_oauth_value(settings.gorgias_client_id) or not _real_oauth_value(
        settings.gorgias_client_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gorgias OAuth is not configured.",
        )


def _real_oauth_value(value: str | None) -> bool:
    return bool(value and value.strip() and value.strip() != "..." and "<" not in value)
