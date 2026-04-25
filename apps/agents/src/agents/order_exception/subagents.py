from __future__ import annotations

from typing import Any

from agents.order_exception.llm_layer import refine_subagent_proposal
from agents.order_exception.state import ExceptionType, OrderExceptionState
from agents.order_exception.tooling import planned_tool_call, tool_plan_requires_human

JsonObject = dict[str, Any]


def address_validation_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    address = _address_context(order, context)
    is_valid = address.get("is_valid")
    status = str(address.get("status", "unknown"))
    issues = _string_list(address.get("issues"))
    customer_email = _customer_email(order, context)

    if is_valid is True or status == "valid":
        return _with_proposal(
            state,
            exception_type="address_validation",
            summary="Shipping address passed validation; no fulfillment hold is needed.",
            recommendation="Proceed with normal fulfillment.",
            tool_calls=[],
            confidence=0.92,
            rationale=["Address validation context marked the address as valid."],
        )

    suggested_address = address.get("suggested_address")
    message = _address_message(order, issues, suggested_address)
    tool_calls = [
        planned_tool_call(
            case_id=case_id,
            tool="shopify_hold_fulfillment_order",
            intent="hold_invalid_address_order",
            payload={
                "fulfillment_order_id": _fulfillment_order_id(order, context),
                "reason": "OTHER",
                "reason_notes": "Address needs customer confirmation before fulfillment.",
            },
        ),
        planned_tool_call(
            case_id=case_id,
            tool="gorgias_draft_reply",
            intent="draft_address_confirmation_message",
            payload={
                "ticket_id": _ticket_id(context),
                "customer_email": customer_email,
                "body_html": message,
            },
        ),
        planned_tool_call(
            case_id=case_id,
            tool="shopify_update_order_note",
            intent="annotate_invalid_address",
            payload={
                "order_id": _order_id(order),
                "note": "Held by Order Exception Agent: address requires confirmation.",
            },
        ),
    ]
    return _with_proposal(
        state,
        exception_type="address_validation",
        summary="Address validation found an ambiguous or invalid shipping address.",
        recommendation="Hold fulfillment and send the customer a confirmation draft.",
        tool_calls=tool_calls,
        confidence=0.86,
        rationale=[
            "The address validation context did not mark the address as valid.",
            "Customer confirmation is safer than editing the address without approval.",
        ],
    )


def fraud_triage_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    score = _number(_nested(context, ["risk", "score"], _nested(order, ["risk", "score"], 0))) or 0
    auto_actions = _string_list(state.get("auto_actions"))

    if score > 80 and {"cancel_order", "refund_payment"}.issubset(set(auto_actions)):
        refund_payload = {
            "order_id": _order_id(order),
            "note": "Refund per fraud FOP: fraud score above 80.",
            "notify_customer": True,
            "refund_line_items": context.get("refund_line_items", []),
            "transactions": context.get("refund_transactions", []),
        }
        tool_calls = [
            planned_tool_call(
                case_id=case_id,
                tool="shopify_cancel_order",
                intent="cancel_high_fraud_order",
                payload={
                    "order_id": _order_id(order),
                    "reason": "FRAUD",
                    "staff_note": "Canceled per merchant FOP: fraud score above 80.",
                },
            ),
            planned_tool_call(
                case_id=case_id,
                tool="shopify_create_refund",
                intent="refund_high_fraud_order",
                payload=refund_payload,
            ),
        ]
        return _with_proposal(
            state,
            exception_type="fraud_triage",
            summary=f"Fraud score {score:.0f} is above the merchant's cancel threshold.",
            recommendation="Cancel the order and issue the refund after approval.",
            tool_calls=tool_calls,
            confidence=0.9,
            rationale=[
                "A matched FOP says fraud scores above 80 should be canceled and refunded.",
                "Money movement still requires a human approval gate in Phase 0.",
            ],
        )

    if score >= 50:
        tool_calls = [
            planned_tool_call(
                case_id=case_id,
                tool="shopify_update_order_note",
                intent="annotate_medium_fraud_review",
                payload={
                    "order_id": _order_id(order),
                    "note": f"Held by Order Exception Agent: fraud score {score:.0f}.",
                },
            )
        ]
        return _with_proposal(
            state,
            exception_type="fraud_triage",
            summary=f"Fraud score {score:.0f} needs manual review.",
            recommendation="Hold for manual fraud review before fulfillment.",
            tool_calls=tool_calls,
            confidence=0.78,
            rationale=["Medium fraud scores need operator judgment before release."],
        )

    return _with_proposal(
        state,
        exception_type="fraud_triage",
        summary=f"Fraud score {score:.0f} is below the review threshold.",
        recommendation="Proceed with normal fulfillment.",
        tool_calls=[],
        confidence=0.84,
        rationale=["No matched FOP requires a hold, cancellation, or refund."],
    )


def payment_failure_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    payment = context.get("payment", {})
    payment_status = _string(_nested(payment, ["status"], order.get("financial_status")))

    if payment_status not in {"failed", "declined", "requires_payment_method", "requires_action"}:
        return _with_proposal(
            state,
            exception_type="payment_failure",
            summary="Payment status does not indicate an active failure.",
            recommendation="Monitor the order without contacting the customer.",
            tool_calls=[],
            confidence=0.8,
            rationale=[f"Payment status is {payment_status or 'unknown'}."],
        )

    customer_email = _customer_email(order, context)
    message = (
        "Hi, we were not able to complete payment for your order. "
        "Please update your payment method so we can continue processing it."
    )
    tool_calls = [
        planned_tool_call(
            case_id=case_id,
            tool="stripe_get_charge",
            intent="inspect_failed_charge",
            payload={"charge_id": _string(_nested(payment, ["charge_id"], ""))},
            write=False,
        ),
        planned_tool_call(
            case_id=case_id,
            tool="gorgias_draft_reply",
            intent="draft_payment_reauth_message",
            payload={
                "ticket_id": _ticket_id(context),
                "customer_email": customer_email,
                "body_html": message,
            },
        ),
        planned_tool_call(
            case_id=case_id,
            tool="shopify_update_order_note",
            intent="annotate_payment_failure",
            payload={
                "order_id": _order_id(order),
                "note": "Payment failed; customer reauthorization draft prepared.",
            },
        ),
    ]
    return _with_proposal(
        state,
        exception_type="payment_failure",
        summary="Payment failed or requires customer reauthorization.",
        recommendation=(
            "Inspect the charge, draft a payment update message, and annotate the order."
        ),
        tool_calls=tool_calls,
        confidence=0.83,
        rationale=["The payment status indicates the merchant cannot fulfill safely yet."],
    )


def high_value_review_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    total = _number(order.get("total_price") or order.get("totalPrice")) or 0
    matched_fops = _matched_fops(state)

    if not matched_fops:
        return _with_proposal(
            state,
            exception_type="high_value_review",
            summary=f"Order value ${total:.2f} does not match an active high-value FOP.",
            recommendation="Proceed with normal fulfillment.",
            tool_calls=[],
            confidence=0.81,
            rationale=["No active high-value review FOP matched this order context."],
        )

    tool_calls = [
        planned_tool_call(
            case_id=case_id,
            tool="shopify_hold_fulfillment_order",
            intent="hold_high_value_order",
            payload={
                "fulfillment_order_id": _fulfillment_order_id(order, context),
                "reason": "OTHER",
                "reason_notes": "High-value first-time international order requires review.",
            },
        ),
        planned_tool_call(
            case_id=case_id,
            tool="shopify_update_order_note",
            intent="annotate_high_value_review",
            payload={
                "order_id": _order_id(order),
                "note": "Held by Order Exception Agent: high-value policy review required.",
            },
        ),
    ]
    return _with_proposal(
        state,
        exception_type="high_value_review",
        summary=f"Order value ${total:.2f} matched a high-value manual review FOP.",
        recommendation="Hold fulfillment until an operator approves release.",
        tool_calls=tool_calls,
        confidence=0.88,
        rationale=[
            "The customer/order attributes matched the merchant's high-value review policy.",
            "The FOP explicitly says fulfillment release requires manual approval.",
        ],
    )


def inventory_conflict_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    inventory = context.get("inventory", {})
    out_of_stock_lines = inventory.get("out_of_stock_lines")
    has_oos = bool(inventory.get("has_out_of_stock_line") or out_of_stock_lines)

    if not has_oos:
        return _with_proposal(
            state,
            exception_type="inventory_conflict",
            summary="No out-of-stock line was found in the inventory context.",
            recommendation="Proceed with normal fulfillment.",
            tool_calls=[],
            confidence=0.8,
            rationale=["Inventory context did not indicate a conflict."],
        )

    message = (
        "Hi, one item in your order is temporarily out of stock. "
        "We can ship the available items now and follow up on the remaining item."
    )
    tool_calls = [
        planned_tool_call(
            case_id=case_id,
            tool="shopify_hold_fulfillment_order",
            intent="hold_inventory_conflict_order",
            payload={
                "fulfillment_order_id": _fulfillment_order_id(order, context),
                "reason": "INVENTORY_OUT_OF_STOCK",
                "reason_notes": "Inventory mismatch requires partial shipment approval.",
            },
        ),
        planned_tool_call(
            case_id=case_id,
            tool="gorgias_draft_reply",
            intent="draft_partial_ship_message",
            payload={
                "ticket_id": _ticket_id(context),
                "customer_email": _customer_email(order, context),
                "body_html": message,
            },
        ),
        planned_tool_call(
            case_id=case_id,
            tool="shopify_update_order_note",
            intent="annotate_inventory_conflict",
            payload={
                "order_id": _order_id(order),
                "note": "Inventory conflict: proposed partial shipment and customer draft.",
            },
        ),
    ]
    return _with_proposal(
        state,
        exception_type="inventory_conflict",
        summary="Inventory context shows at least one out-of-stock line.",
        recommendation="Hold fulfillment, draft a partial-shipment message, and wait for approval.",
        tool_calls=tool_calls,
        confidence=0.85,
        rationale=[
            (
                "The matched inventory FOP says to draft the customer message before "
                "fulfillment changes."
            ),
            "Partial shipment changes require human approval in Phase 0.",
        ],
    )


def _with_proposal(
    state: OrderExceptionState,
    *,
    exception_type: ExceptionType,
    summary: str,
    recommendation: str,
    tool_calls: list[JsonObject],
    confidence: float,
    rationale: list[str],
) -> OrderExceptionState:
    required_approvals = _string_list(state.get("required_approvals"))
    fop_ids = [
        str(fop.get("id"))
        for fop in _matched_fops(state)
        if isinstance(fop.get("id"), str)
    ]
    requires_human = bool(required_approvals) or tool_plan_requires_human(tool_calls)
    proposed_action: JsonObject = {
        "type": exception_type,
        "summary": summary,
        "recommendation": recommendation,
        "requires_human": requires_human,
        "required_approvals": required_approvals,
        "tool_calls": tool_calls,
        "matched_fop_ids": fop_ids,
        "hard_constraints": _string_list(state.get("hard_constraints")),
        "confidence": confidence,
        "rationale": rationale,
    }
    proposed_action = refine_subagent_proposal(
        state=state,
        proposed_action=proposed_action,
    )
    return {
        **state,
        "proposed_action": proposed_action,
        "trace_notes": [
            *_string_list(state.get("trace_notes")),
            f"{exception_type} subagent proposed {len(tool_calls)} tool call(s).",
        ],
    }


def _case_id(state: OrderExceptionState) -> str:
    return str(state.get("case_id") or "case_missing")


def _order(state: OrderExceptionState) -> JsonObject:
    order = state.get("order", {})
    return order if isinstance(order, dict) else {}


def _context(state: OrderExceptionState) -> JsonObject:
    context = state.get("context", {})
    return context if isinstance(context, dict) else {}


def _matched_fops(state: OrderExceptionState) -> list[JsonObject]:
    active_fops = state.get("active_fops", [])
    return [fop for fop in active_fops if isinstance(fop, dict)]


def _address_context(order: JsonObject, context: JsonObject) -> JsonObject:
    address = context.get("address_validation")
    if isinstance(address, dict):
        return address
    shipping_address = order.get("shipping_address")
    return shipping_address if isinstance(shipping_address, dict) else {}


def _address_message(
    order: JsonObject,
    issues: list[str],
    suggested_address: object,
) -> str:
    issue_text = ", ".join(issues) if issues else "the address may be incomplete"
    if isinstance(suggested_address, dict) and suggested_address:
        suggestion = ", ".join(str(value) for value in suggested_address.values() if value)
        return (
            f"Hi, we noticed {issue_text} on your order {_order_name(order)}. "
            f"Can you confirm whether this address is correct: {suggestion}?"
        )
    return (
        f"Hi, we noticed {issue_text} on your order {_order_name(order)}. "
        "Can you reply with your complete shipping address?"
    )


def _order_id(order: JsonObject) -> str:
    return _string(order.get("id") or order.get("order_id") or "unknown_order")


def _order_name(order: JsonObject) -> str:
    return _string(order.get("name") or order.get("order_number") or _order_id(order))


def _customer_email(order: JsonObject, context: JsonObject) -> str:
    customer = context.get("customer", order.get("customer", {}))
    email = _nested(customer, ["email"], order.get("email", "unknown@example.com"))
    return _string(email)


def _fulfillment_order_id(order: JsonObject, context: JsonObject) -> str:
    fulfillment = context.get("fulfillment", {})
    explicit = _nested(fulfillment, ["fulfillment_order_id"], None)
    if explicit is not None:
        return _string(explicit)
    fulfillment_orders = order.get("fulfillment_orders") or order.get("fulfillmentOrders")
    if isinstance(fulfillment_orders, list) and fulfillment_orders:
        first = fulfillment_orders[0]
        return _string(_nested(first, ["id"], "unknown_fulfillment_order"))
    return "unknown_fulfillment_order"


def _ticket_id(context: JsonObject) -> int:
    ticket = context.get("ticket", {})
    value = _nested(ticket, ["id"], context.get("ticket_id", 0))
    numeric = _number(value)
    return int(numeric) if numeric is not None else 0


def _nested(value: object, path: list[str], default: object = None) -> object:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return default
        current = current.get(part)
    return default if current is None else current


def _string(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None
