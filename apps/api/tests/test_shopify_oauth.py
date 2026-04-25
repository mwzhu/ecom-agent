from __future__ import annotations

import hashlib
import hmac
from uuid import uuid4

from api.config import Settings
from api.routes.integrations import (
    sign_shopify_state,
    verify_shopify_query_hmac,
    verify_shopify_state,
)


def test_shopify_oauth_state_round_trips_and_detects_tampering() -> None:
    settings = Settings(shopify_client_secret="oauth-secret")
    merchant_id = uuid4()

    state = sign_shopify_state(settings, merchant_id)

    assert verify_shopify_state(settings, state) == merchant_id
    assert state != sign_shopify_state(settings, merchant_id)


def test_shopify_query_hmac_verification_uses_sorted_params() -> None:
    settings = Settings(shopify_client_secret="oauth-secret")
    params = {
        "shop": "demo.myshopify.com",
        "code": "code_123",
        "state": "state_123",
        "timestamp": "1234567890",
    }
    message = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    signature = hmac.new(
        settings.shopify_client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    assert verify_shopify_query_hmac(settings, {**params, "hmac": signature})
    assert not verify_shopify_query_hmac(settings, {**params, "hmac": "bad"})
