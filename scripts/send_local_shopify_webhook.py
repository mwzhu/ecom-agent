#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a signed local Shopify webhook that creates a real API-backed case."
    )
    parser.add_argument("--api-base-url", default=os.environ.get("API_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--shop-domain", default="local-test.myshopify.com")
    parser.add_argument(
        "--secret",
        default=(
            os.environ.get("SHOPIFY_WEBHOOK_SECRET")
            or os.environ.get("SHOPIFY_CLIENT_SECRET")
            or "local-dev-shopify-webhook-secret"
        ),
    )
    parser.add_argument("--exception-type", default="fraud_triage")
    parser.add_argument("--order-name", default="#REAL-1001")
    parser.add_argument("--risk-score", type=int, default=85)
    args = parser.parse_args()

    payload = {
        "shop_domain": args.shop_domain,
        "exception_type": args.exception_type,
        "risk_score": args.risk_score,
        "payment_status": "paid",
        "order": {
            "id": f"gid://shopify/Order/{uuid4().int % 10_000_000}",
            "name": args.order_name,
            "email": "real-flow-customer@example.com",
            "total_price": "742.00",
            "financial_status": "paid",
            "customer": {
                "email": "real-flow-customer@example.com",
                "first_name": "Real",
                "last_name": "Flow",
            },
        },
        "context": {
            "risk": {"score": args.risk_score},
            "payment": {"status": "captured"},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{args.api_base_url.rstrip('/')}/v1/webhooks/shopify",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Hmac-Sha256": shopify_hmac(args.secret, body),
            "X-Shopify-Shop-Domain": args.shop_domain,
            "X-Shopify-Topic": "orders/create",
            "X-Shopify-Webhook-Id": str(uuid4()),
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(error.read().decode("utf-8"))
        raise SystemExit(error.code) from error


def shopify_hmac(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


if __name__ == "__main__":
    main()
