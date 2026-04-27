from __future__ import annotations

from typing import Literal, TypedDict

ExceptionType = Literal[
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
]

RouteName = Literal[
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
]


class OrderExceptionState(TypedDict, total=False):
    merchant_id: str
    case_id: str
    exception_type: ExceptionType
    order: dict[str, object]
    context: dict[str, object]
    classification: dict[str, object]
    active_fops: list[dict[str, object]]
    fop_prompt_block: str
    hard_constraints: list[str]
    required_approvals: list[str]
    auto_actions: list[str]
    route: RouteName
    memory_context: dict[str, object]
    validation_errors: list[str]
    tool_calls_so_far: list[dict[str, object]]
    proposed_action: dict[str, object] | None
    human_decision: dict[str, object] | None
    resolution: dict[str, object] | None
    trace_notes: list[str]
