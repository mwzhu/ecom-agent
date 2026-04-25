from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status

from api.auth.clerk import ClerkClaims, get_clerk_claims
from api.db import MerchantIdentity, TenantRepository, get_tenant_repository


@dataclass(frozen=True)
class TenantContext:
    merchant_id: UUID
    clerk_org_id: str
    merchant_name: str
    tier: str
    actor_user_id: str
    actor_email: str | None = None


_current_tenant: ContextVar[TenantContext | None] = ContextVar("current_tenant", default=None)


def current_tenant() -> TenantContext:
    tenant = _current_tenant.get()
    if tenant is None:
        raise RuntimeError("Tenant context has not been initialized for this request.")
    return tenant


def tenant_from_identity(identity: MerchantIdentity, claims: ClerkClaims) -> TenantContext:
    return TenantContext(
        merchant_id=identity.id,
        clerk_org_id=identity.clerk_org_id,
        merchant_name=identity.name,
        tier=identity.tier,
        actor_user_id=claims.subject,
        actor_email=claims.email,
    )


async def get_current_tenant(
    claims: Annotated[ClerkClaims, Depends(get_clerk_claims)],
    repository: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> AsyncIterator[TenantContext]:
    merchant = await repository.get_merchant_by_clerk_org_id(claims.org_id)
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No merchant is mapped to this Clerk organization.",
        )
    await repository.set_merchant_scope(merchant.id)
    tenant = tenant_from_identity(merchant, claims)
    token = _current_tenant.set(tenant)
    try:
        yield tenant
    finally:
        _current_tenant.reset(token)
