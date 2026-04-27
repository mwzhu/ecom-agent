from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class FopEvaluation:
    matched_fops: list[JsonObject]
    system_prompt_block: str
    hard_constraints: list[str]
    required_approvals: list[str]
    auto_actions: list[str]


def evaluate_fops(
    *,
    fops: list[JsonObject],
    scope: str,
    order: JsonObject,
    context: JsonObject,
) -> FopEvaluation:
    matched = [
        fop
        for fop in fops
        if fop.get("scope") == scope and _matches_conditions(fop.get("conditions"), order, context)
    ]
    hard_constraints = _dedupe_strings(
        constraint
        for fop in matched
        for constraint in _as_string_list(fop.get("constraints"))
    )
    required_approvals = _dedupe_strings(
        approval
        for fop in matched
        for approval in _as_string_list(fop.get("required_approvals"))
    )
    auto_actions = _dedupe_strings(
        action for fop in matched for action in _as_string_list(fop.get("auto_actions"))
    )
    return FopEvaluation(
        matched_fops=matched,
        system_prompt_block=_render_prompt_block(matched, hard_constraints, required_approvals),
        hard_constraints=hard_constraints,
        required_approvals=required_approvals,
        auto_actions=auto_actions,
    )


def _matches_conditions(raw_conditions: object, order: JsonObject, context: JsonObject) -> bool:
    if raw_conditions is None:
        return True
    if not isinstance(raw_conditions, dict):
        return False

    all_conditions = raw_conditions.get("all")
    if isinstance(all_conditions, list) and not all(
        _matches_condition(condition, order, context) for condition in all_conditions
    ):
        return False

    any_conditions = raw_conditions.get("any")
    if isinstance(any_conditions, list) and any_conditions:
        return any(_matches_condition(condition, order, context) for condition in any_conditions)

    return True


def _matches_condition(condition: object, order: JsonObject, context: JsonObject) -> bool:
    if not isinstance(condition, dict):
        return False
    field = condition.get("field")
    operator = condition.get("operator")
    expected = condition.get("value")
    if not isinstance(field, str) or not isinstance(operator, str):
        return False

    actual = _field_value(field, order, context)
    if operator == "equals":
        return _normalize(actual) == _normalize(expected)
    if operator == "not_equals":
        return _normalize(actual) != _normalize(expected)
    if operator == "greater_than":
        return _compare_numbers(actual, expected, ">")
    if operator == "greater_than_or_equal":
        return _compare_numbers(actual, expected, ">=")
    if operator == "less_than":
        return _compare_numbers(actual, expected, "<")
    if operator == "less_than_or_equal":
        return _compare_numbers(actual, expected, "<=")
    if operator == "contains":
        return isinstance(actual, list) and expected in actual
    if operator == "in":
        return isinstance(expected, list) and actual in expected
    if operator == "not_in":
        return isinstance(expected, list) and actual not in expected
    if operator == "exists":
        return actual is not None
    return False


def _field_value(field: str, order: JsonObject, context: JsonObject) -> object:
    root_name, separator, path = field.partition(".")
    if not separator:
        return context.get(field, order.get(field))
    root = _root_value(root_name, order, context)
    return _nested_value(root, path.split("."))


def _root_value(root_name: str, order: JsonObject, context: JsonObject) -> object:
    if root_name == "order":
        return order
    if root_name == "customer":
        return context.get("customer", order.get("customer", {}))
    if root_name == "risk":
        return context.get("risk", order.get("risk", {}))
    if root_name == "inventory":
        return context.get("inventory", {})
    if root_name == "payment":
        return context.get("payment", {})
    if root_name == "address":
        address_change = context.get("address_change", {})
        if isinstance(address_change, dict) and address_change.get("requested_address"):
            return address_change.get("requested_address")
        customer_request = context.get("customer_request", {})
        if isinstance(customer_request, dict) and customer_request.get("requested_address"):
            return customer_request.get("requested_address")
        return order.get("shipping_address", {})
    return context.get(root_name, {})


def _nested_value(value: object, parts: list[str]) -> object:
    current = value
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _normalize(value: object) -> object:
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _compare_numbers(actual: object, expected: object, operator: str) -> bool:
    actual_number = _number(actual)
    expected_number = _number(expected)
    if actual_number is None or expected_number is None:
        return False
    if operator == ">":
        return actual_number > expected_number
    if operator == ">=":
        return actual_number >= expected_number
    if operator == "<":
        return actual_number < expected_number
    if operator == "<=":
        return actual_number <= expected_number
    return False


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


def _as_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _dedupe_strings(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if isinstance(value, str) and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _render_prompt_block(
    matched_fops: list[JsonObject],
    hard_constraints: list[str],
    required_approvals: list[str],
) -> str:
    if not matched_fops:
        return "No active FOPs matched this case."

    lines = ["Matched merchant FOPs:"]
    for fop in matched_fops:
        fop_id = fop.get("id", "unknown")
        nl_text = fop.get("nl_text", "")
        lines.append(f"- {fop_id}: {nl_text}")
    if hard_constraints:
        lines.append("Hard constraints:")
        lines.extend(f"- {constraint}" for constraint in hard_constraints)
    if required_approvals:
        lines.append("Required approvals:")
        lines.extend(f"- {approval}" for approval in required_approvals)
    return "\n".join(lines)
