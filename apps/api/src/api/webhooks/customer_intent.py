from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Literal, cast

from ecom_shared import (
    ClassificationResult,
    ExceptionType,
    classify_order_exception,
)

JsonObject = dict[str, Any]

CustomerLanguageIntent = Literal[
    "address_change_request",
    "order_cancellation_request",
    "item_change_request",
    "wismo",
    "damaged_in_transit",
    "delivered_not_received",
    "other",
]

CUSTOMER_LANGUAGE_INTENTS = {
    "address_change_request",
    "order_cancellation_request",
    "item_change_request",
    "wismo",
    "damaged_in_transit",
    "delivered_not_received",
    "other",
}
WORKFLOW_EXTRACTION_INTENTS = CUSTOMER_LANGUAGE_INTENTS - {"other"}
MIN_CLASSIFICATION_CONFIDENCE = 0.65
MIN_SLOT_CONFIDENCE = 0.70


@dataclass(frozen=True)
class CustomerTextBundle:
    text: str
    sources: list[JsonObject]


def apply_customer_language_understanding(
    *,
    order: JsonObject,
    context: JsonObject,
) -> ClassificationResult | None:
    """Classify customer free text and extract workflow slots into context.

    Returns the trusted classification when customer text is present. Operational
    triggers without customer prose return None and continue through the shared
    deterministic classifier.
    """

    bundle = _customer_text_bundle(context)
    if not bundle.text:
        return None

    context["customer_text"] = {
        "source": "customer_message",
        "text": bundle.text,
        "sources": bundle.sources,
    }
    understanding = context.setdefault("language_understanding", {})
    if not isinstance(understanding, dict):
        understanding = {}
        context["language_understanding"] = understanding

    fallback = classify_order_exception(order, context)
    try:
        intent_payload = _classify_customer_text(order=order, context=context, text=bundle.text)
    except Exception as exc:  # noqa: BLE001 - ingestion must degrade safely.
        fallback = _deterministic_text_fallback(order=order, context=context, text=bundle.text)
        _merge_fallback_customer_request(context=context, classification=fallback)
        _mark_language_failure(
            context=context,
            phase="classification",
            error=type(exc).__name__,
            fallback=fallback,
        )
        return fallback

    intent = _validated_intent(intent_payload.get("intent"))
    confidence = _clamped_confidence(intent_payload.get("confidence"), 0.0)
    if intent is None or confidence < MIN_CLASSIFICATION_CONFIDENCE:
        fallback = _deterministic_text_fallback(order=order, context=context, text=bundle.text)
        _merge_fallback_customer_request(context=context, classification=fallback)
        _mark_low_confidence_classification(
            context=context,
            payload=intent_payload,
            fallback=fallback,
        )
        return fallback

    if intent == "other":
        fallback = _deterministic_text_fallback(order=order, context=context, text=bundle.text)
        _merge_customer_request(
            context=context,
            extracted={
                "type": "other",
                "order_reference": _string_or_none(intent_payload.get("order_reference")),
                "confidence": confidence,
                "evidence": _string_list(intent_payload.get("evidence")),
                "needs_human_triage": bool(intent_payload.get("needs_human_triage", True)),
            },
            source="llm_intent_classifier",
        )
        return fallback

    classification = ClassificationResult(
        exception_type=cast(ExceptionType, intent),
        confidence=confidence,
        signals=_string_list(intent_payload.get("evidence"))
        or [f"Customer free text classified as {intent}."],
    )
    understanding["intent"] = {
        "source": "llm_intent_classifier",
        "intent": intent,
        "confidence": confidence,
        "order_reference": _string_or_none(intent_payload.get("order_reference")),
        "evidence": _string_list(intent_payload.get("evidence")),
        "needs_human_triage": bool(intent_payload.get("needs_human_triage", False)),
    }
    _merge_customer_request(
        context=context,
        extracted={
            "type": intent,
            "order_reference": _string_or_none(intent_payload.get("order_reference")),
            "confidence": confidence,
            "evidence": _string_list(intent_payload.get("evidence")),
            "needs_human_triage": bool(intent_payload.get("needs_human_triage", False)),
        },
        source="llm_intent_classifier",
    )

    try:
        slot_payload = _extract_workflow_slots(
            intent=intent,
            order=order,
            context=context,
            text=bundle.text,
        )
    except Exception as exc:  # noqa: BLE001 - extraction should not block case creation.
        language = _language_understanding(context)
        language["slot_extraction"] = {
            "source": "llm_workflow_slot_extractor",
            "workflow": intent,
            "error": type(exc).__name__,
        }
        return classification

    language = _language_understanding(context)
    language["slot_extraction"] = {
        "source": "llm_workflow_slot_extractor",
        "workflow": intent,
        "confidence": _clamped_confidence(slot_payload.get("confidence"), 0.0),
        "evidence": _string_list(slot_payload.get("evidence")),
        "is_complete": bool(slot_payload.get("is_complete")),
        "needs_clarification": bool(slot_payload.get("needs_clarification")),
    }
    _merge_slots_for_workflow(context=context, intent=intent, slot_payload=slot_payload)
    return classification


async def apply_customer_language_understanding_async(
    *,
    order: JsonObject,
    context: JsonObject,
) -> ClassificationResult | None:
    """Async webhook-safe variant of customer language understanding."""

    bundle = _customer_text_bundle(context)
    if not bundle.text:
        return None

    context["customer_text"] = {
        "source": "customer_message",
        "text": bundle.text,
        "sources": bundle.sources,
    }
    understanding = context.setdefault("language_understanding", {})
    if not isinstance(understanding, dict):
        understanding = {}
        context["language_understanding"] = understanding

    try:
        intent_payload = await _classify_customer_text_async(
            order=order,
            context=context,
            text=bundle.text,
        )
    except Exception as exc:  # noqa: BLE001 - ingestion must degrade safely.
        fallback = _deterministic_text_fallback(order=order, context=context, text=bundle.text)
        _merge_fallback_customer_request(context=context, classification=fallback)
        _mark_language_failure(
            context=context,
            phase="classification",
            error=type(exc).__name__,
            fallback=fallback,
        )
        return fallback

    intent = _validated_intent(intent_payload.get("intent"))
    confidence = _clamped_confidence(intent_payload.get("confidence"), 0.0)
    if intent is None or confidence < MIN_CLASSIFICATION_CONFIDENCE:
        fallback = _deterministic_text_fallback(order=order, context=context, text=bundle.text)
        _merge_fallback_customer_request(context=context, classification=fallback)
        _mark_low_confidence_classification(
            context=context,
            payload=intent_payload,
            fallback=fallback,
        )
        return fallback

    if intent == "other":
        fallback = _deterministic_text_fallback(order=order, context=context, text=bundle.text)
        _merge_customer_request(
            context=context,
            extracted={
                "type": "other",
                "order_reference": _string_or_none(intent_payload.get("order_reference")),
                "confidence": confidence,
                "evidence": _string_list(intent_payload.get("evidence")),
                "needs_human_triage": bool(intent_payload.get("needs_human_triage", True)),
            },
            source="llm_intent_classifier",
        )
        return fallback

    classification = ClassificationResult(
        exception_type=cast(ExceptionType, intent),
        confidence=confidence,
        signals=_string_list(intent_payload.get("evidence"))
        or [f"Customer free text classified as {intent}."],
    )
    understanding["intent"] = {
        "source": "llm_intent_classifier",
        "intent": intent,
        "confidence": confidence,
        "order_reference": _string_or_none(intent_payload.get("order_reference")),
        "evidence": _string_list(intent_payload.get("evidence")),
        "needs_human_triage": bool(intent_payload.get("needs_human_triage", False)),
    }
    _merge_customer_request(
        context=context,
        extracted={
            "type": intent,
            "order_reference": _string_or_none(intent_payload.get("order_reference")),
            "confidence": confidence,
            "evidence": _string_list(intent_payload.get("evidence")),
            "needs_human_triage": bool(intent_payload.get("needs_human_triage", False)),
        },
        source="llm_intent_classifier",
    )

    try:
        slot_payload = await _extract_workflow_slots_async(
            intent=intent,
            order=order,
            context=context,
            text=bundle.text,
        )
    except Exception as exc:  # noqa: BLE001 - extraction should not block case creation.
        language = _language_understanding(context)
        language["slot_extraction"] = {
            "source": "llm_workflow_slot_extractor",
            "workflow": intent,
            "error": type(exc).__name__,
        }
        return classification

    language = _language_understanding(context)
    language["slot_extraction"] = {
        "source": "llm_workflow_slot_extractor",
        "workflow": intent,
        "confidence": _clamped_confidence(slot_payload.get("confidence"), 0.0),
        "evidence": _string_list(slot_payload.get("evidence")),
        "is_complete": bool(slot_payload.get("is_complete")),
        "needs_clarification": bool(slot_payload.get("needs_clarification")),
    }
    _merge_slots_for_workflow(context=context, intent=intent, slot_payload=slot_payload)
    return classification


def _classify_customer_text(*, order: JsonObject, context: JsonObject, text: str) -> JsonObject:
    if not _llm_enabled():
        raise RuntimeError("Customer language LLM is not enabled.")
    return _invoke_json(
        model=_language_model(),
        system=(
            "You classify ecommerce customer support messages. Return JSON only. "
            "Choose exactly one intent from: address_change_request, "
            "order_cancellation_request, item_change_request, wismo, "
            "damaged_in_transit, delivered_not_received, other. Include concise "
            "evidence and do not infer slots beyond the message."
        ),
        body={
            "message": text,
            "order": order,
            "context_summary": _context_summary(context),
            "required_schema": {
                "intent": "one allowed intent",
                "confidence": "number from 0 to 1",
                "order_reference": "string or null",
                "evidence": ["short quoted or paraphrased evidence"],
                "needs_human_triage": "boolean",
            },
        },
    )


async def _classify_customer_text_async(
    *,
    order: JsonObject,
    context: JsonObject,
    text: str,
) -> JsonObject:
    if not _llm_enabled():
        raise RuntimeError("Customer language LLM is not enabled.")
    return await _invoke_json_async(
        model=_language_model(),
        system=(
            "You classify ecommerce customer support messages. Return JSON only. "
            "Choose exactly one intent from: address_change_request, "
            "order_cancellation_request, item_change_request, wismo, "
            "damaged_in_transit, delivered_not_received, other. Include concise "
            "evidence and do not infer slots beyond the message."
        ),
        body={
            "message": text,
            "order": order,
            "context_summary": _context_summary(context),
            "required_schema": {
                "intent": "one allowed intent",
                "confidence": "number from 0 to 1",
                "order_reference": "string or null",
                "evidence": ["short quoted or paraphrased evidence"],
                "needs_human_triage": "boolean",
            },
        },
    )


def _extract_workflow_slots(
    *,
    intent: CustomerLanguageIntent,
    order: JsonObject,
    context: JsonObject,
    text: str,
) -> JsonObject:
    if intent not in WORKFLOW_EXTRACTION_INTENTS:
        return {}
    if not _llm_enabled():
        raise RuntimeError("Customer language LLM is not enabled.")
    return _invoke_json(
        model=_language_model(),
        system=(
            f"You extract only the fields needed for the {intent} ecommerce workflow. "
            "Return JSON only. Do not execute tools or decide approvals."
        ),
        body={
            "workflow": intent,
            "message": text,
            "order": order,
            "context_summary": _context_summary(context),
            "required_schema": _workflow_schema(intent),
        },
    )


async def _extract_workflow_slots_async(
    *,
    intent: CustomerLanguageIntent,
    order: JsonObject,
    context: JsonObject,
    text: str,
) -> JsonObject:
    if intent not in WORKFLOW_EXTRACTION_INTENTS:
        return {}
    if not _llm_enabled():
        raise RuntimeError("Customer language LLM is not enabled.")
    return await _invoke_json_async(
        model=_language_model(),
        system=(
            f"You extract only the fields needed for the {intent} ecommerce workflow. "
            "Return JSON only. Do not execute tools or decide approvals."
        ),
        body={
            "workflow": intent,
            "message": text,
            "order": order,
            "context_summary": _context_summary(context),
            "required_schema": _workflow_schema(intent),
        },
    )


def _merge_slots_for_workflow(
    *,
    context: JsonObject,
    intent: CustomerLanguageIntent,
    slot_payload: JsonObject,
) -> None:
    confidence = _clamped_confidence(slot_payload.get("confidence"), 0.0)
    evidence = _string_list(slot_payload.get("evidence"))
    common: JsonObject = {
        "order_reference": _string_or_none(slot_payload.get("order_reference")),
        "is_complete": bool(slot_payload.get("is_complete")),
        "needs_clarification": bool(slot_payload.get("needs_clarification")),
        "confidence": confidence,
        "evidence": evidence,
    }
    if intent == "address_change_request":
        requested_address = _validated_address(slot_payload.get("requested_address"))
        extracted: JsonObject = {**common, "type": intent}
        if requested_address and confidence >= MIN_SLOT_CONFIDENCE and common["is_complete"]:
            extracted["requested_address"] = requested_address
            context.setdefault("address_change", {})
            if isinstance(context["address_change"], dict):
                _merge_nested_field(
                    target=cast(JsonObject, context["address_change"]),
                    key="requested_address",
                    value=requested_address,
                    confidence=confidence,
                    context=context,
                    source="llm_address_change_extractor",
                )
        elif requested_address:
            request = context.get("customer_request")
            existing_address = request.get("requested_address") if isinstance(request, dict) else None
            if isinstance(existing_address, dict) and existing_address != requested_address:
                _record_conflict(
                    context=context,
                    field="customer_request.requested_address",
                    existing=existing_address,
                    extracted=requested_address,
                    source="llm_address_change_extractor",
                    confidence=confidence,
                    resolution="low_confidence_extracted_value_ignored",
                )
        _merge_customer_request(
            context=context,
            extracted=extracted,
            source="llm_address_change_extractor",
        )
        return

    if intent == "wismo":
        _merge_customer_request(
            context=context,
            extracted={
                **common,
                "type": intent,
                "tracking_status_ask": bool(slot_payload.get("tracking_status_ask", True)),
                "delivery_promise": _string_or_none(slot_payload.get("delivery_promise")),
            },
            source="llm_wismo_extractor",
        )
        return

    _merge_customer_request(context=context, extracted={**common, "type": intent}, source="llm_slot_extractor")


def _merge_customer_request(*, context: JsonObject, extracted: JsonObject, source: str) -> None:
    request = context.setdefault("customer_request", {})
    if not isinstance(request, dict):
        request = {}
        context["customer_request"] = request
    request.setdefault("extraction_sources", [])
    if isinstance(request.get("extraction_sources"), list):
        request["extraction_sources"].append(source)
    confidence = _clamped_confidence(extracted.get("confidence"), 0.0)
    for key, value in extracted.items():
        if value is None:
            continue
        if key == "confidence":
            request[key] = max(
                _clamped_confidence(request.get(key), 0.0),
                _clamped_confidence(value, 0.0),
            )
            continue
        if key == "evidence":
            existing_evidence = request.setdefault("evidence", [])
            if isinstance(existing_evidence, list) and isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item not in existing_evidence:
                        existing_evidence.append(item)
            continue
        if key == "requested_address":
            _merge_nested_field(
                target=request,
                key=key,
                value=cast(JsonObject, value),
                confidence=confidence,
                context=context,
                source=source,
            )
            continue
        if key in request and request[key] not in (None, "", [], {}):
            if request[key] != value and key in {"type", "order_reference"}:
                _record_conflict(
                    context=context,
                    field=f"customer_request.{key}",
                    existing=request[key],
                    extracted=value,
                    source=source,
                    confidence=confidence,
                )
            continue
        request[key] = value


def _merge_nested_field(
    *,
    target: JsonObject,
    key: str,
    value: JsonObject,
    confidence: float,
    context: JsonObject,
    source: str,
) -> None:
    existing = target.get(key)
    if not isinstance(existing, dict) or not existing:
        target[key] = value
        target[f"{key}_confidence"] = confidence
        return
    existing_confidence = _clamped_confidence(
        target.get(f"{key}_confidence"),
        _clamped_confidence(target.get("confidence"), 1.0),
    )
    if existing == value:
        return
    if confidence > existing_confidence:
        _record_conflict(
            context=context,
            field=f"customer_request.{key}",
            existing=existing,
            extracted=value,
            source=source,
            confidence=confidence,
            resolution="extracted_value_used",
        )
        target[key] = value
        target[f"{key}_confidence"] = confidence
        return
    _record_conflict(
        context=context,
        field=f"customer_request.{key}",
        existing=existing,
        extracted=value,
        source=source,
        confidence=confidence,
        resolution="existing_value_preserved",
    )


def _record_conflict(
    *,
    context: JsonObject,
    field: str,
    existing: object,
    extracted: object,
    source: str,
    confidence: float,
    resolution: str = "existing_value_preserved",
) -> None:
    request = context.setdefault("customer_request", {})
    if not isinstance(request, dict):
        return
    conflicts = request.setdefault("extraction_conflicts", [])
    if isinstance(conflicts, list):
        conflicts.append(
            {
                "field": field,
                "existing": existing,
                "extracted": extracted,
                "source": source,
                "confidence": confidence,
                "resolution": resolution,
            }
        )


def _merge_fallback_customer_request(
    *,
    context: JsonObject,
    classification: ClassificationResult,
) -> None:
    _merge_customer_request(
        context=context,
        extracted={
            "type": classification.exception_type,
            "confidence": classification.confidence,
            "evidence": classification.signals,
            "needs_human_triage": classification.confidence < 0.75,
        },
        source="deterministic_fallback",
    )


def _mark_language_failure(
    *,
    context: JsonObject,
    phase: str,
    error: str,
    fallback: ClassificationResult,
) -> None:
    language = _language_understanding(context)
    language[phase] = {
        "source": "deterministic_fallback",
        "error": error,
        "fallback_exception_type": fallback.exception_type,
        "fallback_confidence": fallback.confidence,
        "needs_human_triage": fallback.confidence < 0.75,
    }
    request = context.setdefault("customer_request", {})
    if isinstance(request, dict) and fallback.confidence < 0.75:
        request["needs_human_triage"] = True


def _mark_low_confidence_classification(
    *,
    context: JsonObject,
    payload: JsonObject,
    fallback: ClassificationResult,
) -> None:
    language = _language_understanding(context)
    language["intent"] = {
        "source": "deterministic_fallback",
        "error": "low_confidence_or_invalid_intent",
        "llm_payload": payload,
        "fallback_exception_type": fallback.exception_type,
        "fallback_confidence": fallback.confidence,
        "needs_human_triage": fallback.confidence < 0.75,
    }
    request = context.setdefault("customer_request", {})
    if isinstance(request, dict) and fallback.confidence < 0.75:
        request["needs_human_triage"] = True


def _deterministic_text_fallback(
    *,
    order: JsonObject,
    context: JsonObject,
    text: str,
) -> ClassificationResult:
    lowered = text.lower()
    if any(phrase in lowered for phrase in ("change the shipping address", "update my address", "wrong address")):
        return ClassificationResult(
            exception_type="address_change_request",
            confidence=0.78,
            signals=["Deterministic fallback matched an address-change phrase in customer text."],
        )
    if any(phrase in lowered for phrase in ("where is my order", "tracking", "shipping status", "wismo")):
        return ClassificationResult(
            exception_type="wismo",
            confidence=0.76,
            signals=["Deterministic fallback matched a tracking/status phrase in customer text."],
        )
    if "cancel" in lowered:
        return ClassificationResult(
            exception_type="order_cancellation_request",
            confidence=0.76,
            signals=["Deterministic fallback matched a cancellation phrase in customer text."],
        )
    return classify_order_exception(order, context)


def _customer_text_bundle(context: JsonObject) -> CustomerTextBundle:
    sources: list[JsonObject] = []
    for path in (
        ("ticket", "subject"),
        ("ticket", "excerpt"),
        ("ticket", "body_text"),
        ("ticket", "body"),
        ("webhook", "payload", "ticket", "subject"),
        ("webhook", "payload", "ticket", "excerpt"),
        ("webhook", "payload", "ticket", "body_text"),
        ("webhook", "payload", "ticket", "body"),
        ("webhook", "payload", "message"),
        ("webhook", "payload", "excerpt"),
    ):
        value = _nested(context, list(path))
        if isinstance(value, str) and value.strip():
            sources.append({"path": ".".join(path), "text": _clean_text(value)})

    for path in (("ticket", "messages"), ("webhook", "payload", "messages"), ("webhook", "payload", "ticket", "messages")):
        value = _nested(context, list(path))
        if isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                body = item.get("body_text") or item.get("body") or item.get("text")
                if isinstance(body, str) and body.strip():
                    sources.append({"path": f"{'.'.join(path)}.{index}", "text": _clean_text(body)})

    seen: set[str] = set()
    deduped: list[JsonObject] = []
    for source in sources:
        text = str(source["text"])
        if text in seen:
            continue
        seen.add(text)
        deduped.append(source)
    return CustomerTextBundle(
        text="\n\n".join(str(source["text"]) for source in deduped),
        sources=deduped,
    )


def _validated_intent(value: object) -> CustomerLanguageIntent | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    if normalized in CUSTOMER_LANGUAGE_INTENTS:
        return cast(CustomerLanguageIntent, normalized)
    return None


def _validated_address(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    address = {
        "address1": _string_or_none(value.get("address1")),
        "address2": _string_or_none(value.get("address2")),
        "city": _string_or_none(value.get("city")),
        "province": _string_or_none(value.get("province")),
        "zip": _string_or_none(value.get("zip")),
        "country": _string_or_none(value.get("country")) or "US",
    }
    required = ("address1", "city", "province", "zip", "country")
    if not all(address.get(key) for key in required):
        return {}
    if address["country"] in {"USA", "United States", "United States of America"}:
        address["country"] = "US"
    return address


def _workflow_schema(intent: CustomerLanguageIntent) -> JsonObject:
    if intent == "address_change_request":
        return {
            "requested_address": {
                "address1": "string or null",
                "address2": "string or null",
                "city": "string or null",
                "province": "state/province code or name, or null",
                "zip": "string or null",
                "country": "ISO country code or null",
            },
            "order_reference": "string or null",
            "is_complete": "boolean",
            "needs_clarification": "boolean",
            "confidence": "number from 0 to 1",
            "evidence": ["short evidence"],
        }
    if intent == "wismo":
        return {
            "order_reference": "string or null",
            "tracking_status_ask": "boolean",
            "delivery_promise": "date/string clue or null",
            "is_complete": "boolean",
            "needs_clarification": "boolean",
            "confidence": "number from 0 to 1",
            "evidence": ["short evidence"],
        }
    return {
        "order_reference": "string or null",
        "is_complete": "boolean",
        "needs_clarification": "boolean",
        "confidence": "number from 0 to 1",
        "evidence": ["short evidence"],
    }


def _context_summary(context: JsonObject) -> JsonObject:
    summary: JsonObject = {}
    for key in ("ticket", "customer", "shipment", "delivery", "payment", "inventory", "fulfillment"):
        value = context.get(key)
        if isinstance(value, dict):
            summary[key] = {k: v for k, v in value.items() if k not in {"body", "body_text", "messages"}}
    return summary


def _language_understanding(context: JsonObject) -> JsonObject:
    value = context.setdefault("language_understanding", {})
    if isinstance(value, dict):
        return value
    context["language_understanding"] = {}
    return cast(JsonObject, context["language_understanding"])


def _llm_enabled() -> bool:
    enabled = os.environ.get("ORDER_EXCEPTION_CUSTOMER_LANGUAGE_LLM_ENABLED", "").lower() == "true"
    return enabled and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _language_model() -> str:
    return os.environ.get(
        "ORDER_EXCEPTION_CUSTOMER_LANGUAGE_MODEL",
        os.environ.get("ORDER_EXCEPTION_FAST_MODEL", "claude-sonnet-4-6"),
    )


def _invoke_json(*, model: str, system: str, body: JsonObject) -> JsonObject:
    chat_anthropic = import_module("langchain_anthropic")
    chat_model_factory = cast(Any, chat_anthropic).ChatAnthropic
    chat_model = chat_model_factory(model=model, temperature=0)
    response = chat_model.invoke(
        [
            ("system", system),
            ("human", json.dumps(body, sort_keys=True, separators=(",", ":"))),
        ]
    )
    payload = json.loads(_strip_json_fence(_message_text(getattr(response, "content", ""))))
    if not isinstance(payload, dict):
        raise ValueError("LLM response was not a JSON object.")
    return payload


async def _invoke_json_async(*, model: str, system: str, body: JsonObject) -> JsonObject:
    chat_anthropic = import_module("langchain_anthropic")
    chat_model_factory = cast(Any, chat_anthropic).ChatAnthropic
    chat_model = chat_model_factory(model=model, temperature=0)
    response = await chat_model.ainvoke(
        [
            ("system", system),
            ("human", json.dumps(body, sort_keys=True, separators=(",", ":"))),
        ]
    )
    payload = json.loads(_strip_json_fence(_message_text(getattr(response, "content", ""))))
    if not isinstance(payload, dict):
        raise ValueError("LLM response was not a JSON object.")
    return payload


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
            elif isinstance(item, str):
                pieces.append(item)
        return "\n".join(pieces)
    return str(content)


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).removeprefix("json").strip()
    return text


def _clean_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", without_tags).strip()


def _nested(value: object, path: list[str], default: object = None) -> object:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return default
        current = current.get(part)
    return default if current is None else current


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if value is None:
        return None
    return str(value)


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _clamped_confidence(value: object, fallback: float) -> float:
    numeric = _number(value)
    if numeric is None:
        return fallback
    return min(1.0, max(0.0, numeric))


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
