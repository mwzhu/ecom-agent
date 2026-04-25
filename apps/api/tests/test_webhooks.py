from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from api.config import Settings, get_settings
from api.db.models import ActorType, ToolCallStatus
from api.integrations import IntegrationProvider
from api.integrations.base import JsonObject, ProviderCredential, ToolCallSnapshot
from api.integrations.dependencies import get_integration_repository
from api.main import app
from api.webhooks.dispatcher import WebhookDispatch, get_webhook_dispatcher


@dataclass
class InMemoryWebhookRepository:
    seen: set[tuple[IntegrationProvider, str]] = field(default_factory=set)
    webhook_sources: dict[tuple[IntegrationProvider, str], UUID] = field(default_factory=dict)
    scopes: list[UUID] = field(default_factory=list)
    processed: list[tuple[IntegrationProvider, str]] = field(default_factory=list)
    cases: list[dict[str, object]] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)

    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        self.scopes.append(merchant_id)

    async def get_credential(
        self,
        merchant_id: UUID,
        provider: IntegrationProvider,
    ) -> ProviderCredential:
        raise AssertionError("Webhook tests should not fetch provider credentials.")

    async def upsert_credential(
        self,
        merchant_id: UUID,
        provider: IntegrationProvider,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        raise AssertionError("Webhook tests should not upsert provider credentials.")

    async def get_tool_call(
        self,
        merchant_id: UUID,
        idempotency_key: str,
    ) -> ToolCallSnapshot | None:
        raise AssertionError("Webhook tests should not read tool calls.")

    async def create_tool_call(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        tool: str,
        input_payload: JsonObject,
        idempotency_key: str,
    ) -> ToolCallSnapshot:
        raise AssertionError("Webhook tests should not create tool calls.")

    async def finish_tool_call(
        self,
        tool_call_id: UUID,
        *,
        status: ToolCallStatus,
        output: JsonObject,
    ) -> None:
        raise AssertionError("Webhook tests should not finish tool calls.")

    async def record_case_event(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        kind: str,
        payload: JsonObject,
        actor: ActorType = ActorType.AGENT,
        langsmith_run_id: str | None = None,
    ) -> None:
        self.events.append(
            {
                "merchant_id": merchant_id,
                "case_id": case_id,
                "kind": kind,
                "payload": payload,
                "actor": actor.value,
                "langsmith_run_id": langsmith_run_id,
            }
        )

    async def resolve_webhook_merchant(
        self,
        *,
        provider: IntegrationProvider,
        external_account_id: str,
    ) -> UUID | None:
        return self.webhook_sources.get((provider, external_account_id))

    async def create_case_for_webhook(
        self,
        *,
        merchant_id: UUID,
        case_type: str,
        subject_ref: JsonObject,
        langgraph_thread_id: str,
    ) -> UUID:
        case_id = uuid4()
        self.cases.append(
            {
                "id": case_id,
                "merchant_id": merchant_id,
                "type": case_type,
                "subject_ref": subject_ref,
                "langgraph_thread_id": langgraph_thread_id,
            }
        )
        return case_id

    async def record_webhook_event(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        event_id: str,
        payload: JsonObject,
    ) -> bool:
        key = (provider, event_id)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    async def mark_webhook_processed(
        self,
        *,
        provider: IntegrationProvider,
        event_id: str,
    ) -> None:
        self.processed.append((provider, event_id))


@dataclass
class InMemoryDispatcher:
    runs: list[WebhookDispatch] = field(default_factory=list)

    async def create_thread(self) -> str:
        return "thread_webhook_1"

    async def trigger(self, dispatch: WebhookDispatch) -> str | None:
        self.runs.append(dispatch)
        return f"run_{dispatch.event_id}"


def test_shopify_webhook_verifies_hmac_dedupes_and_dispatches() -> None:
    secret = "shopify-webhook-secret"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.SHOPIFY, "demo.myshopify.com"): merchant_id}
    )
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher, Settings(shopify_webhook_secret=secret))
    body = json.dumps(
        {
            "topic": "orders/updated",
            "id": "gid://shopify/Order/1",
            "name": "#1001",
            "risk_score": 86,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-shopify-shop-domain": "demo.myshopify.com",
        "x-shopify-topic": "orders/updated",
        "x-shopify-webhook-id": "evt_shopify_1",
        "x-shopify-hmac-sha256": _shopify_signature(secret, body),
    }
    try:
        first = client.post("/v1/webhooks/shopify", content=body, headers=headers)
        second = client.post("/v1/webhooks/shopify", content=body, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload == {
        "provider": "shopify",
        "event_id": "evt_shopify_1",
        "status": "accepted",
        "case_id": first_payload["case_id"],
        "run_id": "run_evt_shopify_1",
    }
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert repository.scopes == [merchant_id, merchant_id]
    assert repository.processed == [(IntegrationProvider.SHOPIFY, "evt_shopify_1")]
    assert repository.cases[0]["type"] == "fraud_triage"
    assert repository.cases[0]["langgraph_thread_id"] == "thread_webhook_1"
    assert [event["kind"] for event in repository.events] == [
        "webhook.received",
        "agent.run_started",
    ]
    assert len(dispatcher.runs) == 1
    assert dispatcher.runs[0].case_id == repository.cases[0]["id"]
    assert dispatcher.runs[0].exception_type == "fraud_triage"


def test_shopify_webhook_rejects_bad_signature() -> None:
    client = _client(
        InMemoryWebhookRepository(),
        InMemoryDispatcher(),
        Settings(shopify_webhook_secret="shopify-webhook-secret"),
    )
    try:
        response = client.post(
            "/v1/webhooks/shopify",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "x-ecom-merchant-id": str(uuid4()),
                "x-shopify-webhook-id": "evt_shopify_2",
                "x-shopify-hmac-sha256": "bad",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_shopify_webhook_rejects_unmapped_source_even_with_merchant_header() -> None:
    secret = "shopify-webhook-secret"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository()
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher, Settings(shopify_webhook_secret=secret))
    body = json.dumps(
        {
            "topic": "orders/updated",
            "id": "gid://shopify/Order/1",
            "merchant_id": str(merchant_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")

    try:
        response = client.post(
            "/v1/webhooks/shopify",
            content=body,
            headers={
                "content-type": "application/json",
                "x-ecom-merchant-id": str(merchant_id),
                "x-shopify-shop-domain": "unknown.myshopify.com",
                "x-shopify-webhook-id": "evt_shopify_unmapped",
                "x-shopify-hmac-sha256": _shopify_signature(secret, body),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert repository.cases == []
    assert dispatcher.runs == []


def _client(
    repository: InMemoryWebhookRepository,
    dispatcher: InMemoryDispatcher,
    settings: Settings,
) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_integration_repository] = lambda: repository
    app.dependency_overrides[get_webhook_dispatcher] = lambda: dispatcher
    return TestClient(app)


def _shopify_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")
