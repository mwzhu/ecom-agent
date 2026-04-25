from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import jwt
from fastapi.testclient import TestClient

from api.auth.clerk import ClerkAuthenticator, get_clerk_authenticator
from api.config import Settings
from api.db import CaseSummary, MerchantIdentity, TenantRepository, get_tenant_repository
from api.main import app

DEV_JWT_SECRET = "local-test-secret-for-hs256-signatures"


@dataclass
class InMemoryTenantRepository:
    merchants: dict[str, MerchantIdentity]
    cases: list[CaseSummary]
    scopes: list[UUID]

    async def get_merchant_by_clerk_org_id(self, clerk_org_id: str) -> MerchantIdentity | None:
        return self.merchants.get(clerk_org_id)

    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        self.scopes.append(merchant_id)

    async def list_cases_for_merchant(self, merchant_id: UUID) -> list[CaseSummary]:
        return [case for case in self.cases if case.merchant_id == merchant_id]


def _token_for_org(org_id: str, subject: str = "user_123") -> str:
    return jwt.encode(
        {"org_id": org_id, "sub": subject, "email": "ops@example.com"},
        key=DEV_JWT_SECRET,
        algorithm="HS256",
    )


def _auth_headers(org_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token_for_org(org_id)}"}


def _repo() -> InMemoryTenantRepository:
    merchant_a = MerchantIdentity(
        id=uuid4(),
        clerk_org_id="org_a",
        name="Merchant A",
        tier="starter",
    )
    merchant_b = MerchantIdentity(
        id=uuid4(),
        clerk_org_id="org_b",
        name="Merchant B",
        tier="growth",
    )
    return InMemoryTenantRepository(
        merchants={
            merchant_a.clerk_org_id: merchant_a,
            merchant_b.clerk_org_id: merchant_b,
        },
        cases=[
            CaseSummary(
                id=uuid4(),
                merchant_id=merchant_a.id,
                type="fraud_triage",
                status="open",
                subject_ref={"order_id": "A-1001"},
            ),
            CaseSummary(
                id=uuid4(),
                merchant_id=merchant_b.id,
                type="inventory_conflict",
                status="open",
                subject_ref={"order_id": "B-2001"},
            ),
        ],
        scopes=[],
    )


def _client_with_repo(repository: TenantRepository) -> TestClient:
    app.dependency_overrides[get_tenant_repository] = lambda: repository
    app.dependency_overrides[get_clerk_authenticator] = lambda: ClerkAuthenticator(
        Settings(
            clerk_allow_unverified_jwt=True,
            clerk_dev_jwt_secret=DEV_JWT_SECRET,
        )
    )
    return TestClient(app)


def test_clerk_org_maps_to_merchant_context() -> None:
    repository = _repo()
    client = _client_with_repo(repository)
    try:
        response = client.get("/v1/me", headers=_auth_headers("org_a"))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["clerk_org_id"] == "org_a"
    assert response.json()["merchant_name"] == "Merchant A"
    assert response.json()["actor_user_id"] == "user_123"
    assert repository.scopes == [repository.merchants["org_a"].id]


def test_tenant_scoping_prevents_cross_reads() -> None:
    repository = _repo()
    client = _client_with_repo(repository)
    try:
        merchant_a_response = client.get("/v1/cases", headers=_auth_headers("org_a"))
        merchant_b_response = client.get("/v1/cases", headers=_auth_headers("org_b"))
    finally:
        app.dependency_overrides.clear()

    assert merchant_a_response.status_code == 200
    assert merchant_b_response.status_code == 200
    assert [case["subject_ref"]["order_id"] for case in merchant_a_response.json()] == ["A-1001"]
    assert [case["subject_ref"]["order_id"] for case in merchant_b_response.json()] == ["B-2001"]


def test_unknown_clerk_org_is_forbidden() -> None:
    client = _client_with_repo(_repo())
    try:
        response = client.get("/v1/me", headers=_auth_headers("org_missing"))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_missing_bearer_token_is_unauthorized() -> None:
    client = _client_with_repo(_repo())
    try:
        response = client.get("/v1/me")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
