from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import import_module
from typing import Any, cast

from agents.order_exception.state import OrderExceptionState
from ecom_shared import ClassificationResult, ExceptionType

JsonObject = dict[str, Any]

EXCEPTION_TYPES = {
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
COMPLEX_ROUTES = {
    "delivered_not_received",
    "fraud_triage",
    "item_change_request",
}
LOCKED_PROPOSAL_FIELDS = {
    "type",
    "requires_human",
    "required_approvals",
    "tool_calls",
    "matched_fop_ids",
    "hard_constraints",
}


@dataclass(frozen=True)
class SupervisorRefinement:
    exception_type: ExceptionType
    confidence: float
    signals: list[str]
    source: str
    model: str | None = None


def refine_supervisor_route(
    *,
    order: JsonObject,
    context: JsonObject,
    deterministic: ClassificationResult,
) -> SupervisorRefinement:
    """Optionally ask Anthropic to refine the deterministic route.

    The shared classifier remains the fallback and eval baseline. The Anthropic
    layer is opt-in so tests, local demos, and CI stay deterministic.
    """

    if not _llm_enabled():
        return _deterministic_supervisor(deterministic)

    model = _supervisor_model()
    try:
        payload = _invoke_json(
            model=model,
            system=(
                "You are the Order Exception Agent supervisor. Classify the case into "
                "exactly one lane: address_change_request, damaged_in_transit, "
                "delivered_not_received, fraud_triage, inventory_conflict, "
                "item_change_request, order_cancellation_request, "
                "order_not_picked, stuck_in_transit, or wismo. Use the "
                "deterministic classification "
                "as the safety baseline and only override it when the evidence is clear. "
                "Respond with JSON only."
            ),
            body={
                "deterministic": {
                    "exception_type": deterministic.exception_type,
                    "confidence": deterministic.confidence,
                    "signals": deterministic.signals,
                },
                "order": order,
                "context": context,
                "required_schema": {
                    "exception_type": "one supported lane",
                    "confidence": "number from 0 to 1",
                    "signals": ["short evidence strings"],
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - model failures should degrade safely.
        return _deterministic_supervisor(
            deterministic,
            extra_signal=f"Anthropic supervisor unavailable: {type(exc).__name__}.",
        )

    exception_type = payload.get("exception_type")
    if not isinstance(exception_type, str) or exception_type not in EXCEPTION_TYPES:
        return _deterministic_supervisor(
            deterministic,
            extra_signal="Anthropic supervisor returned an unsupported lane.",
        )

    confidence = _clamped_confidence(payload.get("confidence"), deterministic.confidence)
    signals = _string_list(payload.get("signals")) or deterministic.signals
    return SupervisorRefinement(
        exception_type=cast(ExceptionType, exception_type),
        confidence=confidence,
        signals=signals,
        source="anthropic_supervisor",
        model=model,
    )


def refine_subagent_proposal(
    *,
    state: OrderExceptionState,
    proposed_action: JsonObject,
) -> JsonObject:
    """Optionally let Anthropic improve the narrative around a safe tool plan."""

    if not _llm_enabled():
        return proposed_action

    exception_type = str(proposed_action.get("type") or state.get("exception_type") or "")
    model = _subagent_model(exception_type)
    try:
        payload = _invoke_json(
            model=model,
            system=(
                "You are an ecommerce order exception subagent. Improve only the "
                "human-facing summary, recommendation, confidence, and rationale for "
                "the provided proposal. Do not add, remove, rename, or edit tool calls, "
                "approval gates, FOP ids, hard constraints, or exception type. Respond "
                "with JSON only."
            ),
            body={
                "case_id": state.get("case_id"),
                "merchant_id": state.get("merchant_id"),
                "exception_type": exception_type,
                "order": state.get("order", {}),
                "context": state.get("context", {}),
                "matched_fops": state.get("active_fops", []),
                "proposal": proposed_action,
                "locked_fields": sorted(LOCKED_PROPOSAL_FIELDS),
                "required_schema": {
                    "summary": "string",
                    "recommendation": "string",
                    "confidence": "number from 0 to 1",
                    "rationale": ["short evidence strings"],
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - model failures should degrade safely.
        refined = dict(proposed_action)
        refined["llm_refinement"] = {
            "source": "deterministic_fallback",
            "error": type(exc).__name__,
        }
        return refined

    refined = dict(proposed_action)
    for key in ("summary", "recommendation"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            refined[key] = value.strip()
    refined["confidence"] = _clamped_confidence(
        payload.get("confidence"),
        _number(proposed_action.get("confidence")) or 0.0,
    )
    rationale = _string_list(payload.get("rationale"))
    if rationale:
        refined["rationale"] = rationale
    refined["llm_refinement"] = {
        "source": "anthropic_subagent",
        "model": model,
        "locked_fields": sorted(LOCKED_PROPOSAL_FIELDS),
    }
    for key in LOCKED_PROPOSAL_FIELDS:
        refined[key] = proposed_action.get(key)
    return refined


def _deterministic_supervisor(
    deterministic: ClassificationResult,
    *,
    extra_signal: str | None = None,
) -> SupervisorRefinement:
    signals = [*deterministic.signals]
    if extra_signal is not None:
        signals.append(extra_signal)
    return SupervisorRefinement(
        exception_type=deterministic.exception_type,
        confidence=deterministic.confidence,
        signals=signals,
        source="order_exception_classifier_v1",
    )


def _llm_enabled() -> bool:
    enabled = os.environ.get("ORDER_EXCEPTION_LLM_ENABLED", "").lower() == "true"
    return enabled and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _supervisor_model() -> str:
    return os.environ.get("ORDER_EXCEPTION_SUPERVISOR_MODEL", "claude-opus-4-7")


def _subagent_model(exception_type: str) -> str:
    env_name = (
        "ORDER_EXCEPTION_COMPLEX_MODEL"
        if exception_type in COMPLEX_ROUTES
        else "ORDER_EXCEPTION_FAST_MODEL"
    )
    fallback = "claude-opus-4-7" if exception_type in COMPLEX_ROUTES else "claude-sonnet-4-6"
    return os.environ.get(env_name, fallback)


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
        raise ValueError("Anthropic response was not a JSON object.")
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


def _clamped_confidence(value: object, fallback: float) -> float:
    numeric = _number(value)
    if numeric is None:
        return fallback
    return min(1.0, max(0.0, numeric))


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


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
