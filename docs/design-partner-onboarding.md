# Design Partner Onboarding

Phase 0 target: 5-8 Shopify DTC operators, with 1 live partner and 2-3 shadow-mode partners by the end of W8.

## Partner Fit

- $2M-$20M GMV on Shopify.
- 1-5 operations people.
- Uses at least Shopify plus one fulfillment system, one helpdesk, and Stripe or another payment processor.
- Has recurring order lifecycle exceptions at least weekly.
- Willing to share 30 days of exception history and give feedback within 24 hours during setup.

## Intake Call

Capture the last 30 days of order exceptions:

- Exception type and trigger source.
- Systems touched by the operator.
- Decision the operator made.
- Customer-facing message, if any.
- Money movement, cancellation, fulfillment hold, or address edit involved.
- Policy or tribal knowledge used.
- Time spent and whether the action was reversible.

Convert each high-confidence example into `packages/eval-datasets/order_exception_v0.json` or a merchant-private LangSmith example before enabling writes.

## Onboarding States

- `candidate`: intro complete; no data access.
- `connected`: credentials installed; read-only ingest is working.
- `shadow`: agent proposes actions, humans compare against real operator decisions, no writes execute.
- `approval_live`: writes are available, but money movement and fulfillment changes require approval.
- `trusted_lane`: narrow autonomous lane approved for non-money, reversible actions.

## Shadow Mode Exit

Before a partner can move to `approval_live`:

- At least 20 historical or synthetic scenarios exist for that merchant's top exception classes.
- Deterministic eval gate is above 85%.
- Judge pass has no queued low-confidence findings for the partner's top scenarios.
- No critical FOP violation is open.
- Console corrections and online eval review items from shadow mode are reviewed and either
  added to evals or explicitly rejected.
- All write actions have idempotency keys and appear in `tool_calls`.

## Live Partner Runbook

Daily:

- Review every open and pending approval case in the console.
- Record a correction whenever the proposal is wrong or incomplete.
- Add missing policies to the partner's FOP backlog.

Weekly:

- Run `pnpm run eval:gate` before any prompt, FOP, or routing change.
- Run `uv run python scripts/run_langsmith_eval_gate.py --judge` before enabling a new
  approval-live lane.
- Export new accepted corrections with `pnpm run eval:export-corrections`, then promote
  representative cases into the curated scenario library.
- Report time saved, auto-resolution rate, approval rate, correction rate, and top unresolved exception pattern.

## Phase 0 Success Metrics

- 1 partner actively using the agent for real order exception review.
- 2-3 partners in shadow mode.
- Top 10 exception patterns represented in evals.
- >85% deterministic pass rate on `order-exception-v0`.
- Every live case has a LangGraph thread id, audit events, and a console review path.
