# Phase 0 Bootstrap

This repo implements the W0-1 bootstrap from `phase0and1.md`:

- uv Python workspace for `apps/agents`, `apps/api`, and `packages/shared-schemas`
- pnpm workspace for the Next.js console
- local Postgres 16 and Redis through Docker Compose
- LangGraph Platform-ready `langgraph.json`
- W3-5 `order-exception` graph with deterministic Phase 0 subagents, hardcoded FOPs,
  and LangGraph interrupt-based approval gates for write actions
- W6-8 eval harness with a 20-scenario `order-exception-v0` library, judge scoring,
  online review queue, and LangSmith dataset sync gate
- FastAPI health endpoint
- Next.js console shell
- CI skeleton for ruff, mypy, pytest, TypeScript, and a gated LangSmith eval job

## Local Run

```bash
open -a Docker
corepack enable
pnpm install
uv sync --all-packages --all-groups
cp .env.example .env
pnpm dev
```

`pnpm dev` starts Postgres and Redis in Docker, then runs:

- `langgraph dev` from `apps/agents` on port `2024`
- FastAPI on port `8000`
- Next.js on port `3000`

## Database Bootstrap

After Postgres is running:

```bash
pnpm db:migrate
```

This applies the W1-2 multi-tenant schema and row-level security policies through Alembic.

## External Setup Checklist

These are account-level tasks that cannot be completed from repo code alone:

- Create LangSmith projects: `ecom-agent-dev`, `ecom-agent-staging`, `ecom-agent-prod`
- Add `LANGSMITH_API_KEY` and set `LANGSMITH_TRACING=true` in deployed environments
- Create the LangGraph Platform project and connect staging deployment to `main`
- Register Stripe, Shopify, and Gorgias developer apps
- Create the Doppler project/configs and mirror the keys in `.env.example`
- Create the Clerk tenant and enable organizations

## Order Exception Agent Smoke Input

The local graph is registered as `order-exception`. A minimal input:

```json
{
  "merchant_id": "demo-merchant",
  "case_id": "case_demo_001",
  "exception_type": "fraud_triage",
  "order": {
    "id": "gid://shopify/Order/1",
    "total_price": "742.00",
    "country_code": "CA"
  }
}
```

The graph routes through a supervisor into one of the Tier 1 subagents:

- `address_change_request`
- `damaged_in_transit`
- `delivered_not_received`
- `fraud_triage`
- `inventory_conflict`
- `item_change_request`
- `order_cancellation_request`
- `order_not_picked`
- `stuck_in_transit`
- `wismo`

Write proposals return a LangGraph interrupt containing the proposed action, matched FOPs,
constraints, required approvals, and idempotent tool-call plan. Resume with a human decision,
for example `{"decision": "approve", "source": "console", "actor": "ops@example.com"}`,
to mark planned write calls as approved.

`apps/agents/langgraph.json` registers the graph for LangGraph Platform. Platform-managed
deployments are expected to provide the durable Postgres checkpointer; local unit tests
compile the graph with `build_graph_for_local()`, which wraps `InMemorySaver`, when they
exercise interrupt resume. The module-level `graph` remains uncheckpointed so LangGraph
Platform can inject the managed checkpointer at deploy/runtime.

Set `ORDER_EXCEPTION_LLM_ENABLED=true` with `ANTHROPIC_API_KEY` to place the Anthropic
supervisor/subagent layer behind the shared deterministic classifier. The deterministic route
remains the fallback and eval baseline; Anthropic may refine the route, summary, recommendation,
confidence, and rationale, but it cannot alter tool calls, matched FOP ids, hard constraints, or
approval requirements. Use `ORDER_EXCEPTION_SUPERVISOR_MODEL`,
`ORDER_EXCEPTION_COMPLEX_MODEL`, and `ORDER_EXCEPTION_FAST_MODEL` to pin Opus/Sonnet model names
per environment.

`webhook_sources` maps provider-owned account identifiers such as Shopify shop domains to
merchant ids before request tenant scope is known. It intentionally does not use tenant RLS;
access should stay constrained to webhook merchant resolution and credential install paths.
Credential installs must include provider-owned identity metadata so webhook source rows are seeded
for Shopify, Stripe, Gorgias, ShipBob, ShipStation, and Gmail. The webhook receiver no longer honors
`X-Ecom-Merchant-Id` or payload `merchant_id` as a fallback; unmapped webhook sources are rejected.

## Eval Gate

Run the Phase 0 regression gate:

```bash
pnpm run eval:gate
```

The deterministic gate loads `packages/eval-datasets/order_exception_v0.json`, invokes the
compiled graph, and checks routing, FOP matches, human approval requirements, planned tools,
and final status for auto-resolved cases. The default pass-rate threshold is 85%.

Run the optional judge pass:

```bash
uv run python scripts/run_langsmith_eval_gate.py --judge
```

By default, the judge reports deterministic fallback scores so local and CI runs stay offline.
Set `EVAL_JUDGE_ENABLED=true`, `ANTHROPIC_API_KEY`, and optionally
`ORDER_EXCEPTION_JUDGE_MODEL` to score with Claude using the rubric in
`packages/eval-datasets/order_exception_judge_rubric.md`.

To sync the same examples into LangSmith:

```bash
RUN_LANGSMITH_EVALS=true pnpm run eval:gate
```

Use `EVAL_BASELINE_PASS_RATE` in CI to fail when a branch regresses by more than 2 percentage
points from the baseline.

CI computes the pull request base-branch pass rate and passes it into the gate automatically,
so model, prompt, FOP, and routing changes cannot silently lower deterministic accuracy.

Online eval findings can queue admin review items with:

```bash
curl -X POST "$API_BASE_URL/v1/evals/online-review" \
  -H "X-Ecom-Internal-Secret: $ONLINE_EVAL_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "00000000-0000-0000-0000-000000000000",
    "case_id": "00000000-0000-0000-0000-000000000000",
    "langsmith_run_id": "run_123",
    "score": 2,
    "passed": false,
    "reason": "Judge flagged missing approval for a write action.",
    "payload": {"unsafe_actions": ["customer-facing write without approval"]}
  }'
```

The API writes an `eval.online_low_confidence` audit event and stores the queued item in
`eval_review_items`; the console reads `/v1/evals/review-queue` for the current merchant.

To export queued console corrections into the same LangSmith dataset and mark them exported:

```bash
pnpm run eval:export-corrections
```

Use `EVAL_CORRECTIONS_DRY_RUN=true` to preview the export without creating LangSmith examples or
updating `eval_corrections.status`.

## Auth And Tenant Scoping

The API now exposes authenticated smoke routes:

- `GET /v1/me` resolves a Clerk organization claim to a merchant.
- `GET /v1/cases` lists only cases for the resolved merchant.

Local development can accept HS256 Clerk-shaped JWTs when `CLERK_ALLOW_UNVERIFIED_JWT=true` and `CLERK_DEV_JWT_SECRET` is set in your ignored `.env`. Staging/prod should keep that flag `false` and provide `CLERK_ISSUER` plus `CLERK_JWKS_URL`.

The request flow is:

1. Decode Clerk bearer token.
2. Extract `org_id` from the token.
3. Look up `merchants.clerk_org_id`.
4. Set `app.merchant_id` inside the active Postgres transaction.
5. Filter repository queries by `merchant_id`.

The SQL migration also enables row-level security on tenant tables as a defense-in-depth check.

## Credential Encryption

`api.security.CredentialCipher` provides the Phase 0 envelope-encryption interface for integration credentials. Local development wraps per-merchant data keys with `LOCAL_KMS_MASTER_KEY`; production can replace that wrapper with AWS KMS while preserving the stored envelope format.

## Tracing Default

`.env.example` enables `LANGSMITH_TRACING=true` so the intended tracing setup is visible. The application default is `false`, which prevents accidental traces or billing until a developer copies `.env.example` and supplies a real LangSmith key.
