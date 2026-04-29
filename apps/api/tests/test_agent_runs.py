from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import tool

from api.agents import ProcessRunResult, get_langgraph_run_processor
from api.agents.processor import (
    EXECUTION_TOOL_REGISTRY,
    _derive_graph_sync_result,
    _execute_tool_plan,
)
from api.config import Settings, get_settings
from api.main import app


@dataclass
class InMemoryRunProcessor:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def process_callback(self, *, thread_id: str, run_id: str) -> ProcessRunResult:
        self.calls.append((thread_id, run_id))
        return ProcessRunResult(
            run_id=run_id,
            thread_id=thread_id,
            status="processed",
            case_id=uuid4(),
            case_status="executing",
            execution_status="processing",
        )


def test_langgraph_callback_requires_secret_when_configured() -> None:
    processor = InMemoryRunProcessor()
    client = _client(processor, Settings(langgraph_run_webhook_secret="secret-token"))

    try:
        response = client.post(
            "/v1/agent-runs/langgraph-complete",
            json={"run_id": "run_123", "thread_id": "thread_123"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert processor.calls == []


def test_langgraph_callback_invokes_processor() -> None:
    processor = InMemoryRunProcessor()
    client = _client(processor, Settings(langgraph_run_webhook_secret="secret-token"))

    try:
        response = client.post(
            "/v1/agent-runs/langgraph-complete?token=secret-token",
            json={"run_id": "run_123", "thread_id": "thread_123"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert processor.calls == [("thread_123", "run_123")]


def test_derive_graph_sync_result_marks_interrupts_pending() -> None:
    result = _derive_graph_sync_result(
        {
            "values": {
                "proposed_action": {
                    "summary": "Cancel and refund.",
                    "recommendation": "Needs human approval.",
                    "tool_calls": [{"tool": "shopify_cancel_order"}],
                }
            },
            "interrupts": [{"value": {"summary": "Cancel and refund."}}],
        }
    )

    assert result.case_status == "pending_approval"
    assert result.graph_status == "awaiting_human"
    assert result.tool_calls_to_run == []
    assert result.resolution_patch["graph"]["interrupt_count"] == 1


def test_derive_graph_sync_result_supports_raw_join_values() -> None:
    result = _derive_graph_sync_result(
        {
            "merchant_id": str(uuid4()),
            "case_id": str(uuid4()),
            "resolution": {
                "status": "auto_resolved",
                "summary": "Customer asked for a routine shipping-status update.",
                "recommendation": "Send the customer a concise tracking update.",
                "matched_fop_ids": [],
                "validation_errors": [],
                "human_decision": None,
            },
            "proposed_action": {
                "summary": "Customer asked for a routine shipping-status update.",
                "recommendation": "Send the customer a concise tracking update.",
                "tool_calls": [],
                "matched_fop_ids": [],
                "required_approvals": [],
                "hard_constraints": [],
            },
            "tool_calls_so_far": [],
            "interrupts": [],
        }
    )

    assert result.case_status == "resolved"
    assert result.graph_status == "auto_resolved"
    assert result.tool_calls_to_run == []
    assert result.resolution_patch["graph"]["summary"] == (
        "Customer asked for a routine shipping-status update."
    )


@pytest.mark.asyncio
async def test_execute_tool_plan_stops_on_first_failed_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def tool_a(**_: object) -> dict[str, object]:
        calls.append("tool_a")
        return {
            "provider": "shopify",
            "tool": "tool_a",
            "idempotency_key": "key-a",
            "status": "succeeded",
            "data": {"ok": True},
            "error": None,
        }

    async def tool_b(**_: object) -> dict[str, object]:
        calls.append("tool_b")
        return {
            "provider": "shopify",
            "tool": "tool_b",
            "idempotency_key": "key-b",
            "status": "failed",
            "data": None,
            "error": {
                "kind": "FATAL",
                "provider": "shopify",
                "message": "boom",
                "status_code": None,
                "retry_after": None,
                "details": {},
            },
        }

    async def tool_c(**_: object) -> dict[str, object]:
        calls.append("tool_c")
        return {
            "provider": "shopify",
            "tool": "tool_c",
            "idempotency_key": "key-c",
            "status": "succeeded",
            "data": {"ok": True},
            "error": None,
        }

    monkeypatch.setitem(EXECUTION_TOOL_REGISTRY, "tool_a", tool_a)
    monkeypatch.setitem(EXECUTION_TOOL_REGISTRY, "tool_b", tool_b)
    monkeypatch.setitem(EXECUTION_TOOL_REGISTRY, "tool_c", tool_c)

    result = await _execute_tool_plan(
        merchant_id=uuid4(),
        case_id=uuid4(),
        tool_calls=[
            {"tool": "tool_a", "idempotency_key": "key-a", "input": {}, "status": "approved"},
            {"tool": "tool_b", "idempotency_key": "key-b", "input": {}, "status": "approved"},
            {"tool": "tool_c", "idempotency_key": "key-c", "input": {}, "status": "approved"},
        ],
    )

    assert result.status == "failed"
    assert result.case_status == "failed"
    assert [item.tool for item in result.results] == ["tool_a", "tool_b"]
    assert calls == ["tool_a", "tool_b"]


@pytest.mark.asyncio
async def test_execute_tool_plan_invokes_structured_tool_registry_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_args: list[dict[str, object]] = []
    merchant_id = uuid4()
    case_id = uuid4()

    @tool("structured_test_tool")
    async def structured_test_tool(
        merchant_id: object,
        case_id: object,
        idempotency_key: object,
        query: object,
        limit: object,
    ) -> dict[str, object]:
        """Record structured tool execution arguments for executor tests."""
        args = {
            "merchant_id": merchant_id,
            "case_id": case_id,
            "idempotency_key": idempotency_key,
            "query": query,
            "limit": limit,
        }
        recorded_args.append(args)
        return {
            "provider": "shopify",
            "tool": "structured_test_tool",
            "idempotency_key": str(idempotency_key),
            "status": "succeeded",
            "data": {"orders": []},
            "error": None,
        }

    monkeypatch.setitem(EXECUTION_TOOL_REGISTRY, "structured_test_tool", structured_test_tool)

    result = await _execute_tool_plan(
        merchant_id=merchant_id,
        case_id=case_id,
        tool_calls=[
            {
                "tool": "structured_test_tool",
                "idempotency_key": "key-read",
                "input": {"query": "email:buyer@example.com", "limit": 5},
                "status": "approved",
            }
        ],
    )

    assert result.status == "succeeded"
    assert result.case_status == "resolved"
    assert [item.tool for item in result.results] == ["structured_test_tool"]
    assert recorded_args == [
        {
            "merchant_id": merchant_id,
            "case_id": case_id,
            "idempotency_key": "key-read",
            "query": "email:buyer@example.com",
            "limit": 5,
        }
    ]


def _client(processor: InMemoryRunProcessor, settings: Settings) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_langgraph_run_processor] = lambda: processor
    return TestClient(app)
