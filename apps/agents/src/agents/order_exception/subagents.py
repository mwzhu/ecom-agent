from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from agents.order_exception.llm_layer import refine_subagent_proposal
from agents.order_exception.state import ExceptionType, OrderExceptionState
from agents.order_exception.tooling import planned_tool_call, tool_plan_requires_human

JsonObject = dict[str, Any]


def address_change_request_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    requested_address = _requested_address(context)

    if _is_shipped(context):
        message = (
            f"Hi, thanks for the address update request for order {_order_name(order)}. "
            "The package has already left the warehouse, so we can't safely edit the Shopify "
            "shipping address anymore. We'll review whether a carrier intercept is still possible "
            "and follow up with the next best option."
        )
        tool_calls = [
            *_draft_reply_calls(
                case_id=case_id,
                context=context,
                intent="draft_post_ship_address_change_reply",
                body_html=message,
            ),
            _order_note_call(
                case_id=case_id,
                order=order,
                intent="annotate_post_ship_address_change",
                note="Customer requested an address change after shipment; carrier intercept review needed.",
            ),
        ]
        return _with_proposal(
            state,
            exception_type="address_change_request",
            summary="Customer requested an address change, but the order is already in transit.",
            recommendation="Do not edit Shopify shipping data; review carrier intercept options and reply to the customer.",
            tool_calls=_compact_tool_calls(tool_calls),
            confidence=0.88,
            rationale=[
                "Once shipment has started, editing the Shopify shipping address is not sufficient to reroute the parcel.",
                "The safest action is to preserve the audit trail and respond with intercept guidance.",
            ],
        )

    if not requested_address:
        message = (
            f"Hi, we can update the shipping address on order {_order_name(order)} before it ships. "
            "Please reply with the full corrected address, including apartment or suite if needed."
        )
        tool_calls = [
            _hold_fulfillment_call(
                case_id=case_id,
                order=order,
                context=context,
                intent="hold_pending_address_change",
                reason_notes="Customer requested an address update before shipment.",
            ),
            *_draft_reply_calls(
                case_id=case_id,
                context=context,
                intent="draft_address_change_clarification",
                body_html=message,
            ),
            _order_note_call(
                case_id=case_id,
                order=order,
                intent="annotate_missing_address_change_details",
                note="Customer requested an address change but did not provide the full corrected address.",
            ),
        ]
        return _with_proposal(
            state,
            exception_type="address_change_request",
            summary="Customer asked to change the shipping address, but the corrected address is incomplete.",
            recommendation="Hold fulfillment, ask for the full corrected address, and keep the order frozen until the customer confirms.",
            tool_calls=_compact_tool_calls(tool_calls),
            confidence=0.87,
            rationale=[
                "The request is actionable only if the corrected shipping address is complete.",
                "Holding fulfillment avoids shipping to the outdated address while the team waits for confirmation.",
            ],
        )

    update_note = (
        "Shipping address updated by Order Exception Agent after customer request. "
        f"New destination: {_single_line_address(requested_address)}."
    )
    message = (
        f"Hi, we updated the shipping address on order {_order_name(order)} to "
        f"{_single_line_address(requested_address)} and placed a temporary fulfillment hold while the warehouse sync completes."
    )
    tool_calls = [
        _hold_fulfillment_call(
            case_id=case_id,
            order=order,
            context=context,
            intent="hold_for_address_change",
            reason_notes="Customer requested a shipping address update before pick/pack.",
        ),
        *_optional_tool_call(
            _three_pl_hold_call(
                case_id=case_id,
                context=context,
                intent="sync_address_change_to_3pl",
                reason="Customer requested an address update before shipment.",
            )
        ),
        planned_tool_call(
            case_id=case_id,
            tool="shopify_update_shipping_address",
            intent="update_shopify_shipping_address",
            payload={
                "order_id": _order_id(order),
                "shipping_address": requested_address,
            },
        ),
        _order_note_call(
            case_id=case_id,
            order=order,
            intent="annotate_address_change_request",
            note=update_note,
        ),
        *_draft_reply_calls(
            case_id=case_id,
            context=context,
            intent="draft_address_change_confirmation",
            body_html=message,
        ),
    ]
    return _with_proposal(
        state,
        exception_type="address_change_request",
        summary="Customer requested a pre-shipment address change and provided a complete replacement address.",
        recommendation="Hold fulfillment long enough to sync the address update, then confirm the change back to the customer.",
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.9,
        rationale=[
            "The order has not shipped yet, so updating the Shopify shipping address is still feasible.",
            "A temporary fulfillment hold reduces the risk of the warehouse using stale address data.",
        ],
    )


def item_change_request_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    requested_changes = _requested_item_changes(context)
    payment_delta = _number(_nested(context, ["item_change", "payment_delta"]))

    if _is_shipped(context):
        message = (
            f"Hi, order {_order_name(order)} has already moved past the editable pre-shipment window, "
            "so we can't safely change its items in place. We'll help with the best next option, "
            "such as a return, exchange, or a new order."
        )
        tool_calls: list[JsonObject | None] = [
            *_draft_reply_calls(
                case_id=case_id,
                context=context,
                intent="draft_late_item_change_reply",
                body_html=message,
            ),
            _order_note_call(
                case_id=case_id,
                order=order,
                intent="annotate_late_item_change_request",
                note="Customer requested an item change after the order moved beyond the editable window.",
            ),
        ]
        return _with_proposal(
            state,
            exception_type="item_change_request",
            summary="Customer requested an item change after the order stopped being safely editable.",
            recommendation="Do not edit the order in Shopify; reply with the post-shipment resolution path.",
            tool_calls=_compact_tool_calls(tool_calls),
            confidence=0.87,
            rationale=[
                "Once an order has shipped or moved too far into fulfillment, in-place order edits are risky.",
                "The safe path is customer communication plus a post-ship workflow such as an exchange.",
            ],
        )

    if not requested_changes:
        message = (
            f"Hi, we can usually adjust order {_order_name(order)} before it ships. "
            "Please reply with the exact item change you want, including the item to remove and the replacement variant or size if applicable."
        )
        tool_calls = [
            _hold_fulfillment_call(
                case_id=case_id,
                order=order,
                context=context,
                intent="hold_pending_item_change_details",
                reason_notes="Customer requested an item change but the requested edits are incomplete.",
            ),
            *_draft_reply_calls(
                case_id=case_id,
                context=context,
                intent="draft_item_change_clarification",
                body_html=message,
            ),
            _order_note_call(
                case_id=case_id,
                order=order,
                intent="annotate_missing_item_change_details",
                note="Customer requested an item change but the requested edits are incomplete.",
            ),
        ]
        return _with_proposal(
            state,
            exception_type="item_change_request",
            summary="Customer requested an item add/remove/swap, but the exact requested edit is incomplete.",
            recommendation="Hold fulfillment, ask the customer for the precise item change, and avoid editing the order until the request is unambiguous.",
            tool_calls=_compact_tool_calls(tool_calls),
            confidence=0.84,
            rationale=[
                "Editing an order without exact line or variant intent risks shipping the wrong merchandise.",
                "A temporary hold is safer than guessing which line item the customer wants changed.",
            ],
        )

    if payment_delta is not None and payment_delta != 0:
        message = (
            f"Hi, we reviewed the requested item change for order {_order_name(order)}. "
            "The updated basket changes the order total, so we'll review the payment difference before applying the edit."
        )
        tool_calls = [
            _hold_fulfillment_call(
                case_id=case_id,
                order=order,
                context=context,
                intent="hold_item_change_with_payment_delta",
                reason_notes="Customer requested an item change with a non-zero payment delta.",
            ),
            *_draft_reply_calls(
                case_id=case_id,
                context=context,
                intent="draft_item_change_payment_delta_reply",
                body_html=message,
            ),
            _order_note_call(
                case_id=case_id,
                order=order,
                intent="annotate_item_change_payment_delta",
                note=f"Customer requested an item change with payment delta {payment_delta:.2f}; operator review required before editing.",
            ),
        ]
        return _with_proposal(
            state,
            exception_type="item_change_request",
            summary="Customer requested an item change that would alter the order total.",
            recommendation="Hold fulfillment and review the payment delta before applying the order edit.",
            tool_calls=_compact_tool_calls(tool_calls),
            confidence=0.86,
            rationale=[
                "Non-zero payment deltas often require manual review for refunds, captures, or approval of added charges.",
                "The safest automated action is to freeze fulfillment and preserve a clear operator handoff.",
            ],
        )

    message = (
        f"Hi, we staged the requested item change for order {_order_name(order)} and placed a temporary fulfillment hold while the warehouse sync completes."
    )
    tool_calls = [
        _hold_fulfillment_call(
            case_id=case_id,
            order=order,
            context=context,
            intent="hold_for_item_change",
            reason_notes="Customer requested an item add/remove/swap before shipment.",
        ),
        planned_tool_call(
            case_id=case_id,
            tool="shopify_apply_order_edit",
            intent="apply_requested_item_change",
            payload={
                "order_id": _order_id(order),
                "quantity_changes": _quantity_change_payloads(requested_changes),
                "variant_additions": _variant_addition_payloads(requested_changes),
                "notify_customer": False,
                "staff_note": "Applied by Order Exception Agent after customer-requested item change.",
            },
        ),
        _order_note_call(
            case_id=case_id,
            order=order,
            intent="annotate_item_change_request",
            note="Customer-requested item change staged through Shopify order edit workflow.",
        ),
        *_draft_reply_calls(
            case_id=case_id,
            context=context,
            intent="draft_item_change_confirmation",
            body_html=message,
        ),
    ]
    return _with_proposal(
        state,
        exception_type="item_change_request",
        summary="Customer requested a pre-shipment item change that can be handled as a Shopify order edit.",
        recommendation="Hold fulfillment, apply the staged Shopify order edit, and confirm the change back to the customer.",
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.89,
        rationale=[
            "The requested edits are concrete and remain within the order's editable fulfillment window.",
            "Using Shopify's order edit workflow preserves the audit trail instead of relying on manual cancel-and-recreate work.",
        ],
    )


def order_cancellation_request_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)

    if _is_shipped(context):
        message = (
            f"Hi, order {_order_name(order)} has already shipped, so we can't cancel it in place anymore. "
            "We'll help with the post-shipment return or refusal path instead."
        )
        tool_calls = [
            *_draft_reply_calls(
                case_id=case_id,
                context=context,
                intent="draft_post_ship_cancellation_reply",
                body_html=message,
            ),
            _order_note_call(
                case_id=case_id,
                order=order,
                intent="annotate_post_ship_cancellation_request",
                note="Customer requested cancellation after shipment; return workflow required instead.",
            ),
        ]
        return _with_proposal(
            state,
            exception_type="order_cancellation_request",
            summary="Customer requested cancellation after the order was already shipped.",
            recommendation="Do not cancel the Shopify order; respond with the post-shipment return/refusal path.",
            tool_calls=_compact_tool_calls(tool_calls),
            confidence=0.9,
            rationale=[
                "Once the parcel is in transit, a Shopify order cancellation no longer cleanly reverses the shipment.",
                "The safer path is to redirect the customer into returns or carrier refusal handling.",
            ],
        )

    tool_calls = [
        planned_tool_call(
            case_id=case_id,
            tool="shopify_cancel_order",
            intent="cancel_order_per_customer_request",
            payload={
                "order_id": _order_id(order),
                "reason": "CUSTOMER",
                "refund": True,
                "restock": True,
                "notify_customer": True,
                "staff_note": "Canceled by Order Exception Agent after customer request.",
            },
        ),
        _order_note_call(
            case_id=case_id,
            order=order,
            intent="annotate_cancellation_request",
            note="Customer requested order cancellation before shipment.",
        ),
    ]
    return _with_proposal(
        state,
        exception_type="order_cancellation_request",
        summary="Customer requested a pre-shipment cancellation.",
        recommendation="Cancel the order, refund the payment to the original payment method, and restock the committed inventory.",
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.92,
        rationale=[
            "The order is still pre-shipment, so cancellation cleanly unwinds payment and inventory state.",
            "Using Shopify's native cancellation path keeps refunding and restocking tied to the order audit trail.",
        ],
    )


def fraud_triage_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    score = _number(_nested(context, ["risk", "score"], _nested(order, ["risk", "score"], 0))) or 0
    auto_actions = _string_list(state.get("auto_actions"))
    prior_chargebacks = int(_number(_nested(context, ["customer", "chargeback_count"], 0)) or 0)
    repeat_orders = int(_number(_nested(context, ["customer", "order_count"], 0)) or 0)
    blocklisted = _bool(
        _nested(context, ["customer", "is_blocklisted"], _nested(context, ["risk", "blocklisted"]))
    )

    if score > 80 and {"cancel_order", "refund_payment"}.issubset(set(auto_actions)):
        tool_calls = [
            planned_tool_call(
                case_id=case_id,
                tool="shopify_cancel_order",
                intent="cancel_high_fraud_order",
                payload={
                    "order_id": _order_id(order),
                    "reason": "FRAUD",
                    "refund": True,
                    "staff_note": "Canceled per merchant FOP: fraud score above 80.",
                },
            ),
            planned_tool_call(
                case_id=case_id,
                tool="shopify_create_refund",
                intent="refund_high_fraud_order",
                payload={
                    "order_id": _order_id(order),
                    "note": "Refund per fraud FOP: fraud score above 80.",
                    "notify_customer": True,
                    "refund_line_items": _json_list(context.get("refund_line_items")),
                    "transactions": _json_list(context.get("refund_transactions")),
                },
            ),
        ]
        return _with_proposal(
            state,
            exception_type="fraud_triage",
            summary=f"Fraud score {score:.0f} is above the merchant's cancel threshold.",
            recommendation="Cancel the order and issue the refund after approval.",
            tool_calls=_compact_tool_calls(tool_calls),
            confidence=0.9,
            rationale=[
                "A matched FOP says fraud scores above 80 should be canceled and refunded.",
                "Money movement still requires a human approval gate before execution.",
            ],
        )

    if score >= 50 or blocklisted:
        customer_query = _customer_history_query(order, context)
        rationale = [
            "The fraud score is in the merchant's gray-zone review band."
            if score >= 50
            else "The customer context indicates the buyer is blocked or previously abusive."
        ]
        if prior_chargebacks:
            rationale.append(f"The customer has {prior_chargebacks} prior chargeback(s).")
        if repeat_orders >= 3:
            rationale.append(f"The buyer also has {repeat_orders} prior completed order(s), so operator judgment still matters.")

        tool_calls = [
            _hold_fulfillment_call(
                case_id=case_id,
                order=order,
                context=context,
                intent="hold_medium_fraud_review",
                reason_notes=f"Fraud review required for score {score:.0f}.",
            ),
            *_optional_tool_call(
                _search_customer_orders_call(
                    case_id=case_id,
                    intent="fetch_customer_order_history_for_fraud",
                    query=customer_query,
                )
            ),
            *_optional_tool_call(
                _search_gorgias_customer_call(
                    case_id=case_id,
                    order=order,
                    context=context,
                    intent="fetch_customer_support_history_for_fraud",
                )
            ),
            _order_note_call(
                case_id=case_id,
                order=order,
                intent="annotate_medium_fraud_review",
                note=f"Held by Order Exception Agent: fraud review required (score {score:.0f}).",
            ),
        ]
        return _with_proposal(
            state,
            exception_type="fraud_triage",
            summary=f"Fraud score {score:.0f} requires gray-zone review before fulfillment release.",
            recommendation="Hold fulfillment, pull recent customer history, and route the order to manual fraud review.",
            tool_calls=_compact_tool_calls(tool_calls),
            confidence=0.82,
            rationale=rationale,
        )

    return _with_proposal(
        state,
        exception_type="fraud_triage",
        summary=f"Fraud score {score:.0f} is below the review threshold.",
        recommendation="Proceed with normal fulfillment.",
        tool_calls=[],
        confidence=0.84,
        rationale=["No matched FOP or risk signal requires a hold, cancellation, or refund."],
    )


def inventory_conflict_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    inventory = _inventory_context(context)
    out_of_stock_lines = _json_list(inventory.get("out_of_stock_lines"))
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

    restock_eta_days = _number(inventory.get("restock_eta_days"))
    partial_ship_ok = _bool(inventory.get("partial_shipment_available"), default=True)
    if partial_ship_ok and restock_eta_days is not None and restock_eta_days <= 5:
        message = (
            "Hi, one item in your order is temporarily out of stock. "
            f"We can ship the available items now and follow up on the remaining item in about {restock_eta_days:.0f} day(s)."
        )
        recommendation = "Hold fulfillment, offer a partial shipment, and keep the customer informed about the short restock delay."
        rationale = [
            "At least one line is out of stock, but the available items can still ship now.",
            "The short restock ETA makes a partial-shipment option reasonable.",
        ]
    elif partial_ship_ok:
        message = (
            "Hi, one item in your order is temporarily out of stock. "
            "We can ship the available items now and follow up separately on the remaining item."
        )
        recommendation = "Hold fulfillment, draft a partial-shipment message, and wait for approval."
        rationale = [
            "Inventory context shows at least one out-of-stock line.",
            "Partial shipment changes require human approval in this workflow.",
        ]
    else:
        message = (
            "Hi, one item in your order is temporarily out of stock, and the order can't ship complete right now. "
            "We're reviewing whether to hold for restock or offer a cancellation option."
        )
        recommendation = "Hold the order, communicate the stock issue, and review whether to wait for restock or cancel."
        rationale = [
            "The order has an out-of-stock line with no safe partial-shipment path.",
            "Customer communication should go out before any fulfillment change or cancellation.",
        ]

    tool_calls = [
        _hold_fulfillment_call(
            case_id=case_id,
            order=order,
            context=context,
            intent="hold_inventory_conflict_order",
            reason_notes="Inventory mismatch requires operator review before fulfillment changes.",
            reason="INVENTORY_OUT_OF_STOCK",
        ),
        _three_pl_hold_call(
            case_id=case_id,
            context=context,
            intent="sync_inventory_hold_to_3pl",
            reason="Inventory conflict requires merchant review before shipment.",
        ),
        *_draft_reply_calls(
            case_id=case_id,
            context=context,
            intent="draft_inventory_conflict_message",
            body_html=message,
        ),
        _order_note_call(
            case_id=case_id,
            order=order,
            intent="annotate_inventory_conflict",
            note="Inventory conflict detected: customer communication drafted and fulfillment hold proposed.",
        ),
    ]
    return _with_proposal(
        state,
        exception_type="inventory_conflict",
        summary="Inventory context shows at least one out-of-stock line after checkout.",
        recommendation=recommendation,
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.87,
        rationale=rationale,
    )


def order_not_picked_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    age_hours = _age_hours(order, context)
    sla_hours = _number(_nested(context, ["fulfillment", "sla_hours"], 24)) or 24

    if age_hours is not None and age_hours < sla_hours and not _bool(
        _nested(context, ["fulfillment", "pick_sla_breached"])
    ):
        return _with_proposal(
            state,
            exception_type="order_not_picked",
            summary="Fulfillment is still within the configured pick SLA window.",
            recommendation="Monitor the order without escalating to warehouse ops yet.",
            tool_calls=[],
            confidence=0.79,
            rationale=[
                f"Observed fulfillment age {age_hours:.0f}h is still below the pick SLA of {sla_hours:.0f}h."
            ],
        )

    message = (
        f"Hi, we're actively checking on order {_order_name(order)} because it has not been picked within the expected warehouse SLA. "
        "We'll update you as soon as we have the warehouse status."
    )
    tool_calls = [
        _three_pl_read_call(
            case_id=case_id,
            context=context,
            intent="fetch_stuck_pick_details",
        ),
        *_draft_reply_calls(
            case_id=case_id,
            context=context,
            intent="draft_stuck_pick_update",
            body_html=message,
        ),
        _order_note_call(
            case_id=case_id,
            order=order,
            intent="annotate_order_not_picked",
            note=(
                "Order exceeded warehouse pick SLA and needs follow-up. "
                f"Observed age: {age_hours:.0f}h." if age_hours is not None else "Order exceeded warehouse pick SLA and needs follow-up."
            ),
        ),
    ]
    return _with_proposal(
        state,
        exception_type="order_not_picked",
        summary="Order has not been picked within the warehouse SLA window.",
        recommendation="Pull the latest 3PL status, preserve the delay in the order notes, and send the customer a proactive delay update if a ticket exists.",
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.84,
        rationale=[
            "The order is stuck before shipment and needs warehouse-status confirmation.",
            "A proactive customer update reduces repeat WISMO tickets while the ops team investigates.",
        ],
    )


def stuck_in_transit_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    stale_days = _days_since_last_scan(context)
    shipment_status = _shipment_status(context)

    if stale_days is None and shipment_status not in {"delayed", "stuck_in_transit", "no_movement"}:
        return _with_proposal(
            state,
            exception_type="stuck_in_transit",
            summary="Shipment context does not show a stale in-transit package.",
            recommendation="Monitor the shipment normally.",
            tool_calls=[],
            confidence=0.76,
            rationale=["The case does not currently show a carrier-stall signal."],
        )

    message = (
        f"Hi, we're tracking order {_order_name(order)} and saw that the carrier hasn't posted a new scan recently. "
        "We're monitoring the shipment closely and will follow up with the next update or resolution path."
    )
    tool_calls = [
        _three_pl_shipment_call(
            case_id=case_id,
            context=context,
            intent="fetch_stuck_in_transit_details",
        ),
        *_draft_reply_calls(
            case_id=case_id,
            context=context,
            intent="draft_stuck_in_transit_reply",
            body_html=message,
        ),
        _order_note_call(
            case_id=case_id,
            order=order,
            intent="annotate_stuck_in_transit",
            note=(
                f"Shipment is stalled in transit with no scan for {stale_days:.0f} day(s)."
                if stale_days is not None
                else "Shipment appears stalled in transit; tracking review required."
            ),
        ),
    ]
    return _with_proposal(
        state,
        exception_type="stuck_in_transit",
        summary="Carrier tracking has gone stale while the package is still in transit.",
        recommendation="Pull the latest shipment context, reply with a tracking update if needed, and preserve an internal audit note for follow-up.",
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.85,
        rationale=[
            (
                f"No carrier scan has been recorded for {stale_days:.0f} day(s)."
                if stale_days is not None
                else "Shipment status is delayed without fresh carrier movement."
            ),
            "The package is not yet marked delivered, so the immediate next step is tracking confirmation rather than a delivery dispute workflow.",
        ],
    )


def wismo_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    shipment_status = _shipment_status(context)
    tracking_number = _string(
        _nested(context, ["shipment", "tracking_number"], _nested(context, ["delivery", "tracking_number"]))
    )
    eta = _string(_nested(context, ["shipment", "estimated_delivery"], _nested(context, ["delivery", "estimated_delivery"])))

    message = (
        f"Hi, here's the latest update for order {_order_name(order)}: status {shipment_status or 'in progress'}"
        + (f", tracking {tracking_number}" if tracking_number else "")
        + (f", estimated delivery {eta}" if eta else "")
        + "."
    )
    tool_calls = [
        _three_pl_shipment_call(
            case_id=case_id,
            context=context,
            intent="fetch_wismo_shipment_status",
        ),
        *_draft_reply_calls(
            case_id=case_id,
            context=context,
            intent="draft_wismo_reply",
            body_html=message,
        ),
    ]
    return _with_proposal(
        state,
        exception_type="wismo",
        summary="Customer asked for a routine shipping-status update.",
        recommendation="Pull the latest shipment context and send the customer a concise tracking update.",
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.88,
        rationale=[
            "The support context is a status inquiry rather than a delivery dispute.",
            "A concise tracking reply resolves most WISMO tickets without fulfillment changes.",
        ],
    )


def delivered_not_received_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    prior_claims = int(_number(_nested(context, ["customer", "missing_claim_count"], 0)) or 0)
    has_signature = _bool(_nested(context, ["delivery", "signature_on_file"]))
    trusted_buyer = int(_number(_nested(context, ["customer", "order_count"], 0)) or 0) >= 2

    if trusted_buyer and prior_claims == 0 and not has_signature:
        summary = "Trusted repeat customer reported a delivered-but-missing package with no prior missing-delivery history."
        recommendation = "Prepare a customer response and route the case for replacement or refund approval."
    else:
        summary = "Delivered-but-missing claim needs manual review before any replacement or refund."
        recommendation = "Preserve the delivery evidence, review prior claim history, and reply with the next investigative step."

    message = (
        f"Hi, thanks for reporting the delivery issue on order {_order_name(order)}. "
        "We're reviewing the carrier delivery details and your order history so we can confirm the next step."
    )
    tool_calls = [
        _search_customer_orders_call(
            case_id=case_id,
            intent="fetch_delivery_claim_history",
            query=_customer_history_query(order, context),
        ),
        _search_gorgias_customer_call(
            case_id=case_id,
            order=order,
            context=context,
            intent="fetch_support_claim_history",
        ),
        *_draft_reply_calls(
            case_id=case_id,
            context=context,
            intent="draft_delivered_not_received_reply",
            body_html=message,
        ),
        _order_note_call(
            case_id=case_id,
            order=order,
            intent="annotate_delivered_not_received_claim",
            note="Delivered-but-not-received claim opened; claim history and delivery evidence review required.",
        ),
    ]
    return _with_proposal(
        state,
        exception_type="delivered_not_received",
        summary=summary,
        recommendation=recommendation,
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.84,
        rationale=[
            "This is a delivery dispute rather than a normal WISMO tracking inquiry.",
            "Prior claim history and signature evidence materially change the refund or replacement risk profile.",
        ],
    )


def damaged_in_transit_subagent(state: OrderExceptionState) -> OrderExceptionState:
    case_id = _case_id(state)
    order = _order(state)
    context = _context(state)
    photo_evidence = _bool(_nested(context, ["delivery", "photo_evidence"]))
    severity = _string(_nested(context, ["delivery", "damage_severity"], "unknown"))
    refund_amount = _number(_nested(context, ["delivery", "suggested_partial_refund_amount"]))

    message = (
        f"Hi, we're sorry your order {_order_name(order)} arrived damaged. "
        "We're reviewing the evidence and the best recovery option right now."
    )
    tool_calls = [
        *_draft_reply_calls(
            case_id=case_id,
            context=context,
            intent="draft_damaged_in_transit_reply",
            body_html=message,
        ),
        _order_note_call(
            case_id=case_id,
            order=order,
            intent="annotate_damaged_in_transit_claim",
            note=(
                f"Damaged-in-transit claim opened (severity: {severity}; photo evidence: {'yes' if photo_evidence else 'no'})."
            ),
        ),
    ]
    if refund_amount is not None and refund_amount > 0:
        tool_calls.append(
            planned_tool_call(
                case_id=case_id,
                tool="shopify_create_refund",
                intent="issue_partial_damage_refund",
                payload={
                    "order_id": _order_id(order),
                    "note": "Partial refund proposed for damaged-in-transit claim.",
                    "notify_customer": True,
                    "refund_line_items": [],
                    "transactions": _json_list(context.get("refund_transactions")),
                    "shipping": {"fullRefund": False},
                },
            )
        )
    return _with_proposal(
        state,
        exception_type="damaged_in_transit",
        summary="Customer reported an order that arrived damaged in transit.",
        recommendation=(
            "Draft the customer reply immediately, record the damage context on the order, "
            "and route the case for refund or replacement approval."
        ),
        tool_calls=_compact_tool_calls(tool_calls),
        confidence=0.83,
        rationale=[
            "Damage claims require evidence preservation before deciding on refund, credit, or replacement.",
            "Customer communication should go out promptly even when the final resolution still needs approval.",
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
    compact_tool_calls = _compact_tool_calls(tool_calls)
    if exception_type == "wismo" and not compact_tool_calls:
        required_approvals = [
            *required_approvals,
            "No shipment lookup or customer-message draft could be prepared from the available context.",
        ]
    requires_human = bool(required_approvals) or tool_plan_requires_human(compact_tool_calls)
    proposed_action: JsonObject = {
        "type": exception_type,
        "summary": summary,
        "recommendation": recommendation,
        "requires_human": requires_human,
        "required_approvals": required_approvals,
        "tool_calls": compact_tool_calls,
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
            f"{exception_type} subagent proposed {len(compact_tool_calls)} tool call(s).",
        ],
    }


def _hold_fulfillment_call(
    *,
    case_id: str,
    order: JsonObject,
    context: JsonObject,
    intent: str,
    reason_notes: str,
    reason: str = "OTHER",
) -> JsonObject | None:
    fulfillment_order_id = _fulfillment_order_id(order, context)
    if not fulfillment_order_id:
        return None
    return planned_tool_call(
        case_id=case_id,
        tool="shopify_hold_fulfillment_order",
        intent=intent,
        payload={
            "fulfillment_order_id": fulfillment_order_id,
            "reason": reason,
            "reason_notes": reason_notes,
        },
    )


def _order_note_call(
    *,
    case_id: str,
    order: JsonObject,
    intent: str,
    note: str,
) -> JsonObject:
    return planned_tool_call(
        case_id=case_id,
        tool="shopify_update_order_note",
        intent=intent,
        payload={
            "order_id": _order_id(order),
            "note": note,
        },
    )


def _draft_reply_calls(
    *,
    case_id: str,
    context: JsonObject,
    intent: str,
    body_html: str,
) -> list[JsonObject]:
    ticket_id = _ticket_id(context)
    if ticket_id <= 0:
        return []
    return [
        planned_tool_call(
            case_id=case_id,
            tool="gorgias_draft_reply",
            intent=intent,
            payload={
                "ticket_id": ticket_id,
                "body_html": body_html,
            },
        )
    ]


def _search_customer_orders_call(
    *,
    case_id: str,
    intent: str,
    query: str | None,
) -> JsonObject | None:
    if not query:
        return None
    return planned_tool_call(
        case_id=case_id,
        tool="shopify_search_orders",
        intent=intent,
        payload={"query": query, "limit": 5},
        write=False,
    )


def _search_gorgias_customer_call(
    *,
    case_id: str,
    order: JsonObject,
    context: JsonObject,
    intent: str,
) -> JsonObject | None:
    email = _customer_email(order, context)
    if not email:
        return None
    return planned_tool_call(
        case_id=case_id,
        tool="gorgias_search_customer",
        intent=intent,
        payload={"email": email},
        write=False,
    )


def _three_pl_hold_call(
    *,
    case_id: str,
    context: JsonObject,
    intent: str,
    reason: str,
) -> JsonObject | None:
    fulfillment = _fulfillment_context(context)
    provider = _normalized_string(fulfillment.get("provider"))
    if provider == "shipbob":
        order_id = _int_from(fulfillment.get("provider_order_id") or fulfillment.get("order_id"))
        if order_id is None:
            return None
        return planned_tool_call(
            case_id=case_id,
            tool="shipbob_hold_order",
            intent=intent,
            payload={"order_id": order_id, "reason": reason},
        )
    if provider == "shipstation":
        order_id = _int_from(fulfillment.get("provider_order_id") or fulfillment.get("order_id"))
        hold_until_date = _string(fulfillment.get("hold_until_date"))
        if order_id is None or not hold_until_date:
            return None
        return planned_tool_call(
            case_id=case_id,
            tool="shipstation_hold_order",
            intent=intent,
            payload={"order_id": order_id, "hold_until_date": hold_until_date},
        )
    return None


def _three_pl_read_call(
    *,
    case_id: str,
    context: JsonObject,
    intent: str,
) -> JsonObject | None:
    fulfillment = _fulfillment_context(context)
    provider = _normalized_string(fulfillment.get("provider"))
    order_id = _int_from(fulfillment.get("provider_order_id") or fulfillment.get("order_id"))
    if order_id is None:
        return None
    if provider == "shipbob":
        return planned_tool_call(
            case_id=case_id,
            tool="shipbob_get_order",
            intent=intent,
            payload={"order_id": order_id},
            write=False,
        )
    if provider == "shipstation":
        return planned_tool_call(
            case_id=case_id,
            tool="shipstation_get_order",
            intent=intent,
            payload={"order_id": order_id},
            write=False,
        )
    return None


def _three_pl_shipment_call(
    *,
    case_id: str,
    context: JsonObject,
    intent: str,
) -> JsonObject | None:
    shipment = _shipment_context(context)
    provider = _normalized_string(
        shipment.get("provider") or _nested(context, ["fulfillment", "provider"])
    )
    shipment_id = _int_from(shipment.get("provider_shipment_id") or shipment.get("shipment_id"))
    if shipment_id is None:
        return None
    if provider == "shipbob":
        return planned_tool_call(
            case_id=case_id,
            tool="shipbob_get_shipment",
            intent=intent,
            payload={"shipment_id": shipment_id},
            write=False,
        )
    if provider == "shipstation":
        return planned_tool_call(
            case_id=case_id,
            tool="shipstation_get_shipment",
            intent=intent,
            payload={"shipment_id": shipment_id},
            write=False,
        )
    return None


def _requested_address(context: JsonObject) -> JsonObject:
    for path in (
        ["customer_request", "shipping_address"],
        ["customer_request", "requested_address"],
        ["address_change", "shipping_address"],
        ["address_change", "requested_address"],
    ):
        value = _nested(context, path)
        if isinstance(value, dict) and value:
            return dict(value)
    return {}


def _requested_item_changes(context: JsonObject) -> list[JsonObject]:
    for path in (
        ["customer_request", "requested_changes"],
        ["item_change", "requested_changes"],
        ["item_change", "changes"],
    ):
        value = _nested(context, path)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _quantity_change_payloads(changes: list[JsonObject]) -> list[JsonObject]:
    payloads: list[JsonObject] = []
    for change in changes:
        if _normalized_string(change.get("action")) not in {"remove", "swap", "change_quantity"}:
            continue
        quantity = _int_from(change.get("new_quantity"))
        if quantity is None and _normalized_string(change.get("action")) in {"remove", "swap"}:
            quantity = 0
        if quantity is None:
            continue
        line_item_id = _string(
            change.get("calculated_line_item_id")
            or change.get("line_item_id")
            or change.get("order_line_item_id")
        )
        if not line_item_id:
            continue
        payload: JsonObject = {
            "line_item_id": line_item_id,
            "quantity": quantity,
        }
        restock = change.get("restock")
        if isinstance(restock, bool):
            payload["restock"] = restock
        payloads.append(payload)
    return payloads


def _variant_addition_payloads(changes: list[JsonObject]) -> list[JsonObject]:
    payloads: list[JsonObject] = []
    for change in changes:
        if _normalized_string(change.get("action")) not in {"add", "swap"}:
            continue
        variant_id = _string(change.get("variant_id") or change.get("target_variant_id"))
        quantity = _int_from(change.get("quantity") or change.get("new_quantity")) or 1
        if not variant_id:
            continue
        payload: JsonObject = {
            "variant_id": variant_id,
            "quantity": quantity,
        }
        location_id = _string(change.get("location_id"))
        if location_id:
            payload["location_id"] = location_id
        allow_duplicates = change.get("allow_duplicates")
        if isinstance(allow_duplicates, bool):
            payload["allow_duplicates"] = allow_duplicates
        payloads.append(payload)
    return payloads


def _single_line_address(address: JsonObject) -> str:
    parts = [
        _string(address.get("name")),
        _string(address.get("address1")),
        _string(address.get("address2")),
        _string(address.get("city")),
        _string(address.get("province") or address.get("province_code")),
        _string(address.get("zip") or address.get("postal_code")),
        _string(address.get("country") or address.get("country_code")),
    ]
    return ", ".join(part for part in parts if part)


def _customer_history_query(order: JsonObject, context: JsonObject) -> str | None:
    customer = context.get("customer", order.get("customer", {}))
    customer_id = _string(_nested(customer, ["id"], ""))
    if customer_id:
        numeric_customer_id = customer_id.rsplit("/", 1)[-1]
        if numeric_customer_id.isdigit():
            return f"customer_id:{numeric_customer_id}"
    email = _customer_email(order, context)
    if email:
        return f'email:"{email}"'
    return None


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


def _inventory_context(context: JsonObject) -> JsonObject:
    inventory = context.get("inventory")
    return inventory if isinstance(inventory, dict) else {}


def _fulfillment_context(context: JsonObject) -> JsonObject:
    fulfillment = context.get("fulfillment")
    return fulfillment if isinstance(fulfillment, dict) else {}


def _shipment_context(context: JsonObject) -> JsonObject:
    for key in ("shipment", "delivery"):
        value = context.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _order_id(order: JsonObject) -> str:
    return _string(order.get("id") or order.get("order_id") or "unknown_order")


def _order_name(order: JsonObject) -> str:
    return _string(order.get("name") or order.get("order_number") or _order_id(order))


def _customer_email(order: JsonObject, context: JsonObject) -> str:
    customer = context.get("customer", order.get("customer", {}))
    email = _nested(customer, ["email"], order.get("email", ""))
    return _string(email)


def _fulfillment_order_id(order: JsonObject, context: JsonObject) -> str:
    fulfillment = context.get("fulfillment", {})
    explicit = _nested(fulfillment, ["fulfillment_order_id"], None)
    if explicit is not None:
        return _valid_shopify_gid(explicit, "FulfillmentOrder")
    fulfillment_orders = order.get("fulfillment_orders") or order.get("fulfillmentOrders")
    if isinstance(fulfillment_orders, list) and fulfillment_orders:
        first = fulfillment_orders[0]
        return _valid_shopify_gid(_nested(first, ["id"], None), "FulfillmentOrder")
    return ""


def _valid_shopify_gid(value: object, resource: str) -> str:
    text = _string(value)
    prefix = f"gid://shopify/{resource}/"
    return text if text.startswith(prefix) else ""


def _ticket_id(context: JsonObject) -> int:
    ticket = context.get("ticket", {})
    value = _nested(ticket, ["id"], context.get("ticket_id", 0))
    numeric = _number(value)
    return int(numeric) if numeric is not None else 0


def _shipment_status(context: JsonObject) -> str:
    return _normalized_string(
        _nested(context, ["shipment", "status"], _nested(context, ["delivery", "status"]))
    )


def _is_shipped(context: JsonObject) -> bool:
    status = _normalized_string(
        _nested(context, ["shipment", "status"], _nested(context, ["fulfillment", "status"]))
    )
    return status in {
        "attempted_delivery",
        "carrier_picked_up",
        "delayed",
        "delivered",
        "in_transit",
        "label_printed",
        "label_purchased",
        "out_for_delivery",
        "partially_fulfilled",
        "picked",
        "packed",
        "shipped",
    }


def _age_hours(order: JsonObject, context: JsonObject) -> float | None:
    explicit = _number(_nested(context, ["fulfillment", "age_hours"]))
    if explicit is not None:
        return explicit
    created_at = _datetime(
        order.get("created_at")
        or order.get("createdAt")
        or _nested(context, ["fulfillment", "queued_at"])
    )
    reference_at = _datetime(
        _nested(context, ["fulfillment", "reference_at"])
        or _nested(context, ["fulfillment", "updated_at"])
        or context.get("reference_at")
    ) or datetime.now(UTC)
    if created_at is None:
        return None
    return max(0.0, (reference_at - created_at).total_seconds() / 3600)


def _days_since_last_scan(context: JsonObject) -> float | None:
    explicit = _number(_nested(context, ["shipment", "days_since_last_scan"]))
    if explicit is not None:
        return explicit
    last_scan_at = _datetime(
        _nested(context, ["shipment", "last_scan_at"])
        or _nested(context, ["shipment", "lastCarrierScanAt"])
        or _nested(context, ["delivery", "last_scan_at"])
    )
    reference_at = _datetime(
        _nested(context, ["shipment", "reference_at"])
        or _nested(context, ["delivery", "reference_at"])
        or context.get("reference_at")
    ) or datetime.now(UTC)
    if last_scan_at is None:
        return None
    return max(0.0, (reference_at - last_scan_at).total_seconds() / 86400)


def _compact_tool_calls(tool_calls: Iterable[JsonObject | None]) -> list[JsonObject]:
    return [call for call in tool_calls if isinstance(call, dict)]


def _optional_tool_call(tool_call: JsonObject | None) -> list[JsonObject]:
    return [tool_call] if tool_call is not None else []


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


def _json_list(value: object) -> list[JsonObject]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _normalized_string(value: object) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    return ""


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


def _int_from(value: object) -> int | None:
    numeric = _number(value)
    if numeric is None:
        return None
    return int(numeric)


def _bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return default


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
