# Ecom Agent

Agentic operations platform for ecommerce brands. Phase 0 bootstraps the monorepo, local services, and a stub Order Exception Agent that can be run with LangGraph locally.

## Quick Start

Requirements:

- Python 3.12+
- `uv`
- Node.js 20+
- Corepack or `pnpm`
- Docker

```bash
open -a Docker
corepack enable
pnpm install
uv sync --all-packages --all-groups
cp .env.example .env
pnpm dev
```

Local services:

- API: http://localhost:8000
- Console: http://localhost:3000
- LangGraph dev server: http://localhost:2024
- Postgres: localhost:5432
- Redis: localhost:6379

See [docs/phase0-bootstrap.md](docs/phase0-bootstrap.md) for the external LangSmith, LangGraph Platform, Doppler, Clerk, Stripe, Shopify, and Gorgias setup checklist.

## Database Bootstrap

After Postgres is running:

```bash
pnpm db:migrate
```

This applies the Phase 0 multi-tenant schema and row-level security policies for merchants, credentials, cases, case events, FOPs, webhook dedupe, and tool call idempotency.

## Order Exception Evals

The Phase 0 eval library lives at `packages/eval-datasets/order_exception_v0.json`.

```bash
pnpm run eval:gate
```

The gate runs deterministic assertions for routing, FOP matches, approval requirements, and tool plans. Add `--judge` or set `EVAL_JUDGE=true` to run the judge pass; it uses deterministic fallback scores unless `EVAL_JUDGE_ENABLED=true` and `ANTHROPIC_API_KEY` are set. Set `RUN_LANGSMITH_EVALS=true` with `LANGSMITH_API_KEY` to sync the scenario library into the `order-exception-v0` LangSmith dataset. Online low-confidence judge findings can be posted to `/v1/evals/online-review` with `ONLINE_EVAL_WEBHOOK_SECRET` and are readable via `GET /v1/evals/review-queue` (console page ships with the Phase 1 M4-5 online evaluator). Run `pnpm run eval:export-corrections` to export queued console corrections into LangSmith and mark them exported.

See [docs/design-partner-onboarding.md](docs/design-partner-onboarding.md) for the W6-8 design partner intake, shadow-mode, and live-readiness runbook.
# ecom-agent
