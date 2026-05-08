from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import pytest

from api.db.models import ActorType, ToolCallStatus
from api.integrations.base import (
    IntegrationErrorKind,
    IntegrationProvider,
    JsonObject,
    ProviderCredential,
    ToolCallSnapshot,
    ToolRequest,
    execute_integration_tool,
)


@dataclass
class InMemoryIntegrationRepository:
    credential: ProviderCredential
    tool_calls: dict[str, ToolCallSnapshot] = field(default_factory=dict)
    outputs_by_id: dict[UUID, JsonObject] = field(default_factory=dict)
    scopes: list[UUID] = field(default_factory=list)
    events: list[JsonObject] = field(default_factory=list)

    async def set_merchant_scope(self, merchant_id: UUID) -> None:
        self.scopes.append(merchant_id)

    async def get_credential(
        self,
        merchant_id: UUID,
        provider: IntegrationProvider,
    ) -> ProviderCredential:
        return self.credential

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
        self.credential = ProviderCredential(
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            metadata=metadata or {},
        )

    async def get_tool_call(
        self,
        merchant_id: UUID,
        idempotency_key: str,
    ) -> ToolCallSnapshot | None:
        return self.tool_calls.get(idempotency_key)

    async def create_tool_call(
        self,
        *,
        merchant_id: UUID,
        case_id: UUID,
        tool: str,
        input_payload: JsonObject,
        idempotency_key: str,
    ) -> ToolCallSnapshot:
        snapshot = ToolCallSnapshot(
            id=uuid4(),
            status=ToolCallStatus.PENDING.value,
            output=None,
        )
        self.tool_calls[idempotency_key] = snapshot
        return snapshot

    async def finish_tool_call(
        self,
        tool_call_id: UUID,
        *,
        status: ToolCallStatus,
        output: JsonObject,
    ) -> None:
        self.outputs_by_id[tool_call_id] = output
        for key, snapshot in self.tool_calls.items():
            if snapshot.id == tool_call_id:
                self.tool_calls[key] = ToolCallSnapshot(
                    id=snapshot.id,
                    status=status.value,
                    output=output,
                )
                return

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
        return None

    async def create_case_for_webhook(
        self,
        *,
        merchant_id: UUID,
        case_type: str,
        subject_ref: JsonObject,
        langgraph_thread_id: str,
    ) -> UUID:
        return uuid4()

    async def record_webhook_event(
        self,
        *,
        merchant_id: UUID,
        provider: IntegrationProvider,
        event_id: str,
        payload: JsonObject,
    ) -> bool:
        return True

    async def mark_webhook_processed(
        self,
        *,
        provider: IntegrationProvider,
        event_id: str,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_execute_integration_tool_logs_success_and_skips_replay() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    calls = 0
    repository = InMemoryIntegrationRepository(
        credential=ProviderCredential(
            provider=IntegrationProvider.STRIPE,
            access_token="sk_test",
            refresh_token=None,
            expires_at=None,
            metadata={},
        )
    )
    request = ToolRequest(
        merchant_id=merchant_id,
        case_id=case_id,
        idempotency_key="case-1:get-charge",
    )

    async def operation(_: ProviderCredential) -> JsonObject:
        nonlocal calls
        calls += 1
        return {"id": "ch_123"}

    first = await execute_integration_tool(
        repository=repository,
        provider=IntegrationProvider.STRIPE,
        tool_name="stripe_get_charge",
        request=request,
        operation=operation,
    )
    second = await execute_integration_tool(
        repository=repository,
        provider=IntegrationProvider.STRIPE,
        tool_name="stripe_get_charge",
        request=request,
        operation=operation,
    )

    assert first.status == "succeeded"
    assert first.data == {"id": "ch_123"}
    assert second.status == "skipped"
    assert second.data == {"id": "ch_123"}
    assert calls == 1
    assert repository.scopes == [merchant_id, merchant_id]
    assert repository.events[0]["kind"] == "tool_call.succeeded"


@pytest.mark.asyncio
async def test_execute_integration_tool_normalizes_rate_limit_errors() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    repository = InMemoryIntegrationRepository(
        credential=ProviderCredential(
            provider=IntegrationProvider.SHOPIFY,
            access_token="shpat_test",
            refresh_token=None,
            expires_at=None,
            metadata={"shop_domain": "demo.myshopify.com"},
        )
    )
    request = ToolRequest(merchant_id=merchant_id, case_id=case_id)

    async def operation(_: ProviderCredential) -> JsonObject:
        request_info = httpx.Request("GET", "https://example.test")
        response = httpx.Response(
            429,
            request=request_info,
            headers={"retry-after": "2"},
            text="slow down",
        )
        raise httpx.HTTPStatusError("rate limited", request=request_info, response=response)

    result = await execute_integration_tool(
        repository=repository,
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_get_order",
        request=request,
        operation=operation,
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.kind == IntegrationErrorKind.RATE_LIMITED
    assert result.error.retry_after == "2"
    assert repository.events[0]["kind"] == "tool_call.failed"


@pytest.mark.asyncio
async def test_execute_integration_tool_blocks_missing_scope() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    calls = 0
    repository = InMemoryIntegrationRepository(
        credential=ProviderCredential(
            provider=IntegrationProvider.STRIPE,
            access_token="sk_test",
            refresh_token=None,
            expires_at=None,
            metadata={"granted_scopes": ["charges:read", "disputes:read"]},
        )
    )

    async def operation(_: ProviderCredential) -> JsonObject:
        nonlocal calls
        calls += 1
        return {"id": "re_123"}

    result = await execute_integration_tool(
        repository=repository,
        provider=IntegrationProvider.STRIPE,
        tool_name="stripe_create_refund",
        request=ToolRequest(
            merchant_id=merchant_id,
            case_id=case_id,
            idempotency_key="case-1:refund",
        ),
        operation=operation,
        write=True,
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.details["missing_scopes"] == ["refunds:write"]
    assert result.error.details["block_reasons"] == ["missing_scopes"]
    assert calls == 0


@pytest.mark.asyncio
async def test_shopify_write_scopes_satisfy_read_tool_requirements() -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    calls = 0
    repository = InMemoryIntegrationRepository(
        credential=ProviderCredential(
            provider=IntegrationProvider.SHOPIFY,
            access_token="shpat_test",
            refresh_token=None,
            expires_at=None,
            metadata={
                "shop_domain": "demo.myshopify.com",
                "granted_scopes": [
                    "write_fulfillments",
                    "write_merchant_managed_fulfillment_orders",
                    "write_orders",
                ],
            },
        )
    )

    async def operation(_: ProviderCredential) -> JsonObject:
        nonlocal calls
        calls += 1
        return {"ok": True}

    result = await execute_integration_tool(
        repository=repository,
        provider=IntegrationProvider.SHOPIFY,
        tool_name="shopify_hold_fulfillment_order",
        request=ToolRequest(
            merchant_id=merchant_id,
            case_id=case_id,
            idempotency_key="case-1:hold",
        ),
        operation=operation,
        write=True,
    )

    assert result.status == "succeeded"
    assert calls == 1
