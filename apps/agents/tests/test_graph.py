from __future__ import annotations

import importlib

from langgraph.types import Command

from agents.order_exception.graph import build_graph_for_local, graph
from agents.order_exception.llm_layer import SupervisorRefinement

graph_module = importlib.import_module("agents.order_exception.graph")


def test_fraud_triage_interrupts_for_high_score_cancel_and_refund_plan() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_fraud",
            "exception_type": "fraud_triage",
            "order": {"id": "gid://shopify/Order/1", "total_price": "742.00"},
            "context": {"risk": {"score": 85}},
        }
    )

    assert result["active_fops"][0]["id"] == "fop_fraud_score_cancel"
    assert result["proposed_action"]["requires_human"] is True
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_fraud_score_cancel"]
    assert [call["tool"] for call in result["proposed_action"]["tool_calls"]] == [
        "shopify_cancel_order",
        "shopify_create_refund",
    ]
    assert result["__interrupt__"][0].value["summary"].startswith("Fraud score 85")


def test_supervisor_classifies_exception_when_caller_omits_type() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_classified",
            "order": {"id": "gid://shopify/Order/10", "total_price": "640.00"},
            "context": {"risk": {"score": 82}},
        }
    )

    assert result["classification"]["exception_type"] == "fraud_triage"
    assert result["route"] == "fraud_triage"
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_fraud_score_cancel"]


def test_supervisor_records_anthropic_refinement_metadata(monkeypatch) -> None:
    def fake_refinement(**_: object) -> SupervisorRefinement:
        return SupervisorRefinement(
            exception_type="item_change_request",
            confidence=0.77,
            signals=["Anthropic confirmed a concrete pre-shipment item edit request."],
            source="anthropic_supervisor",
            model="claude-opus-test",
        )

    monkeypatch.setattr(graph_module, "refine_supervisor_route", fake_refinement)
    result = graph_module.graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_llm_route",
            "order": {"id": "gid://shopify/Order/11", "name": "#1011"},
            "context": {
                "customer_request": {"type": "item_change_request"},
                "item_change": {
                    "payment_delta": 0,
                    "requested_changes": [
                        {
                            "action": "add",
                            "variant_id": "gid://shopify/ProductVariant/21",
                            "quantity": 1,
                        }
                    ],
                },
            },
        }
    )

    assert result["classification"] == {
        "exception_type": "item_change_request",
        "confidence": 0.77,
        "signals": ["Anthropic confirmed a concrete pre-shipment item edit request."],
        "classifier": "anthropic_supervisor",
        "model": "claude-opus-test",
    }
    assert result["route"] == "item_change_request"


def test_human_resume_approves_planned_write_tool_calls() -> None:
    local_graph = build_graph_for_local()
    config = {"configurable": {"thread_id": "case_demo_fraud"}}

    first = local_graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_fraud",
            "exception_type": "fraud_triage",
            "order": {"id": "gid://shopify/Order/1", "total_price": "742.00"},
            "context": {"risk": {"score": 85}},
        },
        config=config,
    )
    resumed = local_graph.invoke(
        Command(resume={"decision": "approve", "source": "console", "actor": "ops@example.com"}),
        config=config,
    )

    assert "__interrupt__" in first
    assert resumed["resolution"]["status"] == "approved"
    assert resumed["resolution"]["validation_errors"] == []
    assert {call["status"] for call in resumed["tool_calls_so_far"]} == {"approved"}
    assert all(call["idempotency_key"] for call in resumed["tool_calls_so_far"])


def test_human_resume_modify_keeps_case_pending_without_approving_calls() -> None:
    local_graph = build_graph_for_local()
    config = {"configurable": {"thread_id": "case_demo_modify"}}

    local_graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_modify",
            "exception_type": "fraud_triage",
            "order": {"id": "gid://shopify/Order/2", "total_price": "742.00"},
            "context": {"risk": {"score": 85}},
        },
        config=config,
    )
    resumed = local_graph.invoke(
        Command(
            resume={
                "decision": "modify",
                "source": "console",
                "actor": "ops@example.com",
                "modification": {"operator_note": "Cancel only after manual note review."},
            }
        ),
        config=config,
    )

    assert resumed["resolution"]["status"] == "awaiting_modification"
    assert resumed.get("tool_calls_so_far", []) == []


def test_inventory_conflict_proposes_partial_shipment() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_inventory",
            "exception_type": "inventory_conflict",
            "order": {"id": "gid://shopify/Order/4", "email": "buyer@example.com"},
            "context": {
                "inventory": {
                    "has_out_of_stock_line": True,
                    "out_of_stock_lines": [{"sku": "TEE-BLACK-M"}],
                },
                "ticket": {"id": 987},
            },
        }
    )

    assert result["proposed_action"]["recommendation"].startswith("Hold fulfillment")
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_inventory_oos_partial_ship"]
    assert [call["tool"] for call in result["proposed_action"]["tool_calls"]] == [
        "shopify_hold_fulfillment_order",
        "gorgias_draft_reply",
        "shopify_update_order_note",
    ]


def test_low_risk_fraud_case_auto_resolves_without_write_tools() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_low_risk",
            "exception_type": "fraud_triage",
            "order": {"id": "gid://shopify/Order/5"},
            "context": {"risk": {"score": 12}},
        }
    )

    assert "__interrupt__" not in result
    assert result["resolution"]["status"] == "auto_resolved"
    assert result["proposed_action"]["requires_human"] is False
    assert result["tool_calls_so_far"] == []


def test_address_change_request_updates_shipping_address_before_shipment() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_address_change",
            "exception_type": "address_change_request",
            "order": {"id": "gid://shopify/Order/12", "name": "#1012"},
            "context": {
                "customer_request": {
                    "type": "address_change_request",
                    "requested_address": {
                        "address1": "123 New St",
                        "city": "Los Angeles",
                        "province": "CA",
                        "zip": "90001",
                        "country": "US",
                    },
                },
                "ticket": {"id": 456},
            },
        }
    )

    assert result["route"] == "address_change_request"
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_address_change_pre_ship"]
    assert [call["tool"] for call in result["proposed_action"]["tool_calls"][:3]] == [
        "shopify_hold_fulfillment_order",
        "shopify_update_shipping_address",
        "shopify_update_order_note",
    ]


def test_item_change_request_uses_shopify_order_edit_for_zero_delta_swap() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_item_change",
            "exception_type": "item_change_request",
            "order": {"id": "gid://shopify/Order/13", "name": "#1013"},
            "context": {
                "customer_request": {"type": "item_change_request"},
                "item_change": {
                    "payment_delta": 0,
                    "requested_changes": [
                        {
                            "action": "swap",
                            "line_item_id": "gid://shopify/CalculatedLineItem/1",
                            "variant_id": "gid://shopify/ProductVariant/2",
                        }
                    ],
                },
                "ticket": {"id": 457},
            },
        }
    )

    assert result["route"] == "item_change_request"
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_item_change_zero_delta"]
    assert "shopify_apply_order_edit" in [
        call["tool"] for call in result["proposed_action"]["tool_calls"]
    ]


def test_order_cancellation_request_cancels_pre_shipment_order() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_cancel",
            "exception_type": "order_cancellation_request",
            "order": {"id": "gid://shopify/Order/14", "name": "#1014"},
            "context": {"customer_request": {"type": "cancel_order"}},
        }
    )

    assert result["route"] == "order_cancellation_request"
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_pre_ship_cancellation"]
    assert result["proposed_action"]["tool_calls"][0]["tool"] == "shopify_cancel_order"


def test_order_not_picked_reads_3pl_status_and_drafts_update() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_not_picked",
            "exception_type": "order_not_picked",
            "order": {
                "id": "gid://shopify/Order/15",
                "name": "#1015",
                "created_at": "2026-04-25T08:00:00Z",
            },
            "context": {
                "fulfillment": {
                    "provider": "shipbob",
                    "order_id": 2001,
                    "age_hours": 30,
                    "sla_hours": 24,
                },
                "ticket": {"id": 458},
            },
        }
    )

    assert result["route"] == "order_not_picked"
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_pick_sla_breach_proactive_update"]
    assert result["proposed_action"]["tool_calls"][0]["tool"] == "shipbob_get_order"


def test_stuck_in_transit_reads_shipment_and_drafts_tracking_reply() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_stuck_transit",
            "exception_type": "stuck_in_transit",
            "order": {"id": "gid://shopify/Order/16", "name": "#1016"},
            "context": {
                "shipment": {
                    "provider": "shipstation",
                    "shipment_id": 3002,
                    "status": "in_transit",
                    "days_since_last_scan": 4,
                },
                "ticket": {"id": 459},
            },
        }
    )

    assert result["route"] == "stuck_in_transit"
    assert result["proposed_action"]["matched_fop_ids"] == [
        "fop_stuck_in_transit_customer_update"
    ]
    assert result["proposed_action"]["tool_calls"][0]["tool"] == "shipstation_get_shipment"


def test_wismo_drafts_concise_tracking_reply() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_wismo",
            "exception_type": "wismo",
            "order": {"id": "gid://shopify/Order/17", "name": "#1017"},
            "context": {
                "customer_request": {"type": "wismo"},
                "shipment": {"status": "out_for_delivery", "tracking_number": "1Z123"},
                "ticket": {"id": 460},
            },
        }
    )

    assert result["route"] == "wismo"
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_wismo_tracking_reply"]
    assert result["proposed_action"]["tool_calls"][-1]["tool"] == "gorgias_draft_reply"


def test_delivered_not_received_reviews_claim_history() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_missing_delivery",
            "exception_type": "delivered_not_received",
            "order": {
                "id": "gid://shopify/Order/18",
                "name": "#1018",
                "email": "buyer@example.com",
            },
            "context": {
                "delivery": {"reported_missing": True, "status": "delivered"},
                "customer": {
                    "order_count": 3,
                    "missing_claim_count": 0,
                    "email": "buyer@example.com",
                },
                "ticket": {"id": 461},
            },
        }
    )

    assert result["route"] == "delivered_not_received"
    assert result["proposed_action"]["matched_fop_ids"] == [
        "fop_delivered_not_received_review"
    ]
    assert [call["tool"] for call in result["proposed_action"]["tool_calls"][:2]] == [
        "shopify_search_orders",
        "gorgias_search_customer",
    ]


def test_damaged_in_transit_drafts_reply_and_note() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_damage",
            "exception_type": "damaged_in_transit",
            "order": {"id": "gid://shopify/Order/19", "name": "#1019"},
            "context": {
                "delivery": {
                    "reported_damaged": True,
                    "damage_severity": "minor",
                    "photo_evidence": True,
                },
                "ticket": {"id": 462},
            },
        }
    )

    assert result["route"] == "damaged_in_transit"
    assert result["proposed_action"]["matched_fop_ids"] == ["fop_damaged_in_transit_review"]
    assert [call["tool"] for call in result["proposed_action"]["tool_calls"][:2]] == [
        "gorgias_draft_reply",
        "shopify_update_order_note",
    ]
