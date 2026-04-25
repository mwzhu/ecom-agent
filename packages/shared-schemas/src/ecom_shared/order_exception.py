from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

ExceptionType = Literal[
    "address_validation",
    "fraud_triage",
    "payment_failure",
    "high_value_review",
    "inventory_conflict",
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

    risk_score = _number(_nested(context, ["risk", "score"], _nested(order, ["risk", "score"])))
    if risk_score is not None and risk_score >= 50:
        return ClassificationResult(
            exception_type="fraud_triage",
            confidence=0.9,
            signals=[f"Risk score {risk_score:.0f} is at or above review threshold."],
        )

    payment_status = str(
        _nested(context, ["payment", "status"], order.get("financial_status")) or ""
    )
    if payment_status in {"failed", "declined", "requires_payment_method", "requires_action"}:
        return ClassificationResult(
            exception_type="payment_failure",
            confidence=0.88,
            signals=[f"Payment status is {payment_status}."],
        )

    address = context.get("address_validation")
    if isinstance(address, dict) and (
        address.get("is_valid") is False
        or address.get("status") in {"invalid", "ambiguous", "incomplete"}
    ):
        return ClassificationResult(
            exception_type="address_validation",
            confidence=0.86,
            signals=["Address validation context is invalid or ambiguous."],
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

    total = _number(order.get("total_price") or order.get("totalPrice")) or 0
    country = str(
        order.get("country_code") or _nested(order, ["shipping_address", "country_code"]) or ""
    )
    order_count = _number(_nested(context, ["customer", "order_count"], 1))
    if total > 500 and country and country != "US" and order_count == 0:
        return ClassificationResult(
            exception_type="high_value_review",
            confidence=0.84,
            signals=[
                f"Order value ${total:.2f} is over threshold.",
                "Customer appears first-time and international.",
            ],
        )

    return ClassificationResult(
        exception_type="high_value_review",
        confidence=0.55,
        signals=[
            "Low-confidence holding lane: no risk, payment, address, inventory, or "
            "high-value signal matched; route to high_value_review for human review."
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


EXCEPTION_TYPES = {
    "address_validation",
    "fraud_triage",
    "payment_failure",
    "high_value_review",
    "inventory_conflict",
}
