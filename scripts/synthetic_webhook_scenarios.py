#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

Provider = Literal["shopify", "stripe", "gorgias"]
JsonObject = dict[str, Any]

IMPLEMENTED_EXCEPTION_TYPES: tuple[str, ...] = (
    "fraud_triage",
    "address_change_request",
    "item_change_request",
    "order_cancellation_request",
    "inventory_conflict",
    "order_not_picked",
    "stuck_in_transit",
    "wismo",
    "delivered_not_received",
    "damaged_in_transit",
)


@dataclass(frozen=True)
class ScenarioContext:
    run_tag: str
    scenario_id: str
    sequence: int
    event_id: str
    order_name: str
    shop_index: int
    shop_domain: str
    stripe_account: str
    gorgias_domain: str
    expected_exception_types: frozenset[str]
    force_exception_type: bool = False


@dataclass(frozen=True)
class Scenario:
    id: str
    provider: Provider
    expected_exception_types: frozenset[str]
    profile_tags: frozenset[str]
    factory: Callable[[ScenarioContext], JsonObject]


def scenario_by_id() -> dict[str, Scenario]:
    return {scenario.id: scenario for scenario in SCENARIOS}


def scenarios_for_profile(profile: str) -> list[Scenario]:
    if profile in {"mixed", "chaos"}:
        return list(SCENARIOS)
    return [scenario for scenario in SCENARIOS if profile in scenario.profile_tags]


COVERAGE_SCENARIO_BY_EXCEPTION: dict[str, str] = {
    "fraud_triage": "fraud_high_score",
    "address_change_request": "address_change_pre_ship",
    "item_change_request": "item_change_zero_delta_swap",
    "order_cancellation_request": "order_cancellation_pre_ship",
    "inventory_conflict": "inventory_conflict_oos",
    "order_not_picked": "order_not_picked_sla_breach",
    "stuck_in_transit": "stuck_in_transit_stale_scan",
    "wismo": "wismo_tracking_request",
    "delivered_not_received": "delivered_not_received_claim",
    "damaged_in_transit": "damaged_in_transit_claim",
}


def _synthetic(ctx: ScenarioContext) -> JsonObject:
    return {
        "run_tag": ctx.run_tag,
        "scenario_id": ctx.scenario_id,
        "expected_exception_types": sorted(ctx.expected_exception_types),
        "shop_index": ctx.shop_index,
    }


def _with_synthetic(ctx: ScenarioContext, payload: JsonObject) -> JsonObject:
    payload = {**payload, "synthetic": _synthetic(ctx)}
    if ctx.force_exception_type:
        payload["exception_type"] = sorted(ctx.expected_exception_types)[0]
    return payload


def _order(ctx: ScenarioContext, *, total: str = "128.00", email: str | None = None) -> JsonObject:
    customer_email = email or f"synthetic.customer.{ctx.sequence}@example.com"
    return {
        "id": f"gid://shopify/Order/{9000000 + ctx.sequence}",
        "admin_graphql_api_id": f"gid://shopify/Order/{9000000 + ctx.sequence}",
        "name": ctx.order_name,
        "email": customer_email,
        "total_price": total,
        "financial_status": "paid",
        "customer": {
            "email": customer_email,
            "first_name": "Synthetic",
            "last_name": f"Customer {ctx.sequence}",
            "order_count": 4,
        },
        "shipping_address": {
            "address1": "210 Market St",
            "city": "San Francisco",
            "province": "CA",
            "zip": "94105",
            "country": "US",
        },
        "line_items": [
            {
                "id": f"gid://shopify/LineItem/{ctx.sequence}",
                "sku": f"SIM-SKU-{ctx.sequence % 20:02d}",
                "title": "Everyday Tee",
                "quantity": 1,
                "fulfillable_quantity": 1,
            }
        ],
    }


def _ticket(ctx: ScenarioContext, *, intent: str, body: str) -> JsonObject:
    return {
        "id": 700000 + ctx.sequence,
        "intent": intent,
        "subject": f"{ctx.order_name} support request",
        "body_text": body,
        "customer": {"email": f"synthetic.customer.{ctx.sequence}@example.com"},
        "order_name": ctx.order_name,
    }


def _reference_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def fraud_high_score(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/create",
            "risk_score": 92,
            "payment_status": "paid",
            "order": _order(ctx, total="742.00"),
            "context": {
                "risk": {"score": 92, "recommendation": "cancel"},
                "payment": {"status": "captured"},
            },
        },
    )


def fraud_medium_score(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/updated",
            "risk_score": 64,
            "order": _order(ctx, total="388.50"),
            "context": {
                "risk": {
                    "score": 64,
                    "signals": ["billing_shipping_distance", "velocity"],
                },
            },
        },
    )


def address_change_pre_ship(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/updated",
            "request_type": "address_change_request",
            "requested_address": {
                "address1": "1448 New Address Ave",
                "city": "Los Angeles",
                "province": "CA",
                "zip": "90017",
                "country": "US",
            },
            "order": _order(ctx, total="118.00"),
            "context": {
                "fulfillment": {"status": "open", "stalled": False},
                "ticket": {"id": 4100 + ctx.sequence, "intent": "address_change_request"},
            },
        },
    )


def address_change_missing_details(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/updated",
            "request_type": "change_address",
            "order": _order(ctx, total="92.00"),
            "context": {
                "ticket": {
                    "id": 4200 + ctx.sequence,
                    "intent": "change_address",
                    "body_text": "Can you send this to my office instead? I forgot the zip.",
                }
            },
        },
    )


def item_change_zero_delta_swap(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/updated",
            "request_type": "swap_item",
            "requested_changes": [
                {
                    "action": "swap",
                    "line_item_id": f"gid://shopify/CalculatedLineItem/{ctx.sequence}",
                    "variant_id": "gid://shopify/ProductVariant/2002",
                    "from": "Tee / M",
                    "to": "Tee / L",
                }
            ],
            "order": _order(ctx, total="58.00"),
            "context": {
                "item_change": {"payment_delta": 0},
                "ticket": {"id": 4300 + ctx.sequence, "intent": "swap_item"},
            },
        },
    )


def item_change_payment_delta_review(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/updated",
            "request_type": "item_change_request",
            "requested_changes": [
                {
                    "action": "add",
                    "variant_id": "gid://shopify/ProductVariant/2099",
                    "title": "Gift Wrap",
                    "quantity": 1,
                }
            ],
            "order": _order(ctx, total="144.00"),
            "context": {
                "item_change": {"payment_delta": 12.50},
                "ticket": {"id": 4400 + ctx.sequence, "intent": "item_change_request"},
            },
        },
    )


def order_cancellation_pre_ship(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/cancel_requested",
            "request_type": "cancel_order",
            "order": _order(ctx, total="214.00"),
            "context": {
                "fulfillment": {"status": "unfulfilled"},
                "ticket": {
                    "id": 4500 + ctx.sequence,
                    "intent": "cancel_order",
                    "body_text": "Please cancel before this ships.",
                },
            },
        },
    )


def inventory_conflict_oos(ctx: ScenarioContext) -> JsonObject:
    order = _order(ctx, total="173.00")
    order["line_items"] = [
        {
            "id": f"gid://shopify/LineItem/{ctx.sequence}",
            "sku": "SIM-OOS-M",
            "title": "Limited Hoodie",
            "quantity": 1,
            "fulfillable_quantity": 0,
            "inventory_status": "out_of_stock",
        }
    ]
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/updated",
            "order": order,
            "inventory": {
                "has_out_of_stock_line": True,
                "out_of_stock_lines": [{"sku": "SIM-OOS-M", "quantity": 1}],
            },
        },
    )


def order_not_picked_sla_breach(ctx: ScenarioContext) -> JsonObject:
    reference = _reference_now()
    created = reference - timedelta(hours=31)
    order = _order(ctx, total="86.00")
    order["created_at"] = created.isoformat().replace("+00:00", "Z")
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "fulfillments/update",
            "order": order,
            "context": {
                "fulfillment": {
                    "provider": "shipbob",
                    "order_id": 800000 + ctx.sequence,
                    "status": "awaiting_pick",
                    "pick_sla_breached": True,
                    "age_hours": 31,
                    "sla_hours": 24,
                    "reference_at": reference.isoformat().replace("+00:00", "Z"),
                }
            },
        },
    )


def stuck_in_transit_stale_scan(ctx: ScenarioContext) -> JsonObject:
    reference = _reference_now()
    last_scan = reference - timedelta(days=5, hours=3)
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "fulfillments/update",
            "order": _order(ctx, total="119.00"),
            "tracking_number": f"1ZSIM{ctx.sequence:08d}",
            "shipment_status": "in_transit",
            "days_since_last_scan": 5,
            "last_scan_at": last_scan.isoformat().replace("+00:00", "Z"),
            "context": {
                "shipment": {
                    "provider": "shipstation",
                    "shipment_id": 810000 + ctx.sequence,
                    "status": "in_transit",
                    "days_since_last_scan": 5,
                    "reference_at": reference.isoformat().replace("+00:00", "Z"),
                }
            },
        },
    )


def wismo_tracking_request(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "orders/updated",
            "request_type": "where_is_my_order",
            "order": _order(ctx, total="64.00"),
            "tracking_number": f"9400SIM{ctx.sequence:08d}",
            "shipment_status": "out_for_delivery",
            "context": {
                "ticket": {
                    "id": 4600 + ctx.sequence,
                    "intent": "where_is_my_order",
                    "body_text": "Can you send me the tracking update?",
                },
                "shipment": {
                    "status": "out_for_delivery",
                    "tracking_number": f"9400SIM{ctx.sequence:08d}",
                },
            },
        },
    )


def delivered_not_received_claim(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "fulfillments/update",
            "order": _order(ctx, total="156.00"),
            "reported_missing": True,
            "delivery_status": "delivered",
            "signature_on_file": False,
            "photo_evidence": True,
            "context": {
                "delivery": {
                    "reported_missing": True,
                    "status": "delivered",
                    "signature_on_file": False,
                    "photo_evidence": True,
                },
                "customer": {"missing_claim_count": 0, "order_count": 5},
                "ticket": {"id": 4700 + ctx.sequence, "intent": "delivered_not_received"},
            },
        },
    )


def damaged_in_transit_claim(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "shop_domain": ctx.shop_domain,
            "topic": "fulfillments/update",
            "order": _order(ctx, total="82.00"),
            "reported_damaged": True,
            "damage_severity": "minor",
            "photo_evidence": True,
            "context": {
                "delivery": {
                    "reported_damaged": True,
                    "damage_severity": "minor",
                    "photo_evidence": True,
                },
                "ticket": {"id": 4800 + ctx.sequence, "intent": "damaged_in_transit"},
            },
        },
    )


def stripe_dispute_created(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "id": ctx.event_id,
            "account": ctx.stripe_account,
            "type": "charge.dispute.created",
            "risk_score": 74,
            "charge_id": f"ch_sim_{ctx.sequence:08d}",
            "payment_status": "disputed",
            "order": _order(ctx, total="236.00"),
            "context": {
                "risk": {"score": 74},
                "payment": {
                    "status": "disputed",
                    "charge_id": f"ch_sim_{ctx.sequence:08d}",
                    "dispute_id": f"dp_sim_{ctx.sequence:08d}",
                },
            },
            "data": {
                "object": {
                    "id": f"dp_sim_{ctx.sequence:08d}",
                    "charge": f"ch_sim_{ctx.sequence:08d}",
                    "payment_intent": f"pi_sim_{ctx.sequence:08d}",
                    "reason": "fraudulent",
                }
            },
        },
    )


def stripe_payment_failed(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "id": ctx.event_id,
            "account": ctx.stripe_account,
            "type": "payment_intent.payment_failed",
            "risk_score": 58,
            "payment_status": "failed",
            "order": _order(ctx, total="319.00"),
            "context": {
                "risk": {"score": 58},
                "payment": {
                    "status": "failed",
                    "payment_intent_id": f"pi_sim_{ctx.sequence:08d}",
                },
            },
            "data": {
                "object": {
                    "id": f"pi_sim_{ctx.sequence:08d}",
                    "last_payment_error": {"code": "card_declined"},
                }
            },
        },
    )


def gorgias_address_change_ticket(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "gorgias_domain": ctx.gorgias_domain,
            "id": f"ticket_{ctx.sequence}",
            "ticket": _ticket(
                ctx,
                intent="address_change_request",
                body="I need to change my shipping address before it leaves the warehouse.",
            ),
            "request_type": "address_change_request",
            "requested_address": {
                "address1": "501 Updated Way",
                "city": "Austin",
                "province": "TX",
                "zip": "78701",
                "country": "US",
            },
            "order": _order(ctx, total="104.00"),
        },
    )


def gorgias_wismo_ticket(ctx: ScenarioContext) -> JsonObject:
    return _with_synthetic(
        ctx,
        {
            "gorgias_domain": ctx.gorgias_domain,
            "id": f"ticket_{ctx.sequence}",
            "ticket": _ticket(
                ctx,
                intent="wismo",
                body="Where is my order? The tracking page has not updated since yesterday.",
            ),
            "request_type": "wismo",
            "tracking_number": f"9205SIM{ctx.sequence:08d}",
            "shipment_status": "in_transit",
            "order": _order(ctx, total="71.00"),
        },
    )


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="fraud_high_score",
        provider="shopify",
        expected_exception_types=frozenset({"fraud_triage"}),
        profile_tags=frozenset({"fraud"}),
        factory=fraud_high_score,
    ),
    Scenario(
        id="fraud_medium_score",
        provider="shopify",
        expected_exception_types=frozenset({"fraud_triage"}),
        profile_tags=frozenset({"fraud"}),
        factory=fraud_medium_score,
    ),
    Scenario(
        id="address_change_pre_ship",
        provider="shopify",
        expected_exception_types=frozenset({"address_change_request"}),
        profile_tags=frozenset({"customer_changes"}),
        factory=address_change_pre_ship,
    ),
    Scenario(
        id="address_change_missing_details",
        provider="shopify",
        expected_exception_types=frozenset({"address_change_request"}),
        profile_tags=frozenset({"customer_changes"}),
        factory=address_change_missing_details,
    ),
    Scenario(
        id="item_change_zero_delta_swap",
        provider="shopify",
        expected_exception_types=frozenset({"item_change_request"}),
        profile_tags=frozenset({"customer_changes"}),
        factory=item_change_zero_delta_swap,
    ),
    Scenario(
        id="item_change_payment_delta_review",
        provider="shopify",
        expected_exception_types=frozenset({"item_change_request"}),
        profile_tags=frozenset({"customer_changes"}),
        factory=item_change_payment_delta_review,
    ),
    Scenario(
        id="order_cancellation_pre_ship",
        provider="shopify",
        expected_exception_types=frozenset({"order_cancellation_request"}),
        profile_tags=frozenset({"customer_changes"}),
        factory=order_cancellation_pre_ship,
    ),
    Scenario(
        id="inventory_conflict_oos",
        provider="shopify",
        expected_exception_types=frozenset({"inventory_conflict"}),
        profile_tags=frozenset({"fulfillment"}),
        factory=inventory_conflict_oos,
    ),
    Scenario(
        id="order_not_picked_sla_breach",
        provider="shopify",
        expected_exception_types=frozenset({"order_not_picked"}),
        profile_tags=frozenset({"fulfillment"}),
        factory=order_not_picked_sla_breach,
    ),
    Scenario(
        id="stuck_in_transit_stale_scan",
        provider="shopify",
        expected_exception_types=frozenset({"stuck_in_transit"}),
        profile_tags=frozenset({"delivery", "fulfillment"}),
        factory=stuck_in_transit_stale_scan,
    ),
    Scenario(
        id="wismo_tracking_request",
        provider="shopify",
        expected_exception_types=frozenset({"wismo"}),
        profile_tags=frozenset({"delivery"}),
        factory=wismo_tracking_request,
    ),
    Scenario(
        id="delivered_not_received_claim",
        provider="shopify",
        expected_exception_types=frozenset({"delivered_not_received"}),
        profile_tags=frozenset({"delivery"}),
        factory=delivered_not_received_claim,
    ),
    Scenario(
        id="damaged_in_transit_claim",
        provider="shopify",
        expected_exception_types=frozenset({"damaged_in_transit"}),
        profile_tags=frozenset({"delivery"}),
        factory=damaged_in_transit_claim,
    ),
    Scenario(
        id="stripe_dispute_created",
        provider="stripe",
        expected_exception_types=frozenset({"fraud_triage"}),
        profile_tags=frozenset({"stripe", "fraud"}),
        factory=stripe_dispute_created,
    ),
    Scenario(
        id="stripe_payment_failed",
        provider="stripe",
        expected_exception_types=frozenset({"fraud_triage"}),
        profile_tags=frozenset({"stripe", "fraud"}),
        factory=stripe_payment_failed,
    ),
    Scenario(
        id="gorgias_address_change_ticket",
        provider="gorgias",
        expected_exception_types=frozenset({"address_change_request"}),
        profile_tags=frozenset({"gorgias", "customer_changes"}),
        factory=gorgias_address_change_ticket,
    ),
    Scenario(
        id="gorgias_wismo_ticket",
        provider="gorgias",
        expected_exception_types=frozenset({"wismo"}),
        profile_tags=frozenset({"gorgias", "delivery"}),
        factory=gorgias_wismo_ticket,
    ),
)
