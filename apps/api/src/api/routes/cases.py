from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.agents import CaseDecisionDispatcher, get_case_decision_dispatcher
from api.auth.tenant import TenantContext, get_current_tenant
from api.db import (
    CaseDetail,
    CaseSummary,
    EvalCorrectionSummary,
    FopSummary,
    TenantRepository,
    get_tenant_repository,
)
from api.db.models import ActorType, CaseStatus

router = APIRouter(prefix="/v1/cases", tags=["cases"])


class CaseSummaryResponse(BaseModel):
    id: str
    merchant_id: str
    type: str
    status: str
    subject_ref: dict[str, object]


class CaseEventResponse(BaseModel):
    id: str
    case_id: str
    merchant_id: str
    kind: str
    payload: dict[str, object]
    langsmith_run_id: str | None
    actor: str
    created_at: str


class CaseDetailResponse(CaseSummaryResponse):
    langgraph_thread_id: str | None
    resolution: dict[str, object] | None
    events: list[CaseEventResponse]


class CaseDecisionRequest(BaseModel):
    decision: Literal["approve", "modify", "reject"]
    source: Literal["console", "slack"] = "console"
    actor: str = Field(min_length=1)
    modification: dict[str, object] | None = None
    note: str | None = None


class CaseDecisionResponse(BaseModel):
    case_id: str
    status: str
    langgraph_run_id: str | None
    submitted_to_langgraph: bool


class EvalCorrectionRequest(BaseModel):
    expected_resolution: dict[str, object]
    notes: str = ""


class EvalCorrectionResponse(BaseModel):
    id: str
    case_id: str
    merchant_id: str
    expected_resolution: dict[str, object]
    notes: str
    created_by: str
    status: str


class FopResponse(BaseModel):
    id: str
    merchant_id: str
    version: int
    nl_text: str
    structured: dict[str, object]
    status: str
    created_by: str
    created_at: str


def _serialize_case(case: CaseSummary) -> CaseSummaryResponse:
    return CaseSummaryResponse(
        id=str(case.id),
        merchant_id=str(case.merchant_id),
        type=case.type,
        status=case.status,
        subject_ref=case.subject_ref,
    )


def _serialize_case_detail(case: CaseDetail) -> CaseDetailResponse:
    return CaseDetailResponse(
        id=str(case.id),
        merchant_id=str(case.merchant_id),
        type=case.type,
        status=case.status,
        subject_ref=case.subject_ref,
        langgraph_thread_id=case.langgraph_thread_id,
        resolution=case.resolution,
        events=[
            CaseEventResponse(
                id=str(event.id),
                case_id=str(event.case_id),
                merchant_id=str(event.merchant_id),
                kind=event.kind,
                payload=event.payload,
                langsmith_run_id=event.langsmith_run_id,
                actor=event.actor,
                created_at=event.created_at,
            )
            for event in case.events
        ],
    )


def _serialize_fop(fop: FopSummary) -> FopResponse:
    return FopResponse(
        id=str(fop.id),
        merchant_id=str(fop.merchant_id),
        version=fop.version,
        nl_text=fop.nl_text,
        structured=fop.structured,
        status=fop.status,
        created_by=fop.created_by,
        created_at=fop.created_at,
    )


def _serialize_correction(correction: EvalCorrectionSummary) -> EvalCorrectionResponse:
    return EvalCorrectionResponse(
        id=str(correction.id),
        case_id=str(correction.case_id),
        merchant_id=str(correction.merchant_id),
        expected_resolution=correction.expected_resolution,
        notes=correction.notes,
        created_by=correction.created_by,
        status=correction.status,
    )


@router.get("", response_model=list[CaseSummaryResponse])
async def list_cases(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> list[CaseSummaryResponse]:
    cases = await repository.list_cases_for_merchant(tenant.merchant_id)
    return [_serialize_case(case) for case in cases]


@router.get("/{case_id}", response_model=CaseDetailResponse)
async def get_case_detail(
    case_id: UUID,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> CaseDetailResponse:
    case = await repository.get_case_detail(tenant.merchant_id, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    return _serialize_case_detail(case)


@router.post("/{case_id}/decision", response_model=CaseDecisionResponse)
async def submit_case_decision(
    case_id: UUID,
    request: CaseDecisionRequest,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[TenantRepository, Depends(get_tenant_repository)],
    dispatcher: Annotated[CaseDecisionDispatcher, Depends(get_case_decision_dispatcher)],
) -> CaseDecisionResponse:
    case = await repository.get_case_detail(tenant.merchant_id, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    if case.langgraph_thread_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Case has no LangGraph thread to resume.",
        )

    decision_payload = {
        "decision": request.decision,
        "source": request.source,
        "actor": request.actor,
        "note": request.note,
        "modification": request.modification,
    }
    resume_result = await dispatcher.resume_case(
        thread_id=case.langgraph_thread_id,
        merchant_id=tenant.merchant_id,
        case_id=case_id,
        decision=decision_payload,
    )
    next_status = (
        CaseStatus.PENDING_APPROVAL.value
        if request.decision == "modify"
        else CaseStatus.CANCELED.value
        if request.decision == "reject"
        else CaseStatus.EXECUTING.value
        if request.decision == "approve"
        else CaseStatus.OPEN.value
    )
    resolution: dict[str, object] = {
        "decision": request.decision,
        "source": request.source,
        "actor": request.actor,
        "note": request.note,
        "modification": request.modification,
        "langgraph_run_id": resume_result.run_id,
        "submitted_to_langgraph": resume_result.submitted,
        "execution": {
            "status": (
                "pending"
                if request.decision == "approve"
                else "skipped"
                if request.decision == "reject"
                else "awaiting_modification"
            )
        },
    }
    await repository.record_case_event(
        merchant_id=tenant.merchant_id,
        case_id=case_id,
        kind="case.decision_submitted",
        payload=resolution,
        actor=ActorType.HUMAN,
        langsmith_run_id=resume_result.run_id,
    )
    await repository.update_case_decision(
        merchant_id=tenant.merchant_id,
        case_id=case_id,
        status=next_status,
        resolution=resolution,
    )
    return CaseDecisionResponse(
        case_id=str(case_id),
        status=next_status,
        langgraph_run_id=resume_result.run_id,
        submitted_to_langgraph=resume_result.submitted,
    )


@router.post("/{case_id}/corrections", response_model=EvalCorrectionResponse)
async def record_eval_correction(
    case_id: UUID,
    request: EvalCorrectionRequest,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> EvalCorrectionResponse:
    case = await repository.get_case_detail(tenant.merchant_id, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    correction = await repository.record_eval_correction(
        merchant_id=tenant.merchant_id,
        case_id=case_id,
        expected_resolution=request.expected_resolution,
        notes=request.notes,
        created_by=tenant.actor_email or tenant.actor_user_id,
    )
    await repository.record_case_event(
        merchant_id=tenant.merchant_id,
        case_id=case_id,
        kind="case.eval_correction_recorded",
        payload={
            "correction_id": str(correction.id),
            "expected_resolution": correction.expected_resolution,
            "notes": correction.notes,
        },
        actor=ActorType.HUMAN,
    )
    return _serialize_correction(correction)


@router.get("/-/fops", response_model=list[FopResponse])
async def list_fops(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    repository: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> list[FopResponse]:
    fops = await repository.list_fops_for_merchant(tenant.merchant_id)
    return [_serialize_fop(fop) for fop in fops]
