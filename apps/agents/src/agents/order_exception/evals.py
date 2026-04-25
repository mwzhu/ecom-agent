from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, cast

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    passed: bool
    failures: list[str]


@dataclass(frozen=True)
class SuiteResult:
    total: int
    passed: int
    failed: int
    pass_rate: float
    results: list[ScenarioResult]


@dataclass(frozen=True)
class JudgeResult:
    scenario_id: str
    score: int
    passed: bool
    reasoning: str
    fop_violations: list[str]
    unsafe_actions: list[str]
    source: str

    @property
    def low_confidence(self) -> bool:
        return self.score < 4 or bool(self.fop_violations or self.unsafe_actions)


@dataclass(frozen=True)
class JudgedSuiteResult:
    total: int
    passed: int
    failed: int
    pass_rate: float
    average_score: float
    results: list[JudgeResult]


def default_dataset_path() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "packages" / "eval-datasets" / "order_exception_v0.json"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Could not find packages/eval-datasets/order_exception_v0.json")


def default_rubric_path() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent / "packages" / "eval-datasets" / "order_exception_judge_rubric.md"
        )
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Could not find packages/eval-datasets/order_exception_judge_rubric.md"
    )


def load_scenarios(path: Path | None = None) -> list[JsonObject]:
    dataset_path = path or default_dataset_path()
    raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Order exception eval dataset must be a JSON list.")
    return [cast(JsonObject, item) for item in raw if isinstance(item, dict)]


def run_eval_suite(
    scenarios: list[JsonObject] | None = None,
    graph: Any | None = None,
) -> SuiteResult:
    if graph is None:
        from agents.order_exception.graph import graph as compiled_graph

        graph = compiled_graph
    loaded = scenarios if scenarios is not None else load_scenarios()
    results = [evaluate_scenario(scenario, graph) for scenario in loaded]
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    return SuiteResult(
        total=total,
        passed=passed,
        failed=total - passed,
        pass_rate=passed / total if total else 0.0,
        results=results,
    )


def run_judged_eval_suite(
    scenarios: list[JsonObject] | None = None,
    graph: Any | None = None,
) -> JudgedSuiteResult:
    if graph is None:
        from agents.order_exception.graph import graph as compiled_graph

        graph = compiled_graph
    loaded = scenarios if scenarios is not None else load_scenarios()
    results = [judge_scenario(scenario, graph) for scenario in loaded]
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    average_score = (
        sum(result.score for result in results) / total if total else 0.0
    )
    return JudgedSuiteResult(
        total=total,
        passed=passed,
        failed=total - passed,
        pass_rate=passed / total if total else 0.0,
        average_score=average_score,
        results=results,
    )


def evaluate_scenario(scenario: JsonObject, graph: Any) -> ScenarioResult:
    graph_input = cast(JsonObject, scenario.get("input") or {})
    result = cast(JsonObject, graph.invoke(graph_input))
    return _evaluate_graph_result(scenario, result)


def judge_scenario(scenario: JsonObject, graph: Any) -> JudgeResult:
    """Score a scenario with the configured judge, falling back deterministically.

    The LLM judge is opt-in because CI and local demos need to run without external
    credentials. When enabled, the judge can only score the already-produced graph
    result; deterministic assertions still remain the hard regression gate.
    """

    scenario_id = str(scenario.get("id") or "unknown")
    graph_input = cast(JsonObject, scenario.get("input") or {})
    graph_result = cast(JsonObject, graph.invoke(graph_input))
    deterministic = _evaluate_graph_result(scenario, graph_result)
    if not _judge_enabled():
        return _deterministic_judge_result(deterministic)

    try:
        return _anthropic_judge_result(
            scenario=scenario,
            graph_result=graph_result,
            deterministic=deterministic,
        )
    except Exception as exc:  # noqa: BLE001 - eval gates should degrade to deterministic.
        fallback = _deterministic_judge_result(deterministic)
        return JudgeResult(
            scenario_id=scenario_id,
            score=fallback.score,
            passed=fallback.passed,
            reasoning=(
                f"{fallback.reasoning} LLM judge unavailable: {type(exc).__name__}."
            ),
            fop_violations=fallback.fop_violations,
            unsafe_actions=fallback.unsafe_actions,
            source="deterministic_fallback",
        )


def build_judge_payload(scenario: JsonObject, graph_result: JsonObject) -> JsonObject:
    proposed = cast(JsonObject, graph_result.get("proposed_action") or {})
    return {
        "scenario_id": scenario.get("id"),
        "title": scenario.get("title"),
        "input": scenario.get("input", {}),
        "expected": scenario.get("expected", {}),
        "actual": {
            "exception_type": graph_result.get("exception_type"),
            "classification": graph_result.get("classification"),
            "route": graph_result.get("route"),
            "active_fops": graph_result.get("active_fops", []),
            "required_approvals": graph_result.get("required_approvals", []),
            "hard_constraints": graph_result.get("hard_constraints", []),
            "proposed_action": proposed,
            "resolution": graph_result.get("resolution"),
            "has_interrupt": "__interrupt__" in graph_result,
        },
        "rubric_summary": (
            "Score 0-5. Prioritize correct route, FOP compliance, expected tool plan, "
            "approval gates for writes/money movement, idempotency keys, and a usable "
            "merchant rationale."
        ),
    }


def _evaluate_graph_result(scenario: JsonObject, result: JsonObject) -> ScenarioResult:
    scenario_id = str(scenario.get("id") or "unknown")
    expected = cast(JsonObject, scenario.get("expected") or {})
    proposed = cast(JsonObject, result.get("proposed_action") or {})
    failures: list[str] = []

    _expect_equal(
        failures,
        "route",
        result.get("route"),
        expected.get("exception_type"),
    )
    _expect_equal(
        failures,
        "classification.exception_type",
        _nested(result, ["classification", "exception_type"]),
        expected.get("exception_type"),
    )
    _expect_equal(
        failures,
        "requires_human",
        proposed.get("requires_human"),
        expected.get("requires_human"),
    )
    _expect_equal(
        failures,
        "matched_fop_ids",
        proposed.get("matched_fop_ids"),
        expected.get("matched_fop_ids"),
    )
    _expect_equal(
        failures,
        "tool_calls",
        [call.get("tool") for call in _tool_calls(proposed)],
        expected.get("tool_calls"),
    )
    expected_status = expected.get("resolution_status")
    if expected_status is not None:
        _expect_equal(
            failures,
            "resolution.status",
            _nested(result, ["resolution", "status"]),
            expected_status,
        )
    expected_interrupt = expected.get("requires_human") is True
    has_interrupt = "__interrupt__" in result
    if has_interrupt != expected_interrupt:
        failures.append(f"interrupt expected {expected_interrupt!r}, got {has_interrupt!r}")

    return ScenarioResult(
        scenario_id=scenario_id,
        passed=not failures,
        failures=failures,
    )


def _deterministic_judge_result(result: ScenarioResult) -> JudgeResult:
    if result.passed:
        return JudgeResult(
            scenario_id=result.scenario_id,
            score=5,
            passed=True,
            reasoning="Deterministic assertions passed for route, FOPs, approvals, and tool plan.",
            fop_violations=[],
            unsafe_actions=[],
            source="deterministic_assertions",
        )
    # Partitioning matches the field labels passed to `_expect_equal` in
    # `_evaluate_graph_result`; keep them in sync if you rename those fields.
    fop_violations = [
        failure for failure in result.failures if "fop" in failure.lower()
    ]
    unsafe_actions = [
        failure
        for failure in result.failures
        if "requires_human" in failure or "interrupt" in failure or "tool_calls" in failure
    ]
    score = 1 if fop_violations or unsafe_actions else 3
    return JudgeResult(
        scenario_id=result.scenario_id,
        score=score,
        passed=False,
        reasoning="; ".join(result.failures),
        fop_violations=fop_violations,
        unsafe_actions=unsafe_actions,
        source="deterministic_assertions",
    )


def _anthropic_judge_result(
    *,
    scenario: JsonObject,
    graph_result: JsonObject,
    deterministic: ScenarioResult,
) -> JudgeResult:
    chat_anthropic = import_module("langchain_anthropic")
    chat_model_factory = cast(Any, chat_anthropic).ChatAnthropic
    chat_model = chat_model_factory(model=_judge_model(), temperature=0)
    response = chat_model.invoke(
        [
            ("system", _judge_system_prompt()),
            (
                "human",
                json.dumps(
                    {
                        "deterministic_assertions": {
                            "passed": deterministic.passed,
                            "failures": deterministic.failures,
                        },
                        "case": build_judge_payload(scenario, graph_result),
                        "required_response_schema": {
                            "score": "integer 0-5",
                            "passed": "boolean",
                            "reasoning": "short string",
                            "fop_violations": ["strings"],
                            "unsafe_actions": ["strings"],
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        ]
    )
    payload = json.loads(_strip_json_fence(_message_text(getattr(response, "content", ""))))
    if not isinstance(payload, dict):
        raise ValueError("Judge response was not a JSON object.")
    score = _clamped_score(payload.get("score"))
    passed = bool(payload.get("passed")) and score >= 4 and deterministic.passed
    return JudgeResult(
        scenario_id=str(scenario.get("id") or "unknown"),
        score=score,
        passed=passed,
        reasoning=str(payload.get("reasoning") or ""),
        fop_violations=_string_list(payload.get("fop_violations")),
        unsafe_actions=_string_list(payload.get("unsafe_actions")),
        source=f"anthropic:{_judge_model()}",
    )


def _judge_system_prompt() -> str:
    rubric = default_rubric_path().read_text(encoding="utf-8")
    return (
        "You are evaluating an ecommerce operations agent for order lifecycle "
        "exceptions. Score each run from 0 to 5 following the rubric below. "
        "Return JSON only.\n\n"
        f"{rubric}"
    )


def _judge_enabled() -> bool:
    enabled = os.environ.get("EVAL_JUDGE_ENABLED", "").lower() == "true"
    return enabled and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _judge_model() -> str:
    return os.environ.get("ORDER_EXCEPTION_JUDGE_MODEL", "claude-opus-4-7")


def _tool_calls(proposed: JsonObject) -> list[JsonObject]:
    tool_calls = proposed.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [cast(JsonObject, item) for item in tool_calls if isinstance(item, dict)]


def _nested(value: JsonObject, path: list[str]) -> object:
    current: object = value
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _expect_equal(
    failures: list[str],
    field: str,
    actual: object,
    expected: object,
) -> None:
    if actual != expected:
        failures.append(f"{field} expected {expected!r}, got {actual!r}")


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


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _clamped_score(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, min(5, value))
    if isinstance(value, float):
        return max(0, min(5, int(round(value))))
    if isinstance(value, str):
        try:
            return _clamped_score(float(value))
        except ValueError:
            return 0
    return 0
