from __future__ import annotations

import base64
import hashlib
import hmac
import json
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
        previous_secret = settings.shopify_previous_webhook_secret
        signature = headers.get("x-shopify-hmac-sha256")
        if not signature or not _any_base64_hmac((secret, previous_secret), body, signature):
            _raise_invalid_signature(provider)
        return

    if provider is IntegrationProvider.STRIPE:
        secret = _required_secret(provider, settings.stripe_webhook_secret)
        previous_secret = settings.stripe_previous_webhook_secret
        signature = headers.get("stripe-signature")
        if not signature or not _any_stripe_signature((secret, previous_secret), body, signature):
            _raise_invalid_signature(provider)
        return

    secret_by_provider = {
        IntegrationProvider.GORGIAS: settings.gorgias_webhook_secret,
        IntegrationProvider.SHIPBOB: settings.shipbob_webhook_secret,
        IntegrationProvider.SHIPSTATION: settings.shipstation_webhook_secret,
        IntegrationProvider.GMAIL: settings.gmail_webhook_secret,
    }
    previous_secret_by_provider = {
        IntegrationProvider.GORGIAS: settings.gorgias_previous_webhook_secret,
    }
    secret = _required_secret(provider, secret_by_provider.get(provider))
    previous_secret = previous_secret_by_provider.get(provider)
    if provider is IntegrationProvider.GORGIAS:
        static_secret = headers.get("x-ecom-webhook-secret")
        if static_secret and any(
            configured and hmac.compare_digest(static_secret, configured)
            for configured in (secret, previous_secret)
        ):
            return
        body_secret = _json_body_secret(body)
        if body_secret and any(
            configured and hmac.compare_digest(body_secret, configured)
            for configured in (secret, previous_secret)
        ):
            return
    signature = (
        headers.get(f"x-{provider.value}-hmac-sha256")
        or headers.get(f"x-{provider.value}-signature")
        or headers.get("x-ecom-signature")
    )
    if not signature or not _any_hex_hmac((secret, previous_secret), body, signature):
        _raise_invalid_signature(provider)


def _any_base64_hmac(secrets: tuple[str | None, ...], body: bytes, signature: str) -> bool:
    return any(secret is not None and _verify_base64_hmac(secret, body, signature) for secret in secrets)


def _any_hex_hmac(secrets: tuple[str | None, ...], body: bytes, signature: str) -> bool:
    return any(secret is not None and _verify_hex_hmac(secret, body, signature) for secret in secrets)


def _any_stripe_signature(
    secrets: tuple[str | None, ...],
    body: bytes,
    signature_header: str,
) -> bool:
    return any(
        secret is not None and _verify_stripe_signature(secret, body, signature_header)
        for secret in secrets
    )


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


def _json_body_secret(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("webhook_secret")
    return value if isinstance(value, str) and value else None


def _raise_invalid_signature(provider: IntegrationProvider) -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Invalid {provider.value} webhook signature.",
    )
