from __future__ import annotations

import hashlib
import json
from typing import Any

JsonObject = dict[str, Any]

WRITE_TOOLS = {
    "shopify_update_order_note",
    "shopify_create_refund",
    "shopify_hold_fulfillment_order",
    "shopify_cancel_order",
    "stripe_create_refund",
    "gorgias_draft_reply",
    "shipbob_hold_order",
    "shipstation_hold_order",
}

MONEY_MOVEMENT_TOOLS = {
    "shopify_create_refund",
    "stripe_create_refund",
}


def planned_tool_call(
    *,
    case_id: str,
    tool: str,
    intent: str,
    payload: JsonObject,
    write: bool | None = None,
) -> JsonObject:
    is_write = tool in WRITE_TOOLS if write is None else write
    call: JsonObject = {
        "tool": tool,
        "intent": intent,
        "input": payload,
        "write": is_write,
        "status": "planned",
    }
    if is_write:
        call["idempotency_key"] = build_idempotency_key(case_id, intent, payload)
    return call


def build_idempotency_key(case_id: str, intent: str, payload: JsonObject) -> str:
    stable = json.dumps(
        {"case_id": case_id, "intent": intent, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def tool_plan_requires_human(tool_calls: list[JsonObject]) -> bool:
    return any(call.get("write") is True for call in tool_calls)


def validate_tool_plan(tool_calls: list[JsonObject]) -> list[str]:
    errors: list[str] = []
    for call in tool_calls:
        tool = call.get("tool")
        if call.get("write") is True and not call.get("idempotency_key"):
            errors.append(f"{tool} is a write tool but has no idempotency_key.")
        if tool in MONEY_MOVEMENT_TOOLS and not call.get("idempotency_key"):
            errors.append(f"{tool} moves money and must be idempotent.")
    return errors


def mark_tool_calls(tool_calls: list[JsonObject], status: str) -> list[JsonObject]:
    return [{**call, "status": status} for call in tool_calls]
