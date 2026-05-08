from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import tool

from api.agents import ProcessRunResult, get_langgraph_run_processor
from api.agents.processor import (
    EXECUTION_TOOL_REGISTRY,
    CompletedLangGraphRun,
    SdkLangGraphRunProcessor,
    _derive_graph_sync_result,
    _execute_tool_plan,
    _synthetic_real_provider_execution_allowed,
    _synthetic_run_tag_from_state,
)
from api.config import PRODUCTION_PROVIDER_WRITE_ACK, Settings, get_settings
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


def test_synthetic_run_tag_is_extracted_from_langgraph_state() -> None:
    assert (
        _synthetic_run_tag_from_state(
            {
                "values": {
                    "context": {
                        "webhook": {
                            "payload": {
                                "synthetic": {
                                    "run_tag": "sim-test",
                                    "scenario_id": "fraud_high_score",
                                }
                            }
                        }
                    }
                }
            }
        )
        == "sim-test"
    )


def test_synthetic_real_provider_mode_requires_all_write_gates() -> None:
    merchant_id = uuid4()

    assert not _synthetic_real_provider_execution_allowed(
        Settings(
            synthetic_execution_mode="real_provider",
            enable_production_provider_writes=False,
            production_provider_write_allowlist=str(merchant_id),
            production_provider_write_ack=PRODUCTION_PROVIDER_WRITE_ACK,
        ),
        merchant_id,
    )
    assert not _synthetic_real_provider_execution_allowed(
        Settings(
            synthetic_execution_mode="skip",
            enable_production_provider_writes=True,
            production_provider_write_allowlist=str(merchant_id),
            production_provider_write_ack=PRODUCTION_PROVIDER_WRITE_ACK,
        ),
        merchant_id,
    )
    assert not _synthetic_real_provider_execution_allowed(
        Settings(
            synthetic_execution_mode="real_provider",
            enable_production_provider_writes=True,
            production_provider_write_allowlist=str(uuid4()),
            production_provider_write_ack=PRODUCTION_PROVIDER_WRITE_ACK,
        ),
        merchant_id,
    )
    assert _synthetic_real_provider_execution_allowed(
        Settings(
            synthetic_execution_mode="real_provider",
            enable_production_provider_writes=True,
            production_provider_write_allowlist=str(merchant_id),
            production_provider_write_ack=PRODUCTION_PROVIDER_WRITE_ACK,
        ),
        merchant_id,
    )
    assert _synthetic_real_provider_execution_allowed(
        Settings(
            synthetic_execution_mode="real_provider",
            enable_production_provider_writes=True,
            production_provider_write_allowlist="*",
            production_provider_write_ack=PRODUCTION_PROVIDER_WRITE_ACK,
        ),
        merchant_id,
    )


def test_production_write_ack_is_required_when_write_gate_is_enabled() -> None:
    with pytest.raises(ValueError, match="PRODUCTION_PROVIDER_WRITE_ACK"):
        Settings(
            synthetic_execution_mode="real_provider",
            enable_production_provider_writes=True,
            production_provider_write_allowlist="*",
            production_provider_write_ack="yes-really",
        )


@pytest.mark.asyncio
async def test_processor_skips_provider_execution_for_approved_synthetic_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merchant_id = uuid4()
    case_id = uuid4()
    recorded_events: list[dict[str, object]] = []
    merged_statuses: list[str] = []
    updated_executions: list[dict[str, object]] = []

    class FakeTransaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def begin(self) -> FakeTransaction:
            return FakeTransaction()

    class FakeSessionmaker:
        def __call__(self) -> FakeSession:
            return FakeSession()

    class FakeProcessor(SdkLangGraphRunProcessor):
        async def _load_completed_run(
            self,
            *,
            thread_id: str,
            run_id: str,
        ) -> CompletedLangGraphRun:
            return CompletedLangGraphRun(
                run_id=run_id,
                thread_id=thread_id,
                status="success",
                metadata={"merchant_id": str(merchant_id), "case_id": str(case_id)},
                state={
                    "values": {
                        "context": {
                            "webhook": {
                                "payload": {
                                    "synthetic": {
                                        "run_tag": "sim-test",
                                        "scenario_id": "fraud_high_score",
                                        "expected_exception_types": ["fraud_triage"],
                                        "shop_index": 1,
                                    }
                                }
                            }
                        },
                        "resolution": {
                            "status": "approved",
                            "summary": "Approved synthetic cancellation.",
                        },
                        "tool_calls_so_far": [
                            {
                                "tool": "shopify_cancel_order",
                                "idempotency_key": "synthetic-key",
                                "input": {"order_id": "#SIM-1234-0001"},
                                "status": "approved",
                            }
                        ],
                    },
                    "interrupts": [],
                },
            )

    async def fake_set_scope(*_: object, **__: object) -> None:
        return None

    async def fake_claim(*_: object, **__: object) -> bool:
        return True

    async def fake_merge(*_: object, status: str, **__: object) -> None:
        merged_statuses.append(status)

    async def fake_record(*_: object, kind: str, payload: dict[str, object], **__: object) -> None:
        recorded_events.append({"kind": kind, "payload": payload})

    async def fake_update(*_: object, status: str, execution_results: list[dict[str, object]], **__: object) -> None:
        updated_executions.append({"status": status, "execution_results": execution_results})

    async def fail_execute(*_: object, **__: object) -> object:
        raise AssertionError("provider registry should not be called for synthetic cases")

    monkeypatch.setattr("api.agents.processor.get_sessionmaker", lambda _: FakeSessionmaker())
    monkeypatch.setattr("api.agents.processor._set_merchant_scope", fake_set_scope)
    monkeypatch.setattr("api.agents.processor._claim_agent_run_execution", fake_claim)
    monkeypatch.setattr("api.agents.processor._merge_case_state", fake_merge)
    monkeypatch.setattr("api.agents.processor._record_case_event", fake_record)
    monkeypatch.setattr("api.agents.processor._update_agent_run_execution", fake_update)
    monkeypatch.setattr("api.agents.processor._execute_tool_plan", fail_execute)

    result = await FakeProcessor(Settings(environment="local")).process_callback(
        thread_id="thread_123",
        run_id="run_123",
    )

    assert result.case_status == "resolved"
    assert result.execution_status == "synthetic_skipped"
    assert merged_statuses == ["executing", "resolved"]
    assert recorded_events[-1]["kind"] == "agent.execution_synthetic_skipped"
    assert updated_executions[-1]["status"] == "succeeded"
    assert updated_executions[-1]["execution_results"][0]["status"] == "skipped"


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

    assert result.status == "partially_executed"
    assert result.case_status == "needs_human_recovery"
    assert [item.tool for item in result.results] == ["tool_a", "tool_b"]
    assert result.error is not None
    assert result.error["recovery_required"] is True
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
