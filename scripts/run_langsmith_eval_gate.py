from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "agents" / "src"))

from agents.order_exception.evals import (  # noqa: E402
    JudgedSuiteResult,
    JudgeResult,
    ScenarioResult,
    SuiteResult,
    load_scenarios,
    run_eval_suite,
    run_judged_eval_suite,
)


def main() -> None:
    args = _parse_args()
    scenarios = load_scenarios()
    result = run_eval_suite(scenarios)
    judge_result = (
        run_judged_eval_suite(scenarios)
        if args.judge or _env_true("EVAL_JUDGE")
        else None
    )

    if args.json:
        print(json.dumps(_json_summary(result, judge_result), sort_keys=True))
    else:
        _print_summary(result, judge_result)

    if os.environ.get("RUN_LANGSMITH_EVALS") == "true":
        try:
            _sync_langsmith_dataset(scenarios, quiet=args.json)
        except Exception as exc:  # noqa: BLE001 - sync failure must not mask gate verdict.
            print(f"LangSmith sync skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    threshold = float(os.environ.get("EVAL_PASS_RATE_THRESHOLD") or "0.85")
    baseline = os.environ.get("EVAL_BASELINE_PASS_RATE") or None
    if result.pass_rate < threshold:
        raise SystemExit(
            f"Eval pass rate {result.pass_rate:.1%} is below threshold {threshold:.1%}."
        )
    if baseline is not None and result.pass_rate < float(baseline) - 0.02:
        raise SystemExit(
            f"Eval pass rate {result.pass_rate:.1%} regressed by more than 2pp from {baseline}."
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Order Exception Phase 0 eval gate.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable summary for CI baseline comparisons.",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help=(
            "Also run the optional judge pass. Without EVAL_JUDGE_ENABLED=true and "
            "ANTHROPIC_API_KEY, this uses deterministic fallback scores."
        ),
    )
    return parser.parse_args()


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").lower() == "true"


def _print_summary(result: SuiteResult, judge_result: JudgedSuiteResult | None) -> None:
    print(
        "order-exception-v0 evals: "
        f"{result.passed}/{result.total} passed ({result.pass_rate:.1%})"
    )
    for scenario_result in result.results:
        if not scenario_result.passed:
            print(f"- {scenario_result.scenario_id}: {'; '.join(scenario_result.failures)}")
    if judge_result is None:
        return
    print(
        f"judge pass: {judge_result.pass_rate:.1%}, "
        f"average score {judge_result.average_score:.2f}/5"
    )
    for item in judge_result.results:
        if item.low_confidence:
            print(f"- judge {item.scenario_id}: score {item.score}; {item.reasoning}")


def _json_summary(
    result: SuiteResult,
    judge_result: JudgedSuiteResult | None,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "total": result.total,
        "passed": result.passed,
        "failed": result.failed,
        "pass_rate": result.pass_rate,
        "failures": [_scenario_result_json(item) for item in result.results if not item.passed],
    }
    if judge_result is not None:
        summary["judge"] = {
            "total": judge_result.total,
            "passed": judge_result.passed,
            "failed": judge_result.failed,
            "pass_rate": judge_result.pass_rate,
            "average_score": judge_result.average_score,
            "low_confidence": [
                _judge_result_json(item)
                for item in judge_result.results
                if item.low_confidence
            ],
        }
    return summary


def _scenario_result_json(result: ScenarioResult) -> dict[str, object]:
    return {
        "scenario_id": result.scenario_id,
        "failures": result.failures,
    }


def _judge_result_json(result: JudgeResult) -> dict[str, object]:
    return {
        "scenario_id": result.scenario_id,
        "score": result.score,
        "passed": result.passed,
        "reasoning": result.reasoning,
        "fop_violations": result.fop_violations,
        "unsafe_actions": result.unsafe_actions,
        "source": result.source,
    }


def _sync_langsmith_dataset(scenarios: list[dict[str, Any]], *, quiet: bool = False) -> None:
    from langsmith import Client

    dataset_name = os.environ.get("LANGSMITH_EVAL_DATASET", "order-exception-v0")
    client = Client()
    try:
        dataset = client.read_dataset(dataset_name=dataset_name)
    except Exception:  # noqa: BLE001 - missing dataset should create a new one.
        dataset = client.create_dataset(
            dataset_name=dataset_name,
            description="Phase 0 Order Exception Agent scenario library.",
        )

    existing_ids: set[str] = set()
    for example in client.list_examples(dataset_id=dataset.id):
        metadata = getattr(example, "metadata", {}) or {}
        scenario_id = metadata.get("scenario_id") if isinstance(metadata, dict) else None
        if isinstance(scenario_id, str):
            existing_ids.add(scenario_id)

    created = 0
    for scenario in scenarios:
        scenario_id = str(scenario.get("id"))
        if scenario_id in existing_ids:
            continue
        client.create_example(
            dataset_id=dataset.id,
            inputs=scenario.get("input", {}),
            outputs=scenario.get("expected", {}),
            metadata={
                "scenario_id": scenario_id,
                "title": scenario.get("title"),
                "tags": scenario.get("tags", []),
            },
        )
        created += 1
    if not quiet:
        print(f"LangSmith dataset {dataset_name!r} synced; {created} new examples created.")


if __name__ == "__main__":
    main()
