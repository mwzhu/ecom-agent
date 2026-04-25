# Eval Datasets

`order_exception_v0.json` is the Phase 0 W6-8 scenario library for the Order Exception Agent.

Each scenario contains:

- `input`: LangGraph input for the `order-exception` assistant.
- `expected`: deterministic assertions for routing, FOP matches, human approval, tool plan, and final status when no interrupt is expected.
- `tags`: workflow and coverage labels used for reporting.

Run the deterministic gate locally:

```bash
pnpm run eval:gate
```

Run the optional judge pass:

```bash
uv run python scripts/run_langsmith_eval_gate.py --judge
```

Without `EVAL_JUDGE_ENABLED=true` and `ANTHROPIC_API_KEY`, the judge emits deterministic fallback
scores. With those variables set, Claude scores the same graph outputs against
`order_exception_judge_rubric.md`.

Sync the scenario library into LangSmith after configuring `LANGSMITH_API_KEY`:

```bash
RUN_LANGSMITH_EVALS=true pnpm run eval:gate
```

Design partner corrections from the console should be converted into additional scenarios before they are promoted into this file. Keep the hand-curated dataset stable and reviewable; use LangSmith experiments for transient runs and prompt/model comparisons.

Low-confidence online judge findings should be sent to `/v1/evals/online-review`; the API records
them in `eval_review_items` and exposes them through `GET /v1/evals/review-queue`. A console page
for operator triage will land alongside the Phase 1 M4-5 online-evaluator rollout.
