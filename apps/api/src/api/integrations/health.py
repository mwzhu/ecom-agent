from __future__ import annotations

from dataclasses import dataclass

from api.db.models import IntegrationHealthStatus
from api.integrations.base import (
    IntegrationError,
    IntegrationErrorKind,
    IntegrationProvider,
    JsonObject,
    ProviderCredential,
)
from api.integrations.gorgias import GorgiasClient
from api.integrations.scopes import effective_granted_scopes, required_scopes_for_provider
from api.integrations.shopify import ShopifyClient
from api.integrations.stripe import StripeClient


@dataclass(frozen=True)
class CredentialHealthResult:
    provider: IntegrationProvider
    status: IntegrationHealthStatus
    provider_account_id: str | None
    granted_scopes: list[str]
    missing_scopes: list[str]
    details: JsonObject


async def check_credential_health(
    credential: ProviderCredential,
    *,
    live: bool = False,
) -> CredentialHealthResult:
    granted_scopes = _granted_scopes(credential)
    effective_scopes = effective_granted_scopes(credential.provider, granted_scopes)
    required_scopes = required_scopes_for_provider(credential.provider)
    missing_scopes = sorted(scope for scope in required_scopes if scope not in effective_scopes)
    provider_account_id = _provider_account_id(credential)
    details: JsonObject = {
        "effective_scopes": effective_scopes,
        "required_scopes": required_scopes,
    }

    if live:
        try:
            live_details = await _live_probe(credential)
            details.update(live_details)
            provider_account_id = str(live_details.get("provider_account_id") or provider_account_id)
        except IntegrationError as exc:
            return CredentialHealthResult(
                provider=credential.provider,
                status=(
                    IntegrationHealthStatus.AUTH_FAILED
                    if exc.normalized.kind is IntegrationErrorKind.AUTH_EXPIRED
                    else IntegrationHealthStatus.ERROR
                ),
                provider_account_id=provider_account_id,
                granted_scopes=granted_scopes,
                missing_scopes=missing_scopes,
                details={"error": exc.normalized.model_dump(mode="json"), **details},
            )

    return CredentialHealthResult(
        provider=credential.provider,
        status=(
            IntegrationHealthStatus.MISSING_SCOPES
            if missing_scopes
            else IntegrationHealthStatus.HEALTHY
        ),
        provider_account_id=provider_account_id,
        granted_scopes=granted_scopes,
        missing_scopes=missing_scopes,
        details={**details, "missing_scopes": missing_scopes},
    )


async def _live_probe(credential: ProviderCredential) -> JsonObject:
    if credential.provider is IntegrationProvider.SHOPIFY:
        shop_domain = _metadata_string(credential, "shop_domain")
        payload = await ShopifyClient(credential.access_token, shop_domain).graphql(
            """
            query EcomAgentCredentialProbe {
              shop { id name myshopifyDomain plan { displayName } }
            }
            """
        )
        shop = payload.get("data", {}).get("shop") if isinstance(payload.get("data"), dict) else {}
        return {
            "provider_account_id": shop.get("myshopifyDomain") if isinstance(shop, dict) else None,
            "account": shop if isinstance(shop, dict) else {},
        }
    if credential.provider is IntegrationProvider.STRIPE:
        account = await StripeClient(credential.access_token).get_account()
        return {
            "provider_account_id": account.get("id"),
            "mode": "live" if account.get("livemode") else "test",
            "account": account,
        }
    if credential.provider is IntegrationProvider.GORGIAS:
        account_domain = _metadata_string(credential, "account_domain")
        ticket_probe = await GorgiasClient(
            credential.access_token,
            account_domain,
            username=_optional_metadata_string(credential, "username"),
            auth_scheme=_optional_metadata_string(credential, "auth_scheme"),
        ).search_customer("healthcheck@example.invalid")
        return {
            "provider_account_id": account_domain,
            "account": {"domain": account_domain, "probe": ticket_probe},
        }
    return {"provider_account_id": _provider_account_id(credential)}


def _provider_account_id(credential: ProviderCredential) -> str | None:
    for key in (
        "shop_domain",
        "stripe_account_id",
        "account_id",
        "gorgias_domain",
        "account_domain",
        "shipbob_merchant_id",
        "shipstation_account_id",
        "gmail_address",
    ):
        value = credential.metadata.get(key)
        if isinstance(value, str) and value:
            return value.lower()
        if isinstance(value, int):
            return str(value)
    return None


def _granted_scopes(credential: ProviderCredential) -> list[str]:
    scope_value = (
        credential.metadata.get("scope")
        or credential.metadata.get("scopes")
        or credential.metadata.get("granted_scopes")
    )
    if isinstance(scope_value, str):
        return sorted({scope.strip() for scope in scope_value.replace(" ", ",").split(",") if scope.strip()})
    if isinstance(scope_value, list):
        return sorted({scope for scope in scope_value if isinstance(scope, str) and scope})
    return []


def _metadata_string(credential: ProviderCredential, key: str) -> str:
    value = credential.metadata.get(key)
    if isinstance(value, str) and value:
        return value
    raise IntegrationError(
        IntegrationErrorKind.FATAL,
        credential.provider,
        f"Credential metadata is missing required field {key!r}.",
    )


def _optional_metadata_string(credential: ProviderCredential, key: str) -> str | None:
    value = credential.metadata.get(key)
    return value if isinstance(value, str) and value else None
