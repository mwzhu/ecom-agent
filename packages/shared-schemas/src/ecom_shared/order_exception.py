from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

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

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ClassificationResult:
    exception_type: ExceptionType
    confidence: float
    signals: list[str]


def classify_order_exception(order: JsonObject, context: JsonObject) -> ClassificationResult:
    explicit = context.get("exception_type")
    if isinstance(explicit, str) and explicit in EXCEPTION_TYPES:
        return ClassificationResult(
            exception_type=cast(ExceptionType, explicit),
            confidence=0.99,
            signals=["Caller supplied a supported exception_type."],
        )

    request_type = _request_type(context)
    if request_type in {"cancel_order", "cancel", "order_cancellation_request"}:
        return ClassificationResult(
            exception_type="order_cancellation_request",
            confidence=0.92,
            signals=["Customer support context indicates a cancellation request."],
        )

    if request_type in {
        "item_change_request",
        "change_item",
        "swap_item",
        "remove_item",
        "add_item",
        "size_change",
    }:
        return ClassificationResult(
            exception_type="item_change_request",
            confidence=0.91,
            signals=["Customer support context indicates an item add/remove/swap request."],
        )

    if request_type in {
        "address_change_request",
        "change_address",
        "address_update",
        "shipping_address_change",
    }:
        return ClassificationResult(
            exception_type="address_change_request",
            confidence=0.91,
            signals=["Customer support context indicates a post-order address change request."],
        )

    if request_type in {"delivered_not_received", "missing_delivery", "porch_piracy"} or bool(
        _nested(context, ["delivery", "reported_missing"])
    ):
        return ClassificationResult(
            exception_type="delivered_not_received",
            confidence=0.93,
            signals=["Delivery context indicates a delivered-but-not-received claim."],
        )

    if request_type in {"damaged_in_transit", "damage_claim"} or bool(
        _nested(context, ["delivery", "reported_damaged"])
    ):
        return ClassificationResult(
            exception_type="damaged_in_transit",
            confidence=0.92,
            signals=["Delivery context indicates a damaged-in-transit claim."],
        )

    risk_score = _number(_nested(context, ["risk", "score"], _nested(order, ["risk", "score"])))
    if risk_score is not None and risk_score >= 50:
        return ClassificationResult(
            exception_type="fraud_triage",
            confidence=0.9,
            signals=[f"Risk score {risk_score:.0f} is at or above review threshold."],
        )

    if _is_order_not_picked(order, context):
        age_hours = _age_hours(order, context)
        return ClassificationResult(
            exception_type="order_not_picked",
            confidence=0.89,
            signals=[
                (
                    f"Fulfillment age {age_hours:.0f}h exceeded the configured pick SLA."
                    if age_hours is not None
                    else "Fulfillment context indicates the order is stuck before pick."
                )
            ],
        )

    inventory = context.get("inventory")
    if isinstance(inventory, dict) and (
        inventory.get("has_out_of_stock_line") or inventory.get("out_of_stock_lines")
    ):
        return ClassificationResult(
            exception_type="inventory_conflict",
            confidence=0.86,
            signals=["Inventory context includes an out-of-stock line."],
        )

    if _is_stuck_in_transit(context):
        stale_days = _days_since_last_scan(context)
        return ClassificationResult(
            exception_type="stuck_in_transit",
            confidence=0.87,
            signals=[
                (
                    f"Shipment has not received a carrier scan for {stale_days:.0f} day(s)."
                    if stale_days is not None
                    else "Shipment context indicates the package is stuck in transit."
                )
            ],
        )

    if request_type in {"wismo", "where_is_my_order", "tracking_request"}:
        return ClassificationResult(
            exception_type="wismo",
            confidence=0.84,
            signals=["Customer support context is a WISMO tracking inquiry."],
        )

    if _has_shipping_context(context):
        return ClassificationResult(
            exception_type="wismo",
            confidence=0.7,
            signals=[
                "Low-confidence Tier 1 fallback: shipment or delivery context exists without "
                "a stronger dispute signal, so route to the least-destructive tracking lane."
            ],
        )

    return ClassificationResult(
        exception_type="fraud_triage",
        confidence=0.55,
        signals=[
            "Low-confidence Tier 1 fallback: no Tier 1 exception matched, so route to "
            "fraud_triage for conservative review without taking a destructive action."
        ],
    )


def _nested(value: object, path: list[str], default: object = None) -> object:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return default
        current = current.get(part)
    return default if current is None else current


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


TIER_1_EXCEPTION_TYPES = {
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
}

EXCEPTION_TYPES = TIER_1_EXCEPTION_TYPES


def _request_type(context: JsonObject) -> str:
    for path in (
        ["customer_request", "type"],
        ["ticket", "intent"],
        ["ticket", "type"],
        ["ticket", "category"],
        ["support_request", "type"],
    ):
        value = _nested(context, path)
        if isinstance(value, str) and value:
            return value.strip().lower()
    return ""


def _has_shipping_context(context: JsonObject) -> bool:
    return any(isinstance(context.get(key), dict) and context.get(key) for key in ("shipment", "delivery"))


def _is_order_not_picked(order: JsonObject, context: JsonObject) -> bool:
    fulfillment_status = _normalized_string(
        _nested(context, ["fulfillment", "status"], order.get("displayFulfillmentStatus"))
    )
    if fulfillment_status in {"picked", "packed", "fulfilled", "in_transit", "delivered", "shipped"}:
        return False

    if bool(_nested(context, ["fulfillment", "pick_sla_breached"])):
        return True

    age_hours = _age_hours(order, context)
    sla_hours = _number(_nested(context, ["fulfillment", "sla_hours"], 24))
    if age_hours is None or sla_hours is None:
        return fulfillment_status in {
            "awaiting_pick",
            "backlog",
            "not_picked",
            "open",
            "pending",
            "unfulfilled",
        } and bool(_nested(context, ["fulfillment", "stalled"]))
    return age_hours >= sla_hours and fulfillment_status in {
        "",
        "awaiting_pick",
        "backlog",
        "not_picked",
        "open",
        "pending",
        "unfulfilled",
    }


def _is_stuck_in_transit(context: JsonObject) -> bool:
    shipment_status = _normalized_string(
        _nested(context, ["shipment", "status"], _nested(context, ["delivery", "status"]))
    )
    if shipment_status in {"delivered", "returned", "cancelled", "damaged"}:
        return False
    if shipment_status in {"stuck_in_transit", "no_movement", "delayed"}:
        return True
    stale_days = _days_since_last_scan(context)
    return stale_days is not None and stale_days >= 3 and shipment_status in {
        "",
        "carrier_picked_up",
        "exception",
        "in_transit",
        "label_printed",
        "label_purchased",
        "out_for_delivery",
    }


def _age_hours(order: JsonObject, context: JsonObject) -> float | None:
    explicit = _number(_nested(context, ["fulfillment", "age_hours"]))
    if explicit is not None:
        return explicit
    created_at = _datetime(
        order.get("created_at")
        or order.get("createdAt")
        or _nested(context, ["fulfillment", "created_at"])
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
    explicit = _number(
        _nested(context, ["shipment", "days_since_last_scan"], _nested(context, ["delivery", "days_since_last_scan"]))
    )
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


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _normalized_string(value: object) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    return ""
