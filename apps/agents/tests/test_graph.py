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
            exception_type="payment_failure",
            confidence=0.77,
            signals=["Anthropic confirmed failed payment evidence."],
            source="anthropic_supervisor",
            model="claude-sonnet-test",
        )

    monkeypatch.setattr(graph_module, "refine_supervisor_route", fake_refinement)
    result = graph_module.graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_llm_route",
            "order": {"id": "gid://shopify/Order/11", "email": "buyer@example.com"},
            "context": {"payment": {"status": "failed", "charge_id": "ch_123"}},
        }
    )

    assert result["classification"] == {
        "exception_type": "payment_failure",
        "confidence": 0.77,
        "signals": ["Anthropic confirmed failed payment evidence."],
        "classifier": "anthropic_supervisor",
        "model": "claude-sonnet-test",
    }
    assert result["route"] == "payment_failure"


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


def test_high_value_review_matches_fop_and_requires_release_approval() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_high_value",
            "exception_type": "high_value_review",
            "order": {
                "id": "gid://shopify/Order/2",
                "total_price": "812.00",
                "country_code": "CA",
            },
            "context": {"customer": {"order_count": 0}},
        }
    )

    assert result["proposed_action"]["requires_human"] is True
    assert result["proposed_action"]["required_approvals"] == ["release_fulfillment"]
    assert result["proposed_action"]["matched_fop_ids"] == [
        "fop_high_value_first_time_international"
    ]
    assert result["route"] == "high_value_review"


def test_address_validation_drafts_message_before_fulfillment_change() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_address",
            "exception_type": "address_validation",
            "order": {
                "id": "gid://shopify/Order/3",
                "name": "#1003",
                "email": "customer@example.com",
            },
            "context": {
                "address_validation": {
                    "is_valid": False,
                    "issues": ["missing apartment number"],
                    "suggested_address": {"address1": "100 Market St", "city": "San Francisco"},
                },
                "ticket": {"id": 12345},
            },
        }
    )

    assert result["active_fops"][0]["id"] == "fop_invalid_address_hold"
    assert result["proposed_action"]["requires_human"] is True
    assert result["proposed_action"]["tool_calls"][1]["tool"] == "gorgias_draft_reply"
    assert "Draft the customer-facing message" in result["fop_prompt_block"]


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


def test_payment_failure_drafts_reauthorization_message() -> None:
    result = graph.invoke(
        {
            "merchant_id": "demo-merchant",
            "case_id": "case_demo_payment",
            "exception_type": "payment_failure",
            "order": {"id": "gid://shopify/Order/6", "email": "buyer@example.com"},
            "context": {
                "payment": {"status": "failed", "charge_id": "ch_123"},
                "ticket": {"id": 555},
            },
        }
    )

    assert result["active_fops"][0]["id"] == "fop_payment_failure_reauth"
    assert result["proposed_action"]["requires_human"] is True
    assert result["proposed_action"]["tool_calls"][0]["tool"] == "stripe_get_charge"
    assert result["proposed_action"]["tool_calls"][0]["write"] is False
    assert result["proposed_action"]["tool_calls"][1]["tool"] == "gorgias_draft_reply"


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
