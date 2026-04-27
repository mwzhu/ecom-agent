from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.integrations import IntegrationProvider
from ecom_shared import ExceptionType, classify_order_exception

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class WebhookCaseSeed:
    exception_type: ExceptionType
    order: JsonObject
    context: JsonObject
    subject_ref: JsonObject


def build_webhook_case_seed(
    provider: IntegrationProvider,
    *,
    event_id: str,
    headers: dict[str, str],
    payload: JsonObject,
) -> WebhookCaseSeed:
    order = _order_from_payload(payload)
    context = _context_from_payload(provider, headers=headers, payload=payload, order=order)
    explicit_exception_type = payload.get("exception_type")
    if isinstance(explicit_exception_type, str):
        context["exception_type"] = explicit_exception_type
    classification = classify_order_exception(order, context)
    exception_type = classification.exception_type
    context["classification"] = {
        "source": "webhook_classifier_v1",
        "exception_type": exception_type,
        "confidence": classification.confidence,
        "signals": classification.signals,
        "provider": provider.value,
        "event_id": event_id,
    }
    return WebhookCaseSeed(
        exception_type=exception_type,
        order=order,
        context=context,
        subject_ref={
            "provider": provider.value,
            "event_id": event_id,
            "topic": headers.get("x-shopify-topic") or payload.get("topic"),
            "order_id": (
                order.get("id")
                or order.get("admin_graphql_api_id")
                or payload.get("order_id")
            ),
            "order_name": order.get("name") or order.get("order_number"),
            "customer_email": order.get("email") or _nested(context, ["customer", "email"]),
        },
    )


def webhook_external_account_id(
    provider: IntegrationProvider,
    *,
    headers: dict[str, str],
    payload: JsonObject,
) -> str | None:
    value: object
    match provider:
        case IntegrationProvider.SHOPIFY:
            value = headers.get("x-shopify-shop-domain") or payload.get("shop_domain")
        case IntegrationProvider.STRIPE:
            value = headers.get("stripe-account") or payload.get("account")
        case IntegrationProvider.GORGIAS:
            value = (
                headers.get("x-gorgias-domain")
                or payload.get("gorgias_domain")
                or _nested(payload, ["account", "domain"])
            )
        case IntegrationProvider.SHIPBOB:
            value = (
                headers.get("x-shipbob-merchant-id")
                or payload.get("shipbob_merchant_id")
                or payload.get("merchant_id")
            )
        case IntegrationProvider.SHIPSTATION:
            value = (
                headers.get("x-shipstation-account-id")
                or payload.get("shipstation_account_id")
                or payload.get("account_id")
            )
        case IntegrationProvider.GMAIL:
            value = (
                headers.get("x-goog-resource-id")
                or payload.get("emailAddress")
                or payload.get("email")
            )
    if isinstance(value, str) and value:
        return value.lower()
    if isinstance(value, int):
        return str(value)
    return None


def _order_from_payload(payload: JsonObject) -> JsonObject:
    explicit = payload.get("order")
    if isinstance(explicit, dict):
        return dict(explicit)
    return {
        key: value
        for key, value in payload.items()
        if key
        in {
            "id",
            "admin_graphql_api_id",
            "order_id",
            "name",
            "order_number",
            "email",
            "total_price",
            "totalPrice",
            "financial_status",
            "shipping_address",
            "customer",
            "fulfillment_orders",
            "line_items",
            "country_code",
        }
    }


def _context_from_payload(
    provider: IntegrationProvider,
    *,
    headers: dict[str, str],
    payload: JsonObject,
    order: JsonObject,
) -> JsonObject:
    raw_context = payload.get("context")
    context = dict(raw_context) if isinstance(raw_context, dict) else {}
    for key in (
        "risk",
        "payment",
        "inventory",
        "fulfillment",
        "shipment",
        "delivery",
        "ticket",
        "customer_request",
        "address_change",
        "item_change",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            context[key] = value
    customer = order.get("customer") or payload.get("customer")
    if isinstance(customer, dict):
        context["customer"] = customer
    _lift_risk_context(context, payload, order)
    _lift_payment_context(context, payload, order)
    _lift_inventory_context(provider, context, payload, order)
    _lift_customer_request_context(context, payload)
    _lift_shipment_context(provider, context, payload)
    context.setdefault(
        "webhook",
        {
            "provider": provider.value,
            "topic": headers.get("x-shopify-topic") or payload.get("topic"),
            "payload": payload,
        },
    )
    return context


def _lift_risk_context(context: JsonObject, payload: JsonObject, order: JsonObject) -> None:
    risk = context.get("risk")
    if isinstance(risk, dict) and risk:
        return
    score = payload.get("risk_score") or _nested(order, ["risk", "score"])
    risks = payload.get("risks")
    if score is None and isinstance(risks, list) and risks:
        scores = [_number(_nested(item, ["score"])) for item in risks if isinstance(item, dict)]
        score = max((item for item in scores if item is not None), default=None)
    if score is not None:
        context["risk"] = {"score": score}


def _lift_payment_context(context: JsonObject, payload: JsonObject, order: JsonObject) -> None:
    payment = context.get("payment")
    if isinstance(payment, dict) and payment:
        return
    status = payload.get("payment_status") or order.get("financial_status")
    charge_id = payload.get("charge_id")
    if status or charge_id:
        context["payment"] = {"status": status, "charge_id": charge_id}


def _lift_inventory_context(
    provider: IntegrationProvider,
    context: JsonObject,
    payload: JsonObject,
    order: JsonObject,
) -> None:
    inventory = context.get("inventory")
    if isinstance(inventory, dict) and inventory:
        return
    out_of_stock_lines: list[JsonObject] = []
    lines = payload.get("line_items") or order.get("line_items")
    if isinstance(lines, list):
        for line in lines:
            if not isinstance(line, dict):
                continue
            quantity = _number(line.get("fulfillable_quantity") or line.get("available_quantity"))
            status = str(line.get("status") or line.get("inventory_status") or "")
            if quantity == 0 or status in {"out_of_stock", "backordered", "inventory_conflict"}:
                out_of_stock_lines.append(line)
    status = str(payload.get("status") or payload.get("fulfillment_status") or "")
    if out_of_stock_lines or status in {"out_of_stock", "backordered", "inventory_conflict"}:
        context["inventory"] = {
            "has_out_of_stock_line": True,
            "out_of_stock_lines": out_of_stock_lines
            or [{"provider": provider.value, "status": status}],
        }

def _lift_customer_request_context(context: JsonObject, payload: JsonObject) -> None:
    customer_request = context.get("customer_request")
    if isinstance(customer_request, dict) and customer_request:
        return

    request_type = payload.get("request_type") or _nested(payload, ["ticket", "intent"])
    if isinstance(request_type, str) and request_type:
        context["customer_request"] = {"type": request_type}

    requested_address = payload.get("requested_address")
    if isinstance(requested_address, dict) and requested_address:
        existing = context.setdefault("customer_request", {})
        if isinstance(existing, dict):
            existing.setdefault("requested_address", requested_address)
        context.setdefault("address_change", {"requested_address": requested_address})

    requested_changes = payload.get("requested_changes")
    if isinstance(requested_changes, list) and requested_changes:
        existing = context.setdefault("customer_request", {})
        if isinstance(existing, dict):
            existing.setdefault("requested_changes", requested_changes)
        context.setdefault("item_change", {"requested_changes": requested_changes})


def _lift_shipment_context(
    provider: IntegrationProvider,
    context: JsonObject,
    payload: JsonObject,
) -> None:
    shipment = context.get("shipment")
    if isinstance(shipment, dict) and shipment:
        return

    tracking_number = payload.get("tracking_number") or payload.get("trackingNumber")
    shipment_status = (
        payload.get("shipment_status")
        or payload.get("tracking_status")
        or payload.get("carrier_status")
    )
    last_scan_at = payload.get("last_scan_at") or payload.get("lastCarrierScanAt")
    days_since_last_scan = payload.get("days_since_last_scan")
    if tracking_number or shipment_status or last_scan_at or days_since_last_scan is not None:
        context["shipment"] = {
            "provider": provider.value,
            "tracking_number": tracking_number,
            "status": shipment_status,
            "last_scan_at": last_scan_at,
            "days_since_last_scan": days_since_last_scan,
            "estimated_delivery": payload.get("estimated_delivery"),
            "shipment_id": payload.get("shipment_id"),
        }

    delivered_missing = payload.get("reported_missing")
    delivered_damaged = payload.get("reported_damaged")
    if delivered_missing or delivered_damaged:
        context["delivery"] = {
            "reported_missing": bool(delivered_missing),
            "reported_damaged": bool(delivered_damaged),
            "status": payload.get("delivery_status") or shipment_status,
            "tracking_number": tracking_number,
            "signature_on_file": payload.get("signature_on_file"),
            "photo_evidence": payload.get("photo_evidence"),
            "damage_severity": payload.get("damage_severity"),
        }


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
