from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import jwt
from fastapi.testclient import TestClient

from api.agents import ResumeResult, get_case_decision_dispatcher
from api.auth.clerk import ClerkAuthenticator, get_clerk_authenticator
from api.config import Settings, get_settings
from api.db import (
    CaseDetail,
    CaseEventSummary,
    CaseSummary,
    EvalCorrectionSummary,
    EvalReviewSummary,
    FopSummary,
    MerchantIdentity,
    TenantRepository,
    get_tenant_repository,
)
from api.db.models import ActorType
from api.main import app

DEV_JWT_SECRET = "local-test-secret-for-hs256-signatures"


@dataclass
class InMemoryCaseRepository:
    merchant: MerchantIdentity
    case: CaseDetail
    events: list[dict[str, object]] = field(default_factory=list)
    eval_reviews: list[EvalReviewSummary] = field(default_factory=list)
    resolution: dict[str, object] | None = None
    status: str = "pending_approval"

    async def get_merchant_by_clerk_org_id(self, clerk_org_id: str) -> MerchantIdentity | None:
        return self.merchant if clerk_org_id == self.merchant.clerk_org_id else None

    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        return None

    async def list_cases_for_merchant(self, merchant_id: UUID) -> list[CaseSummary]:
        return [
            CaseSummary(
                id=self.case.id,
                merchant_id=self.case.merchant_id,
                type=self.case.type,
                status=self.status,
                subject_ref=self.case.subject_ref,
            )
        ]

    async def get_case_detail(self, merchant_id: UUID, case_id: UUID) -> CaseDetail | None:
        if merchant_id != self.case.merchant_id or case_id != self.case.id:
            return None
        return CaseDetail(
            id=self.case.id,
            merchant_id=self.case.merchant_id,
            type=self.case.type,
            status=self.status,
            subject_ref=self.case.subject_ref,
            langgraph_thread_id=self.case.langgraph_thread_id,
            resolution=self.resolution,
            events=self.case.events,
        )

    async def list_fops_for_merchant(self, merchant_id: UUID) -> list[FopSummary]:
        return []

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
        self.events.append(
            {
                "kind": kind,
                "payload": payload,
                "actor": actor.value,
                "langsmith_run_id": langsmith_run_id,
            }
        )

    async def update_case_decision(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        status: str,
        resolution: dict[str, object],
    ) -> None:
        self.status = status
        self.resolution = {**(self.resolution or {}), **resolution}

    async def record_eval_correction(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        expected_resolution: dict[str, object],
        notes: str,
        created_by: str,
    ) -> EvalCorrectionSummary:
        return EvalCorrectionSummary(
            id=uuid4(),
            case_id=case_id,
            merchant_id=merchant_id,
            expected_resolution=expected_resolution,
            notes=notes,
            created_by=created_by,
            status="queued",
        )

    async def list_eval_reviews_for_merchant(
        self,
        merchant_id: UUID,
        *,
        status: str = "queued",
    ) -> list[EvalReviewSummary]:
        return [
            review
            for review in self.eval_reviews
            if review.merchant_id == merchant_id and review.status == status
        ]

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
        review = EvalReviewSummary(
            id=uuid4(),
            case_id=case_id,
            merchant_id=merchant_id,
            langsmith_run_id=langsmith_run_id,
            score=score,
            passed=passed,
            reason=reason,
            payload=payload,
            status="queued",
            created_at="2026-04-17T00:00:00+00:00",
        )
        self.eval_reviews.append(review)
        return review


@dataclass
class InMemoryDispatcher:
    decisions: list[dict[str, object]] = field(default_factory=list)

    async def resume_case(
        self,
        *,
        thread_id: str | None,
        merchant_id: UUID,
        case_id: UUID,
        decision: dict[str, object],
    ) -> ResumeResult:
        self.decisions.append(
            {
                "thread_id": thread_id,
                "merchant_id": merchant_id,
                "case_id": case_id,
                "decision": decision,
            }
        )
        return ResumeResult(run_id="run_123", submitted=True)


def test_case_decision_records_event_and_resumes_langgraph() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    repository = _repo(merchant_id, case_id)
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher)

    try:
        response = client.post(
            f"/v1/cases/{case_id}/decision",
            headers=_auth_headers("org_a"),
            json={"decision": "approve", "actor": "ops@example.com"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "executing"
    assert response.json()["langgraph_run_id"] == "run_123"
    assert dispatcher.decisions[0]["thread_id"] == "thread_123"
    assert dispatcher.decisions[0]["merchant_id"] == merchant_id
    assert dispatcher.decisions[0]["case_id"] == case_id
    assert repository.events[0]["kind"] == "case.decision_submitted"
    assert repository.status == "executing"


def test_case_decision_rejects_case_without_langgraph_thread() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    repository = _repo(merchant_id, case_id, thread_id=None)
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher)

    try:
        response = client.post(
            f"/v1/cases/{case_id}/decision",
            headers=_auth_headers("org_a"),
            json={"decision": "approve", "actor": "ops@example.com"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert dispatcher.decisions == []
    assert repository.events == []
    assert repository.status == "pending_approval"


def test_eval_correction_is_recorded_for_case() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    repository = _repo(merchant_id, case_id)
    client = _client(repository, InMemoryDispatcher())

    try:
        response = client.post(
            f"/v1/cases/{case_id}/corrections",
            headers=_auth_headers("org_a"),
            json={
                "expected_resolution": {"status": "hold_for_review"},
                "notes": "Should not have auto-approved.",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["created_by"] == "ops@example.com"
    assert repository.events[0]["kind"] == "case.eval_correction_recorded"


def test_online_eval_review_is_queued_for_admin_panel() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    repository = _repo(merchant_id, case_id)
    client = _client(repository, InMemoryDispatcher(), online_eval_secret="test-secret")

    try:
        response = client.post(
            "/v1/evals/online-review",
            headers={"X-Ecom-Internal-Secret": "test-secret"},
            json={
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "langsmith_run_id": "run_low_confidence",
                "score": 2,
                "passed": False,
                "reason": "Judge found missing approval gate.",
                "payload": {"unsafe_actions": ["refund without approval"]},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["score"] == 2
    assert repository.eval_reviews[0].langsmith_run_id == "run_low_confidence"
    assert repository.events[0]["kind"] == "eval.online_low_confidence"


def test_online_eval_review_rejects_when_secret_missing() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    repository = _repo(merchant_id, case_id)
    client = _client(repository, InMemoryDispatcher(), online_eval_secret=None)

    try:
        response = client.post(
            "/v1/evals/online-review",
            json={
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "langsmith_run_id": "run_missing_secret",
                "score": 2,
                "passed": False,
                "reason": "Judge flag.",
                "payload": {},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert repository.eval_reviews == []


def test_online_eval_review_rejects_wrong_secret() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    repository = _repo(merchant_id, case_id)
    client = _client(repository, InMemoryDispatcher(), online_eval_secret="test-secret")

    try:
        response = client.post(
            "/v1/evals/online-review",
            headers={"X-Ecom-Internal-Secret": "wrong-secret"},
            json={
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "langsmith_run_id": "run_wrong_secret",
                "score": 2,
                "passed": False,
                "reason": "Judge flag.",
                "payload": {},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert repository.eval_reviews == []


def test_eval_review_queue_is_tenant_scoped() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    repository = _repo(merchant_id, case_id)
    repository.eval_reviews.append(
        EvalReviewSummary(
            id=uuid4(),
            case_id=case_id,
            merchant_id=merchant_id,
            langsmith_run_id="run_low_confidence",
            score=3,
            passed=False,
            reason="Low judge score.",
            payload={"scenario_id": "fraud_high_score_cancel_refund"},
            status="queued",
            created_at="2026-04-17T00:00:00+00:00",
        )
    )
    client = _client(repository, InMemoryDispatcher())

    try:
        response = client.get("/v1/evals/review-queue", headers=_auth_headers("org_a"))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["case_id"] == str(case_id)
    assert response.json()[0]["reason"] == "Low judge score."


def _repo(
    merchant_id: UUID,
    case_id: UUID,
    *,
    thread_id: str | None = "thread_123",
) -> InMemoryCaseRepository:
    merchant = MerchantIdentity(
        id=merchant_id,
        clerk_org_id="org_a",
        name="Merchant A",
        tier="starter",
    )
    case = CaseDetail(
        id=case_id,
        merchant_id=merchant_id,
        type="fraud_triage",
        status="pending_approval",
        subject_ref={"order_id": "A-1001"},
        langgraph_thread_id=thread_id,
        resolution=None,
        events=[
            CaseEventSummary(
                id=uuid4(),
                case_id=case_id,
                merchant_id=merchant_id,
                kind="agent.proposal",
                payload={"summary": "Cancel and refund."},
                langsmith_run_id="trace_123",
                actor="agent",
                created_at="2026-04-17T00:00:00+00:00",
            )
        ],
    )
    return InMemoryCaseRepository(merchant=merchant, case=case)


def _client(
    repository: TenantRepository,
    dispatcher: InMemoryDispatcher,
    *,
    online_eval_secret: str | None = "test-secret",
) -> TestClient:
    app.dependency_overrides[get_tenant_repository] = lambda: repository
    app.dependency_overrides[get_case_decision_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_clerk_authenticator] = lambda: ClerkAuthenticator(
        Settings(
            clerk_allow_unverified_jwt=True,
            clerk_dev_jwt_secret=DEV_JWT_SECRET,
        )
    )
    app.dependency_overrides[get_settings] = lambda: Settings(
        clerk_allow_unverified_jwt=True,
        clerk_dev_jwt_secret=DEV_JWT_SECRET,
        online_eval_webhook_secret=online_eval_secret,
    )
    return TestClient(app)


def _token_for_org(org_id: str, subject: str = "user_123") -> str:
    return jwt.encode(
        {"org_id": org_id, "sub": subject, "email": "ops@example.com"},
        key=DEV_JWT_SECRET,
        algorithm="HS256",
    )


def _auth_headers(org_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token_for_org(org_id)}"}
