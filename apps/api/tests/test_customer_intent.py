from __future__ import annotations

import pytest

import api.webhooks.customer_intent as customer_intent
from api.integrations import IntegrationProvider
from api.webhooks.classifier import build_webhook_case_seed


def test_llm_classifies_address_intent_and_extracts_requested_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_invoke_json(**kwargs: object) -> dict[str, object]:
        body = kwargs["body"]
        assert isinstance(body, dict)
        if body.get("workflow") == "address_change_request":
            return {
                "requested_address": {
                    "address1": "515 Valencia St",
                    "address2": None,
                    "city": "San Francisco",
                    "province": "CA",
                    "zip": "94110",
                    "country": "US",
                },
                "order_reference": "#1036",
                "is_complete": True,
                "needs_clarification": False,
                "confidence": 0.92,
                "evidence": ["Message contains street, city, state, and ZIP."],
            }
        return {
            "intent": "address_change_request",
            "confidence": 0.91,
            "order_reference": "#1036",
            "evidence": ["Customer asked to change the shipping address."],
            "needs_human_triage": False,
        }

    monkeypatch.setenv("ORDER_EXCEPTION_CUSTOMER_LANGUAGE_LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(customer_intent, "_invoke_json", fake_invoke_json)
    context = {
        "ticket": {
            "excerpt": (
                "Hi, I just placed order #1036. Can you change the shipping address "
                "to 515 Valencia St, San Francisco, CA 94110 before it ships?"
            )
        }
    }

    classification = customer_intent.apply_customer_language_understanding(
        order={"name": "#1036"},
        context=context,
    )

    assert classification is not None
    assert classification.exception_type == "address_change_request"
    assert classification.confidence == 0.91
    assert context["customer_request"]["type"] == "address_change_request"
    assert context["customer_request"]["order_reference"] == "#1036"
    assert context["customer_request"]["confidence"] == 0.92
    assert context["customer_request"]["is_complete"] is True
    assert context["customer_request"]["needs_clarification"] is False
    assert context["customer_request"]["requested_address"] == {
        "address1": "515 Valencia St",
        "address2": None,
        "city": "San Francisco",
        "province": "CA",
        "zip": "94110",
        "country": "US",
    }
    assert context["customer_request"]["requested_address_confidence"] == 0.92
    assert context["customer_request"]["extraction_sources"] == [
        "llm_intent_classifier",
        "llm_address_change_extractor",
    ]
    assert "Customer asked to change the shipping address." in context["customer_request"]["evidence"]
    assert "Message contains street, city, state, and ZIP." in context["customer_request"]["evidence"]


def test_wismo_text_does_not_populate_address_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_invoke_json(**kwargs: object) -> dict[str, object]:
        body = kwargs["body"]
        assert isinstance(body, dict)
        if body.get("workflow") == "wismo":
            return {
                "order_reference": "#1036",
                "tracking_status_ask": True,
                "delivery_promise": None,
                "is_complete": True,
                "needs_clarification": False,
                "confidence": 0.88,
                "evidence": ["Customer asks where the order is."],
            }
        return {
            "intent": "wismo",
            "confidence": 0.9,
            "order_reference": "#1036",
            "evidence": ["Customer asks for tracking."],
            "needs_human_triage": False,
        }

    monkeypatch.setenv("ORDER_EXCEPTION_CUSTOMER_LANGUAGE_LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(customer_intent, "_invoke_json", fake_invoke_json)
    context = {"ticket": {"excerpt": "Where is my order #1036? Has it shipped yet?"}}

    classification = customer_intent.apply_customer_language_understanding(
        order={"name": "#1036"},
        context=context,
    )

    assert classification is not None
    assert classification.exception_type == "wismo"
    assert context["customer_request"]["type"] == "wismo"
    assert "requested_address" not in context["customer_request"]
    assert "address_change" not in context


def test_invalid_llm_json_falls_back_to_deterministic_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_invoke_json(**_: object) -> dict[str, object]:
        raise ValueError("invalid json")

    monkeypatch.setenv("ORDER_EXCEPTION_CUSTOMER_LANGUAGE_LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(customer_intent, "_invoke_json", fake_invoke_json)
    context = {
        "ticket": {
            "excerpt": "Can you change the shipping address on order #1036 before it ships?"
        }
    }

    classification = customer_intent.apply_customer_language_understanding(
        order={"name": "#1036"},
        context=context,
    )

    assert classification is not None
    assert classification.exception_type == "address_change_request"
    assert context["language_understanding"]["classification"]["source"] == "deterministic_fallback"
    assert context["customer_request"]["type"] == "address_change_request"


def test_low_confidence_extraction_does_not_overwrite_explicit_structured_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_address = {
        "address1": "100 Market St",
        "city": "San Francisco",
        "province": "CA",
        "zip": "94105",
        "country": "US",
    }

    def fake_invoke_json(**kwargs: object) -> dict[str, object]:
        body = kwargs["body"]
        assert isinstance(body, dict)
        if body.get("workflow") == "address_change_request":
            return {
                "requested_address": {
                    "address1": "515 Valencia St",
                    "city": "San Francisco",
                    "province": "CA",
                    "zip": "94110",
                    "country": "US",
                },
                "order_reference": "#1036",
                "is_complete": True,
                "needs_clarification": False,
                "confidence": 0.42,
                "evidence": ["Low confidence parse."],
            }
        return {
            "intent": "address_change_request",
            "confidence": 0.9,
            "order_reference": "#1036",
            "evidence": ["Customer asks for an address change."],
            "needs_human_triage": False,
        }

    monkeypatch.setenv("ORDER_EXCEPTION_CUSTOMER_LANGUAGE_LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(customer_intent, "_invoke_json", fake_invoke_json)
    context = {
        "customer_request": {
            "type": "address_change_request",
            "requested_address": explicit_address,
        },
        "ticket": {
            "excerpt": "Please update order #1036 to 515 Valencia St, San Francisco CA 94110."
        },
    }

    customer_intent.apply_customer_language_understanding(order={"name": "#1036"}, context=context)

    assert context["customer_request"]["requested_address"] == explicit_address
    assert context["customer_request"]["extraction_conflicts"] == [
        {
            "field": "customer_request.requested_address",
            "existing": explicit_address,
            "extracted": {
                "address1": "515 Valencia St",
                "address2": None,
                "city": "San Francisco",
                "province": "CA",
                "zip": "94110",
                "country": "US",
            },
            "source": "llm_address_change_extractor",
            "confidence": 0.42,
            "resolution": "low_confidence_extracted_value_ignored",
        }
    ]


def test_operational_trigger_without_customer_text_does_not_call_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(**_: object) -> dict[str, object]:
        raise AssertionError("LLM should not be called for non-language operational triggers.")

    monkeypatch.setenv("ORDER_EXCEPTION_CUSTOMER_LANGUAGE_LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(customer_intent, "_invoke_json", fail_if_called)

    classification = customer_intent.apply_customer_language_understanding(
        order={"name": "#1036"},
        context={"risk": {"score": 91}},
    )

    assert classification is None


def test_synthetic_shopify_tags_still_work_without_customer_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(**_: object) -> dict[str, object]:
        raise AssertionError("LLM should not be called for tag-only demo payloads.")

    monkeypatch.setenv("ORDER_EXCEPTION_CUSTOMER_LANGUAGE_LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(customer_intent, "_invoke_json", fail_if_called)

    seed = build_webhook_case_seed(
        IntegrationProvider.SHOPIFY,
        event_id="evt_demo_tag",
        headers={"x-shopify-topic": "orders/create"},
        payload={
            "id": "gid://shopify/Order/1037",
            "name": "#1037",
            "tags": "flowlabs-demo, address-change-request",
        },
    )

    assert seed.exception_type == "address_change_request"
    assert seed.context["customer_request"]["type"] == "address_change_request"
