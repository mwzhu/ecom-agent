from __future__ import annotations

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from api.auth.tenant import TenantContext, get_current_tenant
from api.config import Settings, get_settings
from api.db import EvalReviewSummary, TenantRepository, get_tenant_repository
from api.db.models import ActorType

router = APIRouter(prefix="/v1/evals", tags=["evals"])


class EvalReviewResponse(BaseModel):
    id: str
    case_id: str
    merchant_id: str
    langsmith_run_id: str | None
    score: int
    passed: bool
    reason: str
    payload: dict[str, object]
    status: str
    created_at: str


class OnlineEvalReviewRequest(BaseModel):
    merchant_id: UUID
    case_id: UUID
    langsmith_run_id: str | None = None
    score: int = Field(ge=0, le=5)
    passed: bool = False
    reason: str = Field(min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)


def _serialize_review(review: EvalReviewSummary) -> EvalReviewResponse:
    return EvalReviewResponse(
        id=str(review.id),
        case_id=str(review.case_id),
        merchant_id=str(review.merchant_id),
        langsmith_run_id=review.langsmith_run_id,
        score=review.score,
        passed=review.passed,
        reason=review.reason,
        payload=review.payload,
        status=review.status,
        created_at=review.created_at,
    )


@router.get("/review-queue", response_model=list[EvalReviewResponse])
async def list_eval_review_queue(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> list[EvalReviewResponse]:
    reviews = await repository.list_eval_reviews_for_merchant(tenant.merchant_id)
    return [_serialize_review(review) for review in reviews]


@router.post("/online-review", response_model=EvalReviewResponse)
async def queue_online_eval_review(
    request: OnlineEvalReviewRequest,
    repository: Annotated[TenantRepository, Depends(get_tenant_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    internal_secret: Annotated[str | None, Header(alias="X-Ecom-Internal-Secret")] = None,
) -> EvalReviewResponse:
    _verify_internal_secret(settings=settings, internal_secret=internal_secret)
    await repository.set_merchant_scope(request.merchant_id)
    case = await repository.get_case_detail(request.merchant_id, request.case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")

    review = await repository.queue_eval_review_item(
        merchant_id=request.merchant_id,
        case_id=request.case_id,
        langsmith_run_id=request.langsmith_run_id,
        score=request.score,
        passed=request.passed,
        reason=request.reason,
        payload=request.payload,
    )
    await repository.record_case_event(
        merchant_id=request.merchant_id,
        case_id=request.case_id,
        kind="eval.online_low_confidence",
        payload={
            "review_id": str(review.id),
            "score": review.score,
            "passed": review.passed,
            "reason": review.reason,
            "payload": review.payload,
        },
        actor=ActorType.SYSTEM,
        langsmith_run_id=request.langsmith_run_id,
    )
    return _serialize_review(review)


def _verify_internal_secret(*, settings: Settings, internal_secret: str | None) -> None:
    expected = settings.online_eval_webhook_secret
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Online eval webhook secret is not configured.",
        )
    if not secrets.compare_digest(internal_secret or "", expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid online eval webhook secret.",
        )
