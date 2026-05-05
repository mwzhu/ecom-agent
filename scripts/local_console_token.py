#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a local Clerk-shaped HS256 JWT.")
    parser.add_argument("--org-id", default="org_local_demo")
    parser.add_argument("--subject", default="user_local_operator")
    parser.add_argument("--email", default="ops@example.com")
    parser.add_argument(
        "--secret",
        default=default_secret(),
    )
    parser.add_argument("--ttl-seconds", type=int, default=60 * 60 * 24)
    args = parser.parse_args()

    now = int(time.time())
    token = encode_jwt(
        {
            "org_id": args.org_id,
            "sub": args.subject,
            "email": args.email,
            "iat": now,
            "exp": now + args.ttl_seconds,
        },
        secret=args.secret,
    )
    print(token)


def encode_jwt(payload: dict[str, Any], *, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join([b64url_json(header), b64url_json(payload)])
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256)
    return f"{signing_input}.{b64url(signature.digest())}"


def b64url_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url(raw)


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def default_secret() -> str:
    return (
        os.environ.get("CLERK_DEV_JWT_SECRET")
        or root_env().get("CLERK_DEV_JWT_SECRET")
        or "local-dev-secret-for-hs256-signatures"
    )


def root_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    try:
        contents = env_path.read_text()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


if __name__ == "__main__":
    main()
