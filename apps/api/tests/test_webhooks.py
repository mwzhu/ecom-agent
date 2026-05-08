from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

import api.webhooks.customer_intent as customer_intent
from api.config import Settings, get_settings
from api.db.models import ActorType, NormalizedEventSourceType, ToolCallStatus
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
    normalized: list[dict[str, object]] = field(default_factory=list)
    normalized_processed: list[tuple[str, UUID]] = field(default_factory=list)
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

    async def find_case_for_provider_order(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        order_id: str,
    ) -> UUID | None:
        for case in self.cases:
            subject_ref = case.get("subject_ref")
            if (
                case.get("merchant_id") == merchant_id
                and isinstance(subject_ref, dict)
                and subject_ref.get("provider") == provider.value
                and str(subject_ref.get("order_id")) == str(order_id)
            ):
                case_id = case.get("id")
                return case_id if isinstance(case_id, UUID) else None
        return None

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

    async def record_normalized_event(
        self,
        *,
        merchant_id: UUID,
        source_type: NormalizedEventSourceType,
        provider: IntegrationProvider | None,
        source_event_id: str,
        event_type: str,
        payload: JsonObject,
        dedupe_key: str,
    ) -> bool:
        self.normalized.append(
            {
                "merchant_id": merchant_id,
                "source_type": source_type.value,
                "provider": provider.value if provider else None,
                "source_event_id": source_event_id,
                "event_type": event_type,
                "payload": payload,
                "dedupe_key": dedupe_key,
            }
        )
        return True

    async def mark_normalized_event_processed(
        self,
        *,
        merchant_id: UUID,
        dedupe_key: str,
        case_id: UUID,
    ) -> None:
        self.normalized_processed.append((dedupe_key, case_id))

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
    assert repository.normalized[0]["source_type"] == "webhook"
    assert repository.normalized[0]["event_type"] == "orders/updated"
    assert repository.normalized_processed == [
        ("webhook:shopify:evt_shopify_1", repository.cases[0]["id"])
    ]
    assert [event["kind"] for event in repository.events] == [
        "webhook.received",
        "agent.run_started",
    ]
    assert len(dispatcher.runs) == 1
    assert dispatcher.runs[0].case_id == repository.cases[0]["id"]


def test_shopify_demo_order_update_webhook_is_ignored() -> None:
    secret = "shopify-webhook-secret"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.SHOPIFY, "demo.myshopify.com"): merchant_id}
    )
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher, Settings(shopify_webhook_secret=secret))
    body = json.dumps(
        {
            "id": "gid://shopify/Order/1",
            "name": "#1001",
            "tags": "flowlabs-demo, address-change-request, real-demo-001",
            "note": "Real demo: customer requested an address change before fulfillment.",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-shopify-shop-domain": "demo.myshopify.com",
        "x-shopify-topic": "orders/updated",
        "x-shopify-webhook-id": "evt_shopify_demo_update",
        "x-shopify-hmac-sha256": _shopify_signature(secret, body),
    }
    try:
        response = client.post("/v1/webhooks/shopify", content=body, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert repository.cases == []
    assert dispatcher.runs == []


def test_shopify_followup_webhook_updates_existing_case_without_new_run() -> None:
    secret = "shopify-webhook-secret"
    merchant_id = uuid4()
    existing_case_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.SHOPIFY, "demo.myshopify.com"): merchant_id},
        cases=[
            {
                "id": existing_case_id,
                "merchant_id": merchant_id,
                "type": "order_cancellation_request",
                "subject_ref": {
                    "provider": "shopify",
                    "order_id": 12345,
                    "order_name": "#1001",
                },
                "langgraph_thread_id": "thread_existing",
            }
        ],
    )
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher, Settings(shopify_webhook_secret=secret))
    body = json.dumps(
        {
            "id": 987,
            "order_id": 12345,
            "note": "Order canceled",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-shopify-shop-domain": "demo.myshopify.com",
        "x-shopify-topic": "refunds/create",
        "x-shopify-webhook-id": "evt_shopify_refund_followup",
        "x-shopify-hmac-sha256": _shopify_signature(secret, body),
    }
    try:
        response = client.post("/v1/webhooks/shopify", content=body, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "provider": "shopify",
        "event_id": "evt_shopify_refund_followup",
        "status": "updated_existing_case",
        "case_id": str(existing_case_id),
        "run_id": None,
    }
    assert len(repository.cases) == 1
    assert dispatcher.runs == []
    assert repository.events[0]["kind"] == "webhook.followup_received"
    assert repository.normalized_processed == [
        ("webhook:shopify:evt_shopify_refund_followup", existing_case_id)
    ]


def test_shopify_late_order_create_updates_existing_order_case_without_new_run() -> None:
    secret = "shopify-webhook-secret"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.SHOPIFY, "demo.myshopify.com"): merchant_id}
    )
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher, Settings(shopify_webhook_secret=secret))

    updated_body = json.dumps(
        {
            "topic": "orders/updated",
            "id": 12345,
            "name": "#1001",
            "risk_score": 86,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    updated_headers = {
        "content-type": "application/json",
        "x-shopify-shop-domain": "demo.myshopify.com",
        "x-shopify-topic": "orders/updated",
        "x-shopify-webhook-id": "evt_shopify_order_updated_first",
        "x-shopify-hmac-sha256": _shopify_signature(secret, updated_body),
    }
    create_body = json.dumps(
        {
            "topic": "orders/create",
            "id": 12345,
            "name": "#1001",
            "risk_score": 86,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    create_headers = {
        "content-type": "application/json",
        "x-shopify-shop-domain": "demo.myshopify.com",
        "x-shopify-topic": "orders/create",
        "x-shopify-webhook-id": "evt_shopify_order_create_late",
        "x-shopify-hmac-sha256": _shopify_signature(secret, create_body),
    }

    try:
        first = client.post("/v1/webhooks/shopify", content=updated_body, headers=updated_headers)
        second = client.post("/v1/webhooks/shopify", content=create_body, headers=create_headers)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    assert second.status_code == 200
    assert second.json() == {
        "provider": "shopify",
        "event_id": "evt_shopify_order_create_late",
        "status": "updated_existing_case",
        "case_id": str(repository.cases[0]["id"]),
        "run_id": None,
    }
    assert len(repository.cases) == 1
    assert len(dispatcher.runs) == 1
    assert [event["kind"] for event in repository.events] == [
        "webhook.received",
        "agent.run_started",
        "webhook.followup_received",
    ]
    assert repository.normalized_processed == [
        ("webhook:shopify:evt_shopify_order_updated_first", repository.cases[0]["id"]),
        ("webhook:shopify:evt_shopify_order_create_late", repository.cases[0]["id"]),
    ]


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


def test_shopify_webhook_accepts_previous_secret_during_rotation() -> None:
    current_secret = "shopify-current-secret"
    previous_secret = "shopify-previous-secret"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.SHOPIFY, "demo.myshopify.com"): merchant_id}
    )
    dispatcher = InMemoryDispatcher()
    client = _client(
        repository,
        dispatcher,
        Settings(
            shopify_webhook_secret=current_secret,
            shopify_previous_webhook_secret=previous_secret,
        ),
    )
    body = json.dumps({"id": "gid://shopify/Order/2"}, separators=(",", ":")).encode("utf-8")

    try:
        response = client.post(
            "/v1/webhooks/shopify",
            content=body,
            headers={
                "content-type": "application/json",
                "x-shopify-shop-domain": "demo.myshopify.com",
                "x-shopify-webhook-id": "evt_shopify_rotating",
                "x-shopify-hmac-sha256": _shopify_signature(previous_secret, body),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


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


def test_stripe_webhook_uses_configured_account_id_when_event_has_no_account() -> None:
    secret = "whsec_test"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.STRIPE, "acct_123"): merchant_id}
    )
    dispatcher = InMemoryDispatcher()
    client = _client(
        repository,
        dispatcher,
        Settings(stripe_webhook_secret=secret, stripe_account_id="ACCT_123"),
    )
    body = json.dumps(
        {
            "id": "evt_stripe_1",
            "type": "charge.dispute.created",
            "data": {
                "object": {
                    "id": "dp_123",
                    "charge": "ch_123",
                    "payment_intent": "pi_123",
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")

    try:
        response = client.post(
            "/v1/webhooks/stripe",
            content=body,
            headers={
                "content-type": "application/json",
                "stripe-signature": _stripe_signature(secret, body),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert repository.scopes == [merchant_id]
    assert repository.cases[0]["merchant_id"] == merchant_id
    assert len(dispatcher.runs) == 1


def test_gorgias_webhook_accepts_static_shared_secret_for_http_integration() -> None:
    secret = "gorgias-static-secret"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.GORGIAS, "flow-labs-2.gorgias.com"): merchant_id}
    )
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher, Settings(gorgias_webhook_secret=secret))
    body = json.dumps(
        {
            "id": 123,
            "ticket": {
                "id": 123,
                "subject": "Need to change my address",
                "customer": {"email": "buyer@example.com"},
            },
            "customer_request": {"type": "change_address"},
        },
        separators=(",", ":"),
    ).encode("utf-8")

    try:
        response = client.post(
            "/v1/webhooks/gorgias",
            content=body,
            headers={
                "content-type": "application/json",
                "x-gorgias-domain": "flow-labs-2.gorgias.com",
                "x-ecom-webhook-secret": secret,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert repository.scopes == [merchant_id]
    assert repository.cases[0]["type"] == "address_change_request"
    assert len(dispatcher.runs) == 1


def test_gorgias_ticket_text_extracts_address_change_slots(monkeypatch) -> None:
    llm_calls = 0

    async def fake_invoke_json(**kwargs: object) -> dict[str, object]:
        nonlocal llm_calls
        llm_calls += 1
        body = kwargs["body"]
        assert isinstance(body, dict)
        if body.get("workflow") == "address_change_request":
            return {
                "requested_address": {
                    "address1": "515 Valencia St",
                    "address2": None,
                    "city": "San Francisco",
                    "province": "CA",
                    "zip": "94110",
                    "country": "US",
                },
                "order_reference": "#1036",
                "is_complete": True,
                "needs_clarification": False,
                "confidence": 0.92,
                "evidence": ["Message contains street, city, state, and ZIP."],
            }
        return {
            "intent": "address_change_request",
            "confidence": 0.91,
            "order_reference": "#1036",
            "evidence": ["Customer asked to change the shipping address."],
            "needs_human_triage": False,
        }

    monkeypatch.setenv("ORDER_EXCEPTION_CUSTOMER_LANGUAGE_LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(customer_intent, "_invoke_json_async", fake_invoke_json)
    secret = "gorgias-static-secret"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.GORGIAS, "flow-labs-2.gorgias.com"): merchant_id}
    )
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher, Settings(gorgias_webhook_secret=secret))
    body = json.dumps(
        {
            "id": 1036,
            "ticket": {
                "id": 1036,
                "subject": "Address change for #1036",
                "excerpt": (
                    "Hi, I just placed order #1036. Can you change the shipping address "
                    "to 515 Valencia St, San Francisco, CA 94110 before it ships?"
                ),
                "customer": {"email": "buyer@example.com"},
            },
            "order": {
                "id": "gid://shopify/Order/1036",
                "name": "#1036",
                "email": "buyer@example.com",
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")

    try:
        response = client.post(
            "/v1/webhooks/gorgias",
            content=body,
            headers={
                "content-type": "application/json",
                "x-gorgias-domain": "flow-labs-2.gorgias.com",
                "x-ecom-webhook-secret": secret,
            },
        )
        duplicate = client.post(
            "/v1/webhooks/gorgias",
            content=body,
            headers={
                "content-type": "application/json",
                "x-gorgias-domain": "flow-labs-2.gorgias.com",
                "x-ecom-webhook-secret": secret,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert llm_calls == 2
    assert repository.cases[0]["type"] == "address_change_request"
    request = dispatcher.runs[0].context["customer_request"]
    assert request["type"] == "address_change_request"
    assert request["requested_address"] == {
        "address1": "515 Valencia St",
        "address2": None,
        "city": "San Francisco",
        "province": "CA",
        "zip": "94110",
        "country": "US",
    }
    assert request["is_complete"] is True
    assert request["needs_clarification"] is False


def test_gorgias_webhook_accepts_body_shared_secret_for_http_integration() -> None:
    secret = "gorgias-static-secret"
    merchant_id = uuid4()
    repository = InMemoryWebhookRepository(
        webhook_sources={(IntegrationProvider.GORGIAS, "flow-labs-2.gorgias.com"): merchant_id}
    )
    dispatcher = InMemoryDispatcher()
    client = _client(repository, dispatcher, Settings(gorgias_webhook_secret=secret))
    body = json.dumps(
        {
            "id": 123,
            "webhook_secret": secret,
            "gorgias_domain": "flow-labs-2.gorgias.com",
            "ticket": {
                "id": 123,
                "subject": "Need to change my address",
                "customer": {"email": "buyer@example.com"},
            },
            "customer_request": {"type": "change_address"},
        },
        separators=(",", ":"),
    ).encode("utf-8")

    try:
        response = client.post(
            "/v1/webhooks/gorgias",
            content=body,
            headers={
                "content-type": "application/json",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert repository.cases[0]["type"] == "address_change_request"


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


def _stripe_signature(secret: str, body: bytes) -> str:
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
