from __future__ import annotations

from ecom_shared import classify_order_exception


def test_classifies_risk_before_other_signals() -> None:
    result = classify_order_exception(
        {"financial_status": "failed"},
        {"risk": {"score": 88}},
    )

    assert result.exception_type == "fraud_triage"
    assert result.confidence == 0.9


def test_low_signal_case_routes_to_explicit_holding_lane() -> None:
    result = classify_order_exception(
        {"id": "gid://shopify/Order/1", "total_price": "42.00"},
        {},
    )

    assert result.exception_type == "high_value_review"
    assert result.confidence == 0.55
    assert "Low-confidence holding lane" in result.signals[0]
