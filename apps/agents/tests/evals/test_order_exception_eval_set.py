from __future__ import annotations

from agents.order_exception.evals import (
    ScenarioResult,
    build_judge_payload,
    judge_scenario,
    load_scenarios,
    run_eval_suite,
    run_judged_eval_suite,
)


def test_order_exception_eval_dataset_has_tier1_coverage() -> None:
    scenarios = load_scenarios()
    tags = {tag for scenario in scenarios for tag in scenario.get("tags", [])}

    assert len(scenarios) >= 10
    assert {
        "fraud",
        "address_change",
        "item_change",
        "cancellation",
        "inventory",
        "fulfillment",
        "shipment",
        "delivery",
    }.issubset(tags)
    assert sum(1 for scenario in scenarios if "top_10" in scenario.get("tags", [])) >= 5


def test_order_exception_eval_dataset_passes_deterministic_gate() -> None:
    result = run_eval_suite()

    assert result.pass_rate >= 0.85, _format_failures(result.results)
    assert result.failed == 0, _format_failures(result.results)


def test_order_exception_eval_judge_fallback_scores_dataset() -> None:
    result = run_judged_eval_suite()

    assert result.average_score == 5
    assert result.pass_rate == 1
    assert all(not item.low_confidence for item in result.results)


def test_order_exception_eval_judge_flags_low_confidence_failure() -> None:
    scenario = {
        "id": "broken_expected_approval",
        "input": {
            "merchant_id": "demo-merchant",
            "case_id": "eval_fraud_low_broken",
            "exception_type": "fraud_triage",
            "order": {"id": "gid://shopify/Order/broken"},
            "context": {"risk": {"score": 10}},
        },
        "expected": {
            "exception_type": "fraud_triage",
            "requires_human": True,
            "matched_fop_ids": [],
            "tool_calls": ["shopify_create_refund"],
        },
    }
    result = judge_scenario(scenario, graph=_SingleScenarioGraph())

    assert result.passed is False
    assert result.low_confidence is True
    assert result.unsafe_actions


def test_order_exception_judge_payload_includes_fops_and_proposal() -> None:
    scenario = load_scenarios()[0]
    graph_result = _SingleScenarioGraph().invoke(scenario["input"])
    payload = build_judge_payload(scenario, graph_result)

    assert payload["scenario_id"] == scenario["id"]
    actual = payload["actual"]
    assert isinstance(actual, dict)
    assert actual["active_fops"]
    assert actual["proposed_action"]


def _format_failures(results: list[ScenarioResult]) -> str:
    lines: list[str] = []
    for result in results:
        if not result.passed:
            lines.append(f"{result.scenario_id}: {'; '.join(result.failures)}")
    return "\n".join(lines)


class _SingleScenarioGraph:
    def invoke(self, graph_input: object) -> dict[str, object]:
        from agents.order_exception.graph import graph

        return graph.invoke(graph_input)
