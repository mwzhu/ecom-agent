from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient

from api.config import Settings, get_settings


@dataclass(frozen=True)
class ClerkClaims:
    org_id: str
    subject: str
    email: str | None = None


class ClerkAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def authenticate(self, authorization: str | None) -> ClerkClaims:
        token = self._extract_bearer_token(authorization)
        payload = self._decode_token(token)
        org_id = self._org_id_from_payload(payload)
        subject = self._string_claim(payload, "sub")
        if subject is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Clerk token is missing subject.",
            )
        return ClerkClaims(
            org_id=org_id,
            subject=subject,
            email=self._string_claim(payload, "email"),
        )

    def _extract_bearer_token(self, authorization: str | None) -> str:
        if authorization is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token.",
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header.",
            )
        return token

    def _decode_token(self, token: str) -> dict[str, Any]:
        try:
            if self._settings.clerk_allow_unverified_jwt:
                if self._settings.clerk_dev_jwt_secret is None:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Dev JWT verification is not configured.",
                    )
                payload = jwt.decode(
                    token,
                    self._settings.clerk_dev_jwt_secret,
                    algorithms=["HS256"],
                    options={"verify_aud": False},
                )
            else:
                if self._settings.clerk_jwks_url is None or self._settings.clerk_issuer is None:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Clerk verification is not configured.",
                    )
                signing_key = _jwks_client(
                    self._settings.clerk_jwks_url
                ).get_signing_key_from_jwt(token)
                payload = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=["RS256", "EdDSA"],
                    audience=self._settings.clerk_audience,
                    issuer=self._settings.clerk_issuer,
                    options={"verify_aud": self._settings.clerk_audience is not None},
                )
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Clerk token.",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Clerk token payload.",
            )
        return payload

    def _org_id_from_payload(self, payload: dict[str, Any]) -> str:
        direct_org_id = self._string_claim(payload, "org_id")
        if direct_org_id is not None:
            return direct_org_id
        organization = payload.get("o")
        if isinstance(organization, dict):
            nested_org_id = organization.get("id")
            if isinstance(nested_org_id, str) and nested_org_id:
                return nested_org_id
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Clerk token is missing organization context.",
        )

    def _string_claim(self, payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        return None


def get_clerk_authenticator(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ClerkAuthenticator:
    return ClerkAuthenticator(settings)


def get_clerk_claims(
    authenticator: Annotated[ClerkAuthenticator, Depends(get_clerk_authenticator)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> ClerkClaims:
    return authenticator.authenticate(authorization)


@lru_cache
def _jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)
