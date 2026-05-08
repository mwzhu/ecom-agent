from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends
from langchain_core.tools import BaseTool
from langgraph_sdk import get_client
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import PRODUCTION_PROVIDER_WRITE_ACK, Settings, get_settings
from api.db.models import (
    ActorType,
    AgentRunExecution,
    AgentRunExecutionStatus,
    Case,
    CaseEvent,
    CaseStatus,
)
from api.db.session import get_sessionmaker
from api.integrations.base import (
    IntegrationErrorKind,
    JsonObject,
    ToolExecutionResult,
    ToolResultStatus,
)
from api.integrations.gorgias import gorgias_draft_reply, gorgias_search_customer
from api.integrations.shipbob import shipbob_get_order, shipbob_get_shipment, shipbob_hold_order
from api.integrations.shipstation import (
    shipstation_get_order,
    shipstation_get_shipment,
    shipstation_hold_order,
)
from api.integrations.shopify import (
    shopify_apply_order_edit,
    shopify_cancel_order,
    shopify_create_refund,
    shopify_hold_fulfillment_order,
    shopify_search_orders,
    shopify_update_order_note,
    shopify_update_shipping_address,
)
from api.integrations.stripe import (
    stripe_create_refund,
    stripe_get_charge,
    stripe_get_dispute,
    stripe_list_disputes,
)

ToolRunner = Callable[..., Awaitable[JsonObject]]
ToolRegistryEntry = BaseTool | ToolRunner
logger = logging.getLogger(__name__)

EXECUTION_TOOL_REGISTRY: dict[str, ToolRegistryEntry] = {
    "gorgias_draft_reply": gorgias_draft_reply,
    "gorgias_search_customer": gorgias_search_customer,
    "shipbob_get_order": shipbob_get_order,
    "shipbob_get_shipment": shipbob_get_shipment,
    "shipbob_hold_order": shipbob_hold_order,
    "shipstation_get_order": shipstation_get_order,
    "shipstation_get_shipment": shipstation_get_shipment,
    "shipstation_hold_order": shipstation_hold_order,
    "shopify_apply_order_edit": shopify_apply_order_edit,
    "shopify_cancel_order": shopify_cancel_order,
    "shopify_create_refund": shopify_create_refund,
    "shopify_hold_fulfillment_order": shopify_hold_fulfillment_order,
    "shopify_search_orders": shopify_search_orders,
    "shopify_update_order_note": shopify_update_order_note,
    "shopify_update_shipping_address": shopify_update_shipping_address,
    "stripe_create_refund": stripe_create_refund,
    "stripe_get_charge": stripe_get_charge,
    "stripe_get_dispute": stripe_get_dispute,
    "stripe_list_disputes": stripe_list_disputes,
}


@dataclass(frozen=True)
class ProcessRunResult:
    run_id: str
    thread_id: str
    status: str
    case_id: UUID
    case_status: str
    execution_status: str


@dataclass(frozen=True)
class CompletedLangGraphRun:
    run_id: str
    thread_id: str
    status: str
    metadata: JsonObject
    state: JsonObject


@dataclass(frozen=True)
class GraphSyncResult:
    case_status: str
    graph_status: str
    resolution_patch: JsonObject
    tool_calls_to_run: list[JsonObject]


@dataclass(frozen=True)
class ExecutionBatchResult:
    status: str
    case_status: str
    results: list[ToolExecutionResult]
    error: JsonObject | None = None


class LangGraphRunProcessor(Protocol):
    async def process_callback(self, *, thread_id: str, run_id: str) -> ProcessRunResult:
        ...


class SdkLangGraphRunProcessor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def process_callback(self, *, thread_id: str, run_id: str) -> ProcessRunResult:
        completed_run = await self._load_completed_run(thread_id=thread_id, run_id=run_id)
        merchant_id = _uuid_value(
            completed_run.metadata.get("merchant_id")
            or _value(_object_dict(completed_run.state.get("values")), "merchant_id")
        )
        case_id = _uuid_value(
            completed_run.metadata.get("case_id")
            or _value(_object_dict(completed_run.state.get("values")), "case_id")
        )
        sync_result = _derive_graph_sync_result(completed_run.state)

        sessionmaker = get_sessionmaker(self._settings)
        claimed = False
        async with sessionmaker() as session:
            async with session.begin():
                await _set_merchant_scope(session, merchant_id)
                claimed = await _claim_agent_run_execution(
                    session,
                    merchant_id=merchant_id,
                    case_id=case_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    graph_status=sync_result.graph_status,
                    graph_state=completed_run.state,
                )
                if not claimed:
                    existing_status = await _agent_run_status(session, run_id)
                    return ProcessRunResult(
                        run_id=run_id,
                        thread_id=thread_id,
                        status="duplicate",
                        case_id=case_id,
                        case_status=sync_result.case_status,
                        execution_status=existing_status or "duplicate",
                    )
                await _merge_case_state(
                    session,
                    merchant_id=merchant_id,
                    case_id=case_id,
                    status=sync_result.case_status,
                    resolution_patch=sync_result.resolution_patch,
                )
                await _record_case_event(
                    session,
                    merchant_id=merchant_id,
                    case_id=case_id,
                    kind="agent.run_state_synced",
                    payload={
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "run_status": completed_run.status,
                        "graph_status": sync_result.graph_status,
                        "case_status": sync_result.case_status,
                    },
                    langsmith_run_id=run_id,
                )
                if not sync_result.tool_calls_to_run:
                    await _update_agent_run_execution(
                        session,
                        run_id=run_id,
                        status=AgentRunExecutionStatus.SYNCED.value,
                        graph_status=sync_result.graph_status,
                        graph_state=completed_run.state,
                        execution_results=[],
                        error=None,
                    )

        if not claimed:
            raise RuntimeError("Agent run execution claim unexpectedly failed.")

        if not sync_result.tool_calls_to_run:
            return ProcessRunResult(
                run_id=run_id,
                thread_id=thread_id,
                status="processed",
                case_id=case_id,
                case_status=sync_result.case_status,
                execution_status=AgentRunExecutionStatus.SYNCED.value,
            )

        synthetic_run_tag = _synthetic_run_tag_from_state(completed_run.state)
        synthetic_real_provider_execution = _synthetic_real_provider_execution_allowed(
            self._settings,
            merchant_id,
        )
        if synthetic_run_tag and synthetic_real_provider_execution:
            logger.warning(
                "Synthetic run tag %s will execute real provider tools for merchant %s.",
                synthetic_run_tag,
                merchant_id,
            )
        elif synthetic_run_tag and self._settings.synthetic_execution_mode == "real_provider":
            logger.warning(
                "Synthetic real-provider execution was requested but not allowed for merchant %s.",
                merchant_id,
            )
        if synthetic_run_tag and not synthetic_real_provider_execution:
            result_payloads = _synthetic_tool_result_payloads(
                tool_calls=sync_result.tool_calls_to_run,
                synthetic_run_tag=synthetic_run_tag,
            )
            async with sessionmaker() as session:
                async with session.begin():
                    await _set_merchant_scope(session, merchant_id)
                    resolution_patch = {
                        "execution": {
                            "run_id": run_id,
                            "thread_id": thread_id,
                            "status": "synthetic_skipped",
                            "synthetic_run_tag": synthetic_run_tag,
                            "synthetic_execution_mode": self._settings.synthetic_execution_mode,
                            "tool_calls": sync_result.tool_calls_to_run,
                            "tool_results": result_payloads,
                        }
                    }
                    await _merge_case_state(
                        session,
                        merchant_id=merchant_id,
                        case_id=case_id,
                        status=CaseStatus.RESOLVED.value,
                        resolution_patch=resolution_patch,
                    )
                    await _record_case_event(
                        session,
                        merchant_id=merchant_id,
                        case_id=case_id,
                        kind="agent.execution_synthetic_skipped",
                        payload={
                            "run_id": run_id,
                            "thread_id": thread_id,
                            "synthetic_run_tag": synthetic_run_tag,
                            "synthetic_execution_mode": self._settings.synthetic_execution_mode,
                            "tool_calls": sync_result.tool_calls_to_run,
                            "tool_results": result_payloads,
                        },
                        langsmith_run_id=run_id,
                    )
                    await _update_agent_run_execution(
                        session,
                        run_id=run_id,
                        status=AgentRunExecutionStatus.SUCCEEDED.value,
                        graph_status=sync_result.graph_status,
                        graph_state=completed_run.state,
                        execution_results=result_payloads,
                        error=None,
                    )
            return ProcessRunResult(
                run_id=run_id,
                thread_id=thread_id,
                status="processed",
                case_id=case_id,
                case_status=CaseStatus.RESOLVED.value,
                execution_status="synthetic_skipped",
            )

        async with sessionmaker() as session:
            async with session.begin():
                await _set_merchant_scope(session, merchant_id)
                await _record_case_event(
                    session,
                    merchant_id=merchant_id,
                    case_id=case_id,
                    kind="agent.execution_started",
                    payload={
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "tool_count": len(sync_result.tool_calls_to_run),
                        "graph_status": sync_result.graph_status,
                    },
                    langsmith_run_id=run_id,
                )

        execution_result = await _execute_tool_plan(
            merchant_id=merchant_id,
            case_id=case_id,
            tool_calls=sync_result.tool_calls_to_run,
        )

        async with sessionmaker() as session:
            async with session.begin():
                await _set_merchant_scope(session, merchant_id)
                result_payloads = [
                    result.model_dump(mode="json") for result in execution_result.results
                ]
                execution_resolution_patch: JsonObject = {
                    "execution": {
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "status": execution_result.status,
                        "tool_results": result_payloads,
                        "error": execution_result.error,
                    }
                }
                await _merge_case_state(
                    session,
                    merchant_id=merchant_id,
                    case_id=case_id,
                    status=execution_result.case_status,
                    resolution_patch=execution_resolution_patch,
                )
                await _record_case_event(
                    session,
                    merchant_id=merchant_id,
                    case_id=case_id,
                    kind=(
                        "agent.execution_completed"
                        if execution_result.status == "succeeded"
                        else "agent.execution_failed"
                    ),
                    payload={
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "status": execution_result.status,
                        "tool_results": result_payloads,
                        "error": execution_result.error,
                    },
                    langsmith_run_id=run_id,
                )
                await _update_agent_run_execution(
                    session,
                    run_id=run_id,
                    status=(
                        AgentRunExecutionStatus.SUCCEEDED.value
                        if execution_result.status == "succeeded"
                        else AgentRunExecutionStatus.FAILED.value
                    ),
                    graph_status=sync_result.graph_status,
                    graph_state=completed_run.state,
                    execution_results=result_payloads,
                    error=execution_result.error,
                )

        return ProcessRunResult(
            run_id=run_id,
            thread_id=thread_id,
            status="processed",
            case_id=case_id,
            case_status=execution_result.case_status,
            execution_status=execution_result.status,
        )

    async def _load_completed_run(self, *, thread_id: str, run_id: str) -> CompletedLangGraphRun:
        client = get_client(url=self._settings.langgraph_studio_url)
        run = await client.runs.get(thread_id, run_id)
        await client.runs.join(thread_id, run_id)
        state = await client.threads.get_state(thread_id)
        return CompletedLangGraphRun(
            run_id=run_id,
            thread_id=thread_id,
            status=_value(run, "status") or "unknown",
            metadata=_object_dict(_object_dict(run).get("metadata")),
            state=_object_dict(state),
        )


def get_langgraph_run_processor(
    settings: Annotated[Settings, Depends(get_settings)],
) -> LangGraphRunProcessor:
    return SdkLangGraphRunProcessor(settings)


def _derive_graph_sync_result(state: JsonObject) -> GraphSyncResult:
    values = _object_dict(state.get("values"))
    if not values:
        values = _object_dict(state)
    raw_interrupts = state.get("interrupts")
    interrupt_count = len(raw_interrupts) if isinstance(raw_interrupts, list) else 0
    proposed_action = _object_dict(values.get("proposed_action"))
    resolution = _object_dict(values.get("resolution"))
    validation_errors = _string_list(
        values.get("validation_errors") or resolution.get("validation_errors")
    )
    tool_calls_so_far = _object_list(values.get("tool_calls_so_far"))
    executable_calls = [
        call
        for call in tool_calls_so_far
        if _value(call, "status") in {"approved", "auto_ready"}
    ]

    if interrupt_count > 0:
        graph_status = "awaiting_human"
        case_status = CaseStatus.PENDING_APPROVAL.value
    else:
        graph_status = _value(resolution, "status") or "open"
        if graph_status in {"awaiting_modification", "awaiting_human"}:
            case_status = CaseStatus.PENDING_APPROVAL.value
        elif graph_status == "rejected":
            case_status = CaseStatus.CANCELED.value
        elif executable_calls:
            case_status = CaseStatus.EXECUTING.value
        elif graph_status in {"approved", "auto_resolved"}:
            case_status = CaseStatus.RESOLVED.value
        else:
            case_status = CaseStatus.OPEN.value

    summary = _value(resolution, "summary") or _value(proposed_action, "summary")
    recommendation = _value(resolution, "recommendation") or _value(
        proposed_action, "recommendation"
    )
    tool_calls = tool_calls_so_far or _object_list(proposed_action.get("tool_calls"))
    resolution_patch: JsonObject = {
        "graph": {
            "status": graph_status,
            "summary": summary,
            "recommendation": recommendation,
            "matched_fop_ids": _string_list(
                resolution.get("matched_fop_ids") or proposed_action.get("matched_fop_ids")
            ),
            "validation_errors": validation_errors,
            "required_approvals": _string_list(proposed_action.get("required_approvals")),
            "hard_constraints": _string_list(proposed_action.get("hard_constraints")),
            "tool_calls": tool_calls,
            "human_decision": _object_dict(
                values.get("human_decision") or resolution.get("human_decision")
            )
            or None,
            "interrupt_count": interrupt_count,
        }
    }
    return GraphSyncResult(
        case_status=case_status,
        graph_status=graph_status,
        resolution_patch=resolution_patch,
        tool_calls_to_run=executable_calls,
    )


async def _execute_tool_plan(
    *,
    merchant_id: UUID,
    case_id: UUID,
    tool_calls: list[JsonObject],
) -> ExecutionBatchResult:
    validation_errors = _validate_tool_calls(tool_calls)
    if validation_errors:
        return ExecutionBatchResult(
            status="failed",
            case_status=CaseStatus.FAILED.value,
            results=[],
            error={
                "kind": IntegrationErrorKind.FATAL.value,
                "message": "Planned tool calls failed API-side validation.",
                "details": {"validation_errors": validation_errors},
            },
        )

    results: list[ToolExecutionResult] = []
    for call in tool_calls:
        try:
            result = await _invoke_tool_call(
                tool_call=call,
                merchant_id=merchant_id,
                case_id=case_id,
            )
        except Exception as exc:  # noqa: BLE001 - keep batch orchestration from leaving cases stuck.
            partial = len(results) > 0
            return ExecutionBatchResult(
                status="partially_executed" if partial else "failed",
                case_status=(
                    CaseStatus.NEEDS_HUMAN_RECOVERY.value if partial else CaseStatus.FAILED.value
                ),
                results=results,
                error={
                    "kind": IntegrationErrorKind.FATAL.value,
                    "message": str(exc) or "Tool invocation failed unexpectedly.",
                    "tool": _value(call, "tool"),
                    "recovery_required": partial,
                    "details": {},
                },
            )
        results.append(result)
        if result.status == ToolResultStatus.FAILED:
            partial = len(results) > 1
            return ExecutionBatchResult(
                status="partially_executed" if partial else "failed",
                case_status=(
                    CaseStatus.NEEDS_HUMAN_RECOVERY.value if partial else CaseStatus.FAILED.value
                ),
                results=results,
                error={
                    "kind": (
                        result.error.kind.value
                        if result.error
                        else IntegrationErrorKind.FATAL.value
                    ),
                    "message": result.error.message if result.error else "Tool execution failed.",
                    "tool": result.tool,
                    "provider": result.provider.value,
                    "recovery_required": partial,
                    "details": result.error.details if result.error else {},
                },
            )

    return ExecutionBatchResult(
        status="succeeded",
        case_status=CaseStatus.RESOLVED.value,
        results=results,
    )


async def _invoke_tool_call(
    *,
    tool_call: JsonObject,
    merchant_id: UUID,
    case_id: UUID,
) -> ToolExecutionResult:
    tool_name = _value(tool_call, "tool")
    if tool_name is None:
        raise ValueError("Executable tool call is missing a tool name.")
    handler = EXECUTION_TOOL_REGISTRY[tool_name]
    args: JsonObject = {
        **_object_dict(tool_call.get("input")),
        "merchant_id": merchant_id,
        "case_id": case_id,
        "idempotency_key": _value(tool_call, "idempotency_key"),
    }
    if isinstance(handler, BaseTool):
        payload = await handler.ainvoke(args)
    else:
        payload = await handler(**args)
    return ToolExecutionResult.model_validate(payload)


async def _set_merchant_scope(session: AsyncSession, merchant_id: UUID) -> None:
    await session.execute(
        text("select set_config('app.merchant_id', :merchant_id, true)"),
        {"merchant_id": str(merchant_id)},
    )


async def _claim_agent_run_execution(
    session: AsyncSession,
    *,
    merchant_id: UUID,
    case_id: UUID,
    thread_id: str,
    run_id: str,
    graph_status: str,
    graph_state: JsonObject,
) -> bool:
    statement = (
        postgres_insert(AgentRunExecution)
        .values(
            merchant_id=merchant_id,
            case_id=case_id,
            thread_id=thread_id,
            run_id=run_id,
            graph_status=graph_status,
            status=AgentRunExecutionStatus.PROCESSING.value,
            graph_state=graph_state,
            execution_results=[],
        )
        .on_conflict_do_nothing(constraint="uq_agent_run_executions_run")
        .returning(AgentRunExecution.id)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none() is not None


async def _agent_run_status(session: AsyncSession, run_id: str) -> str | None:
    result = await session.execute(
        select(AgentRunExecution.status).where(AgentRunExecution.run_id == run_id)
    )
    value = result.scalar_one_or_none()
    return value if isinstance(value, str) else None


async def _update_agent_run_execution(
    session: AsyncSession,
    *,
    run_id: str,
    status: str,
    graph_status: str,
    graph_state: JsonObject,
    execution_results: list[JsonObject],
    error: JsonObject | None,
) -> None:
    result = await session.execute(
        select(AgentRunExecution).where(AgentRunExecution.run_id == run_id)
    )
    execution = result.scalar_one()
    execution.status = status
    execution.graph_status = graph_status
    execution.graph_state = graph_state
    execution.execution_results = execution_results
    execution.error = error
    execution.updated_at = datetime.now(UTC)
    await session.flush()


async def _merge_case_state(
    session: AsyncSession,
    *,
    merchant_id: UUID,
    case_id: UUID,
    status: str,
    resolution_patch: JsonObject,
) -> None:
    case = await session.get(Case, case_id)
    if case is None or case.merchant_id != merchant_id:
        raise ValueError(f"Case {case_id} does not belong to merchant {merchant_id}.")
    existing = dict(case.resolution) if isinstance(case.resolution, dict) else {}
    case.status = status
    case.resolution = _deep_merge(existing, resolution_patch)
    if status in {
        CaseStatus.RESOLVED.value,
        CaseStatus.FAILED.value,
        CaseStatus.CANCELED.value,
    }:
        case.resolved_at = datetime.now(UTC)
    elif status in {
        CaseStatus.OPEN.value,
        CaseStatus.PENDING_APPROVAL.value,
        CaseStatus.EXECUTING.value,
    }:
        case.resolved_at = None
    await session.flush()


async def _record_case_event(
    session: AsyncSession,
    *,
    merchant_id: UUID,
    case_id: UUID,
    kind: str,
    payload: JsonObject,
    actor: ActorType = ActorType.SYSTEM,
    langsmith_run_id: str | None = None,
) -> None:
    session.add(
        CaseEvent(
            merchant_id=merchant_id,
            case_id=case_id,
            kind=kind,
            payload=payload,
            actor=actor.value,
            langsmith_run_id=langsmith_run_id,
        )
    )
    await session.flush()


def _validate_tool_calls(tool_calls: list[JsonObject]) -> list[str]:
    errors: list[str] = []
    for call in tool_calls:
        tool_name = _value(call, "tool")
        if tool_name not in EXECUTION_TOOL_REGISTRY:
            errors.append(f"Unsupported tool {tool_name!r}.")
        if not _value(call, "idempotency_key"):
            errors.append(f"{tool_name!r} is missing idempotency_key.")
        if not isinstance(call.get("input"), dict):
            errors.append(f"{tool_name!r} is missing an object input payload.")
    return errors


def _deep_merge(base: JsonObject, patch: JsonObject) -> JsonObject:
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(
                {str(inner_key): inner_value for inner_key, inner_value in current.items()},
                {str(inner_key): inner_value for inner_key, inner_value in value.items()},
            )
        else:
            merged[key] = value
    return merged


def _uuid_value(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise ValueError("Expected UUID-compatible value.")


def _value(payload: object, key: str) -> str | None:
    if isinstance(payload, dict):
        value = payload.get(key)
        return value if isinstance(value, str) else None
    value = getattr(payload, key, None)
    return value if isinstance(value, str) else None


def _object_dict(value: object) -> JsonObject:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {str(key): item for key, item in raw.items()}
    return {}


def _object_list(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    result: list[JsonObject] = []
    for item in value:
        if isinstance(item, dict):
            result.append({str(key): inner for key, inner in item.items()})
    return result


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _synthetic_run_tag_from_state(state: JsonObject) -> str | None:
    values = _object_dict(state.get("values")) or _object_dict(state)
    context = _object_dict(values.get("context"))
    webhook = _object_dict(context.get("webhook"))
    payload = _object_dict(webhook.get("payload"))
    synthetic = _object_dict(payload.get("synthetic"))
    tag = synthetic.get("run_tag")
    return tag if isinstance(tag, str) and tag else None


def _synthetic_real_provider_execution_allowed(settings: Settings, merchant_id: UUID) -> bool:
    if settings.synthetic_execution_mode != "real_provider":
        return False
    if not settings.enable_production_provider_writes:
        return False
    if settings.production_provider_write_ack != PRODUCTION_PROVIDER_WRITE_ACK:
        return False
    return _production_write_allowlist_contains(
        settings.production_provider_write_allowlist,
        merchant_id,
    )


def _production_write_allowlist_contains(allowlist: str, merchant_id: UUID) -> bool:
    tokens = {
        token.strip()
        for token in allowlist.replace("\n", ",").replace(" ", ",").split(",")
        if token.strip()
    }
    return "*" in tokens or str(merchant_id) in tokens


def _synthetic_tool_result_payloads(
    *,
    tool_calls: list[JsonObject],
    synthetic_run_tag: str,
) -> list[JsonObject]:
    return [
        {
            "provider": _provider_for_tool_name(_value(call, "tool")),
            "tool": _value(call, "tool") or "unknown",
            "idempotency_key": _value(call, "idempotency_key") or "",
            "status": ToolResultStatus.SKIPPED.value,
            "data": {
                "reason": "synthetic_run",
                "synthetic_run_tag": synthetic_run_tag,
                "input": _object_dict(call.get("input")),
            },
            "error": None,
        }
        for call in tool_calls
    ]


def _provider_for_tool_name(tool_name: str | None) -> str:
    if isinstance(tool_name, str):
        prefix = tool_name.split("_", 1)[0]
        if prefix in {"shopify", "gorgias", "shipbob", "shipstation", "stripe"}:
            return prefix
    return "shopify"
