from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth.tenant import TenantContext, get_current_tenant

router = APIRouter(prefix="/v1", tags=["auth"])


class MeResponse(BaseModel):
    merchant_id: str
    clerk_org_id: str
    merchant_name: str
    tier: str
    actor_user_id: str
    actor_email: str | None


@router.get("/me", response_model=MeResponse)
async def me(tenant: Annotated[TenantContext, Depends(get_current_tenant)]) -> MeResponse:
    return MeResponse(
        merchant_id=str(tenant.merchant_id),
        clerk_org_id=tenant.clerk_org_id,
        merchant_name=tenant.merchant_name,
        tier=tenant.tier,
        actor_user_id=tenant.actor_user_id,
        actor_email=tenant.actor_email,
    )
