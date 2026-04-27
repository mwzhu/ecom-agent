from __future__ import annotations

from ecom_shared import classify_order_exception


def test_classifies_risk_before_other_signals() -> None:
    result = classify_order_exception(
        {"financial_status": "failed"},
        {"risk": {"score": 88}},
    )

    assert result.exception_type == "fraud_triage"
    assert result.confidence == 0.9


def test_low_signal_case_routes_to_tier1_fallback_lane() -> None:
    result = classify_order_exception(
        {"id": "gid://shopify/Order/1", "total_price": "42.00"},
        {},
    )

    assert result.exception_type == "fraud_triage"
    assert result.confidence == 0.55
    assert "Low-confidence Tier 1 fallback" in result.signals[0]


def test_classifies_customer_address_change_request_before_other_context() -> None:
    result = classify_order_exception(
        {"id": "gid://shopify/Order/2"},
        {
            "customer_request": {"type": "address_change_request"},
            "shipment": {"status": "label_created"},
        },
    )

    assert result.exception_type == "address_change_request"
    assert result.confidence == 0.91


def test_classifies_stuck_in_transit_from_stale_carrier_scan() -> None:
    result = classify_order_exception(
        {"id": "gid://shopify/Order/3"},
        {"shipment": {"status": "in_transit", "days_since_last_scan": 4}},
    )

    assert result.exception_type == "stuck_in_transit"
    assert "4 day" in result.signals[0]


def test_generic_shipping_context_falls_back_to_wismo() -> None:
    result = classify_order_exception(
        {"id": "gid://shopify/Order/4"},
        {"shipment": {"status": "label_created", "tracking_number": "1Z999"}},
    )

    assert result.exception_type == "wismo"
    assert result.confidence == 0.7
