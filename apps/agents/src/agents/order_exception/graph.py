from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from langsmith import traceable

from agents.order_exception.llm_layer import refine_supervisor_route
from agents.order_exception.state import ExceptionType, OrderExceptionState, RouteName
from agents.order_exception.subagents import (
    address_change_request_subagent,
    damaged_in_transit_subagent,
    delivered_not_received_subagent,
    fraud_triage_subagent,
    inventory_conflict_subagent,
    item_change_request_subagent,
    order_cancellation_request_subagent,
    order_not_picked_subagent,
    stuck_in_transit_subagent,
    wismo_subagent,
)
from agents.order_exception.tooling import mark_tool_calls, validate_tool_plan
from agents.shared import evaluate_fops, load_fops_for_merchant
from ecom_shared import classify_order_exception


@traceable(name="order_exception_classify")
def classify(state: OrderExceptionState) -> OrderExceptionState:
    order = _object_dict(state.get("order"))
    context = _object_dict(state.get("context"))
    supplied_exception_type = state.get("exception_type")
    if supplied_exception_type in {
        "address_change_request",
        "damaged_in_transit",
        "delivered_not_received",
        "fraud_triage",
        "inventory_conflict",
        "item_change_request",
        "order_cancellation_request",
        "order_not_picked",
        "stuck_in_transit",
        "wismo",
    }:
        exception_type = _exception_type(supplied_exception_type)
        classification: dict[str, object] = {
            "exception_type": exception_type,
            "confidence": 0.99,
            "signals": ["Caller supplied a supported exception_type."],
            "classifier": "caller",
        }
    else:
        result = classify_order_exception(order, context)
        refinement = refine_supervisor_route(
            order=order,
            context=context,
            deterministic=result,
        )
        exception_type = refinement.exception_type
        classification = {
            "exception_type": exception_type,
            "confidence": refinement.confidence,
            "signals": refinement.signals,
            "classifier": refinement.source,
        }
        if refinement.model is not None:
            classification["model"] = refinement.model
    return {
        **state,
        "order": order,
        "context": {**context, "exception_type": exception_type},
        "exception_type": exception_type,
        "classification": classification,
        "trace_notes": [
            *_string_list(state.get("trace_notes")),
            f"Classifier selected {exception_type}.",
        ],
    }


@traceable(name="order_exception_initialize")
def initialize(state: OrderExceptionState) -> OrderExceptionState:
    merchant_id = str(state.get("merchant_id") or "demo-merchant")
    exception_type = _exception_type(state.get("exception_type"))
    order = _object_dict(state.get("order"))
    context = _object_dict(state.get("context"))
    scope = f"order_exception.{exception_type}"
    all_fops = load_fops_for_merchant(merchant_id)
    fop_evaluation = evaluate_fops(
        fops=all_fops,
        scope=scope,
        order=order,
        context=context,
    )

    return {
        **state,
        "merchant_id": merchant_id,
        "exception_type": exception_type,
        "order": order,
        "context": context,
        "active_fops": fop_evaluation.matched_fops,
        "fop_prompt_block": fop_evaluation.system_prompt_block,
        "hard_constraints": fop_evaluation.hard_constraints,
        "required_approvals": fop_evaluation.required_approvals,
        "auto_actions": fop_evaluation.auto_actions,
        "memory_context": _object_dict(context.get("memory")),
        "tool_calls_so_far": _object_list(state.get("tool_calls_so_far")),
        "trace_notes": [
            *_string_list(state.get("trace_notes")),
            f"Loaded {len(all_fops)} FOP(s); {len(fop_evaluation.matched_fops)} matched {scope}.",
        ],
    }


@traceable(name="order_exception_supervisor")
def supervisor(state: OrderExceptionState) -> OrderExceptionState:
    route = _route_name(state.get("exception_type"))
    model_profile = (
        "opus-4.7"
        if route in {"delivered_not_received", "fraud_triage", "item_change_request"}
        else "sonnet-4.6"
    )
    return {
        **state,
        "route": route,
        "trace_notes": [
            *_string_list(state.get("trace_notes")),
            f"Supervisor routed to {route} using {model_profile} profile.",
        ],
    }


@traceable(name="order_exception_approval_gate")
def approval_gate(state: OrderExceptionState) -> OrderExceptionState:
    proposed_action = _object_dict(state.get("proposed_action"))
    if not proposed_action.get("requires_human"):
        return state

    existing_decision = state.get("human_decision")
    if isinstance(existing_decision, dict):
        return state

    decision = interrupt(
        {
            "case_id": state.get("case_id"),
            "merchant_id": state.get("merchant_id"),
            "summary": proposed_action.get("summary"),
            "recommendation": proposed_action.get("recommendation"),
            "required_approvals": proposed_action.get("required_approvals", []),
            "tool_calls": proposed_action.get("tool_calls", []),
            "matched_fop_ids": proposed_action.get("matched_fop_ids", []),
            "hard_constraints": proposed_action.get("hard_constraints", []),
        }
    )
    return {
        **state,
        "human_decision": _object_dict(decision),
        "trace_notes": [
            *_string_list(state.get("trace_notes")),
            "Human approval received through LangGraph interrupt resume.",
        ],
    }


@traceable(name="order_exception_finalize")
def finalize(state: OrderExceptionState) -> OrderExceptionState:
    proposed_action = _object_dict(state.get("proposed_action"))
    tool_calls = _object_list(proposed_action.get("tool_calls"))
    validation_errors = validate_tool_plan(tool_calls)
    decision = _object_dict(state.get("human_decision"))

    if proposed_action.get("requires_human") and not decision:
        return {
            **state,
            "validation_errors": validation_errors,
            "resolution": {
                "status": "awaiting_human",
                "summary": proposed_action.get("summary"),
            },
        }

    if decision and decision.get("decision") == "reject":
        return {
            **state,
            "validation_errors": validation_errors,
            "tool_calls_so_far": [
                *_object_list(state.get("tool_calls_so_far")),
                *mark_tool_calls(tool_calls, "rejected"),
            ],
            "resolution": {
                "status": "rejected",
                "summary": proposed_action.get("summary"),
                "human_decision": decision,
            },
        }

    if decision and decision.get("decision") == "modify":
        return {
            **state,
            "validation_errors": validation_errors,
            "resolution": {
                "status": "awaiting_modification",
                "summary": proposed_action.get("summary"),
                "recommendation": proposed_action.get("recommendation"),
                "matched_fop_ids": proposed_action.get("matched_fop_ids", []),
                "validation_errors": validation_errors,
                "human_decision": decision,
            },
        }

    status = "approved" if proposed_action.get("requires_human") else "auto_resolved"
    call_status = "approved" if proposed_action.get("requires_human") else "auto_ready"
    return {
        **state,
        "validation_errors": validation_errors,
        "tool_calls_so_far": [
            *_object_list(state.get("tool_calls_so_far")),
            *mark_tool_calls(tool_calls, call_status),
        ],
        "resolution": {
            "status": status,
            "summary": proposed_action.get("summary"),
            "recommendation": proposed_action.get("recommendation"),
            "matched_fop_ids": proposed_action.get("matched_fop_ids", []),
            "tool_call_count": len(tool_calls),
            "validation_errors": validation_errors,
            "human_decision": decision or None,
        },
    }


def route_from_state(state: OrderExceptionState) -> RouteName:
    return _route_name(state.get("route") or state.get("exception_type"))


def next_after_proposal(state: OrderExceptionState) -> str:
    proposed_action = _object_dict(state.get("proposed_action"))
    return "approval_gate" if proposed_action.get("requires_human") else "finalize"


def build_graph(checkpointer: Any | None = None) -> Any:
    builder = StateGraph(OrderExceptionState)
    builder.add_node("classify", classify)
    builder.add_node("initialize", initialize)
    builder.add_node("supervisor", supervisor)
    builder.add_node("address_change_request", address_change_request_subagent)
    builder.add_node("damaged_in_transit", damaged_in_transit_subagent)
    builder.add_node("delivered_not_received", delivered_not_received_subagent)
    builder.add_node("fraud_triage", fraud_triage_subagent)
    builder.add_node("inventory_conflict", inventory_conflict_subagent)
    builder.add_node("item_change_request", item_change_request_subagent)
    builder.add_node("order_cancellation_request", order_cancellation_request_subagent)
    builder.add_node("order_not_picked", order_not_picked_subagent)
    builder.add_node("stuck_in_transit", stuck_in_transit_subagent)
    builder.add_node("wismo", wismo_subagent)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "initialize")
    builder.add_edge("initialize", "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        route_from_state,
        {
            "address_change_request": "address_change_request",
            "damaged_in_transit": "damaged_in_transit",
            "delivered_not_received": "delivered_not_received",
            "fraud_triage": "fraud_triage",
            "inventory_conflict": "inventory_conflict",
            "item_change_request": "item_change_request",
            "order_cancellation_request": "order_cancellation_request",
            "order_not_picked": "order_not_picked",
            "stuck_in_transit": "stuck_in_transit",
            "wismo": "wismo",
        },
    )
    for node in (
        "address_change_request",
        "damaged_in_transit",
        "delivered_not_received",
        "fraud_triage",
        "inventory_conflict",
        "item_change_request",
        "order_cancellation_request",
        "order_not_picked",
        "stuck_in_transit",
        "wismo",
    ):
        builder.add_conditional_edges(
            node,
            next_after_proposal,
            {"approval_gate": "approval_gate", "finalize": "finalize"},
        )
    builder.add_edge("approval_gate", "finalize")
    builder.add_edge("finalize", END)
    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


def build_graph_for_local() -> Any:
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(checkpointer=InMemorySaver())


def _exception_type(value: object) -> ExceptionType:
    route = _route_name(value)
    return route


def _route_name(value: object) -> RouteName:
    if value in {
        "address_change_request",
        "damaged_in_transit",
        "delivered_not_received",
        "fraud_triage",
        "inventory_conflict",
        "item_change_request",
        "order_cancellation_request",
        "order_not_picked",
        "stuck_in_transit",
        "wismo",
    }:
        return value  # type: ignore[return-value]
    return "fraud_triage"


def _object_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


graph = build_graph()
