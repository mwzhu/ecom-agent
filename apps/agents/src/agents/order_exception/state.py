from __future__ import annotations

from typing import Literal, TypedDict

ExceptionType = Literal[
    "address_validation",
    "fraud_triage",
    "payment_failure",
    "high_value_review",
    "inventory_conflict",
]

RouteName = Literal[
    "address_validation",
    "fraud_triage",
    "payment_failure",
    "high_value_review",
    "inventory_conflict",
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
