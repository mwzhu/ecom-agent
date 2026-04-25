from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import ActorType, Case, CaseEvent, EvalCorrection, EvalReviewItem, Fop, Merchant
from api.db.session import get_async_session


@dataclass(frozen=True)
class MerchantIdentity:
    id: UUID
    clerk_org_id: str
    name: str
    tier: str


@dataclass(frozen=True)
class CaseSummary:
    id: UUID
    merchant_id: UUID
    type: str
    status: str
    subject_ref: dict[str, object]


@dataclass(frozen=True)
class CaseEventSummary:
    id: UUID
    case_id: UUID
    merchant_id: UUID
    kind: str
    payload: dict[str, object]
    langsmith_run_id: str | None
    actor: str
    created_at: str


@dataclass(frozen=True)
class CaseDetail:
    id: UUID
    merchant_id: UUID
    type: str
    status: str
    subject_ref: dict[str, object]
    langgraph_thread_id: str | None
    resolution: dict[str, object] | None
    events: list[CaseEventSummary]


@dataclass(frozen=True)
class FopSummary:
    id: UUID
    merchant_id: UUID
    version: int
    nl_text: str
    structured: dict[str, object]
    status: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class EvalCorrectionSummary:
    id: UUID
    case_id: UUID
    merchant_id: UUID
    expected_resolution: dict[str, object]
    notes: str
    created_by: str
    status: str


@dataclass(frozen=True)
class EvalReviewSummary:
    id: UUID
    case_id: UUID
    merchant_id: UUID
    langsmith_run_id: str | None
    score: int
    passed: bool
    reason: str
    payload: dict[str, object]
    status: str
    created_at: str


class TenantRepository(Protocol):
    async def get_merchant_by_clerk_org_id(self, clerk_org_id: str) -> MerchantIdentity | None:
        ...

    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        ...

    async def list_cases_for_merchant(self, merchant_id: UUID) -> list[CaseSummary]:
        ...

    async def get_case_detail(self, merchant_id: UUID, case_id: UUID) -> CaseDetail | None:
        ...

    async def list_fops_for_merchant(self, merchant_id: UUID) -> list[FopSummary]:
        ...

    async def record_case_event(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        kind: str,
        payload: dict[str, object],
        actor: ActorType,
        langsmith_run_id: str | None = None,
    ) -> None:
        ...

    async def update_case_decision(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        status: str,
        resolution: dict[str, object],
    ) -> None:
        ...

    async def record_eval_correction(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        expected_resolution: dict[str, object],
        notes: str,
        created_by: str,
    ) -> EvalCorrectionSummary:
        ...

    async def list_eval_reviews_for_merchant(
        self,
        merchant_id: UUID,
        *,
        status: str = "queued",
    ) -> list[EvalReviewSummary]:
        ...

    async def queue_eval_review_item(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        langsmith_run_id: str | None,
        score: int,
        passed: bool,
        reason: str,
        payload: dict[str, object],
    ) -> EvalReviewSummary:
        ...


class SqlAlchemyTenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_merchant_by_clerk_org_id(self, clerk_org_id: str) -> MerchantIdentity | None:
        result = await self._session.execute(
            select(Merchant).where(Merchant.clerk_org_id == clerk_org_id)
        )
        merchant = result.scalar_one_or_none()
        if merchant is None:
            return None
        return MerchantIdentity(
            id=merchant.id,
            clerk_org_id=merchant.clerk_org_id,
            name=merchant.name,
            tier=merchant.tier,
        )

    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        await self._session.execute(
            text("select set_config('app.merchant_id', :merchant_id, true)"),
            {"merchant_id": str(merchant_id)},
        )

    async def list_cases_for_merchant(self, merchant_id: UUID) -> list[CaseSummary]:
        result = await self._session.execute(
            select(Case)
            .where(Case.merchant_id == merchant_id)
            .order_by(Case.created_at.desc(), Case.id)
        )
        return [
            CaseSummary(
                id=case.id,
                merchant_id=case.merchant_id,
                type=case.type,
                status=case.status,
                subject_ref=dict(case.subject_ref),
            )
            for case in result.scalars()
        ]

    async def get_case_detail(self, merchant_id: UUID, case_id: UUID) -> CaseDetail | None:
        result = await self._session.execute(
            select(Case).where(Case.merchant_id == merchant_id, Case.id == case_id)
        )
        case = result.scalar_one_or_none()
        if case is None:
            return None

        events_result = await self._session.execute(
            select(CaseEvent)
            .where(CaseEvent.merchant_id == merchant_id, CaseEvent.case_id == case_id)
            .order_by(CaseEvent.created_at.asc(), CaseEvent.id)
        )
        events = [
            CaseEventSummary(
                id=event.id,
                case_id=event.case_id,
                merchant_id=event.merchant_id,
                kind=event.kind,
                payload=dict(event.payload),
                langsmith_run_id=event.langsmith_run_id,
                actor=event.actor,
                created_at=event.created_at.isoformat(),
            )
            for event in events_result.scalars()
        ]
        resolution = dict(case.resolution) if isinstance(case.resolution, dict) else None
        return CaseDetail(
            id=case.id,
            merchant_id=case.merchant_id,
            type=case.type,
            status=case.status,
            subject_ref=dict(case.subject_ref),
            langgraph_thread_id=case.langgraph_thread_id,
            resolution=resolution,
            events=events,
        )

    async def list_fops_for_merchant(self, merchant_id: UUID) -> list[FopSummary]:
        result = await self._session.execute(
            select(Fop)
            .where(Fop.merchant_id == merchant_id)
            .order_by(Fop.created_at.desc(), Fop.id)
        )
        return [
            FopSummary(
                id=fop.id,
                merchant_id=fop.merchant_id,
                version=fop.version,
                nl_text=fop.nl_text,
                structured=dict(fop.structured),
                status=fop.status,
                created_by=fop.created_by,
                created_at=fop.created_at.isoformat(),
            )
            for fop in result.scalars()
        ]

    async def record_case_event(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        kind: str,
        payload: dict[str, object],
        actor: ActorType,
        langsmith_run_id: str | None = None,
    ) -> None:
        self._session.add(
            CaseEvent(
                merchant_id=merchant_id,
                case_id=case_id,
                kind=kind,
                payload=payload,
                actor=actor.value,
                langsmith_run_id=langsmith_run_id,
            )
        )
        await self._session.flush()

    async def update_case_decision(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        status: str,
        resolution: dict[str, object],
    ) -> None:
        case = await self._session.get(Case, case_id)
        if case is None or case.merchant_id != merchant_id:
            return
        case.status = status
        case.resolution = resolution
        await self._session.flush()

    async def record_eval_correction(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        expected_resolution: dict[str, object],
        notes: str,
        created_by: str,
    ) -> EvalCorrectionSummary:
        correction = EvalCorrection(
            merchant_id=merchant_id,
            case_id=case_id,
            expected_resolution=expected_resolution,
            notes=notes,
            created_by=created_by,
        )
        self._session.add(correction)
        await self._session.flush()
        return EvalCorrectionSummary(
            id=correction.id,
            case_id=correction.case_id,
            merchant_id=correction.merchant_id,
            expected_resolution=dict(correction.expected_resolution),
            notes=correction.notes,
            created_by=correction.created_by,
            status=correction.status,
        )

    async def list_eval_reviews_for_merchant(
        self,
        merchant_id: UUID,
        *,
        status: str = "queued",
    ) -> list[EvalReviewSummary]:
        result = await self._session.execute(
            select(EvalReviewItem)
            .where(EvalReviewItem.merchant_id == merchant_id, EvalReviewItem.status == status)
            .order_by(EvalReviewItem.created_at.desc(), EvalReviewItem.id)
        )
        return [_serialize_eval_review(item) for item in result.scalars()]

    async def queue_eval_review_item(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        langsmith_run_id: str | None,
        score: int,
        passed: bool,
        reason: str,
        payload: dict[str, object],
    ) -> EvalReviewSummary:
        if langsmith_run_id is not None:
            existing = await self._session.execute(
                select(EvalReviewItem).where(
                    EvalReviewItem.merchant_id == merchant_id,
                    EvalReviewItem.case_id == case_id,
                    EvalReviewItem.langsmith_run_id == langsmith_run_id,
                )
            )
            existing_review = existing.scalar_one_or_none()
            if existing_review is not None:
                return _serialize_eval_review(existing_review)

        review = EvalReviewItem(
            merchant_id=merchant_id,
            case_id=case_id,
            langsmith_run_id=langsmith_run_id,
            score=score,
            passed=passed,
            reason=reason,
            payload=payload,
        )
        self._session.add(review)
        await self._session.flush()
        return _serialize_eval_review(review)


def _serialize_eval_review(item: EvalReviewItem) -> EvalReviewSummary:
    return EvalReviewSummary(
        id=item.id,
        case_id=item.case_id,
        merchant_id=item.merchant_id,
        langsmith_run_id=item.langsmith_run_id,
        score=item.score,
        passed=item.passed,
        reason=item.reason,
        payload=dict(item.payload),
        status=item.status,
        created_at=item.created_at.isoformat(),
    )


def get_tenant_repository(
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> TenantRepository:
    return SqlAlchemyTenantRepository(session)
