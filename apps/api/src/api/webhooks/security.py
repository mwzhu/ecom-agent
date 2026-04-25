from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, status

from api.config import Settings
from api.integrations import IntegrationProvider


def verify_provider_signature(
    provider: IntegrationProvider,
    *,
    settings: Settings,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if provider is IntegrationProvider.SHOPIFY:
        secret = _required_secret(
            provider,
            settings.shopify_webhook_secret or settings.shopify_client_secret,
        )
        signature = headers.get("x-shopify-hmac-sha256")
        if not signature or not _verify_base64_hmac(secret, body, signature):
            _raise_invalid_signature(provider)
        return

    if provider is IntegrationProvider.STRIPE:
        secret = _required_secret(provider, settings.stripe_webhook_secret)
        signature = headers.get("stripe-signature")
        if not signature or not _verify_stripe_signature(
            secret,
            body,
            signature,
        ):
            _raise_invalid_signature(provider)
        return

    secret_by_provider = {
        IntegrationProvider.GORGIAS: settings.gorgias_webhook_secret,
        IntegrationProvider.SHIPBOB: settings.shipbob_webhook_secret,
        IntegrationProvider.SHIPSTATION: settings.shipstation_webhook_secret,
        IntegrationProvider.GMAIL: settings.gmail_webhook_secret,
    }
    secret = _required_secret(provider, secret_by_provider.get(provider))
    signature = (
        headers.get(f"x-{provider.value}-hmac-sha256")
        or headers.get(f"x-{provider.value}-signature")
        or headers.get("x-ecom-signature")
    )
    if not signature or not _verify_hex_hmac(secret, body, signature):
        _raise_invalid_signature(provider)


def _verify_base64_hmac(secret: str, body: bytes, signature: str) -> bool:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(signature, expected)


def _verify_hex_hmac(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    normalized = signature.removeprefix("sha256=").strip()
    return hmac.compare_digest(normalized, expected)


def _verify_stripe_signature(
    secret: str | None,
    body: bytes,
    signature_header: str,
    tolerance_seconds: int = 300,
) -> bool:
    if secret is None:
        return False
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, separator, value = item.partition("=")
        if separator:
            parts.setdefault(key, []).append(value)
    timestamps = parts.get("t", [])
    signatures = parts.get("v1", [])
    if not timestamps or not signatures:
        return False
    timestamp = timestamps[0]
    try:
        event_time = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - event_time) > tolerance_seconds:
        return False
    signed_payload = timestamp.encode("utf-8") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(candidate, expected) for candidate in signatures)


def _required_secret(provider: IntegrationProvider, secret: str | None) -> str:
    if secret is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{provider.value} webhook secret is not configured.",
        )
    return secret


def _raise_invalid_signature(provider: IntegrationProvider) -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Invalid {provider.value} webhook signature.",
    )
