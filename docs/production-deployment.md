# Production Deployment

This runbook moves the pilot stack from localhost and tunnels to stable URLs that design partners and provider dashboards can reach.

## Architecture

Use this first-pilot architecture:

- `api.<domain>`: Render Web Service running the Dockerized FastAPI app.
- `console.<domain>`: Vercel deployment of the Next.js operator console.
- LangGraph: LangGraph Cloud / LangSmith deployment for the `order-exception` graph.
- Postgres: Render Postgres.
- Redis: Render Redis or Upstash Redis.

Why this shape:

- Local tunnels are not production. OAuth callbacks and webhooks break when URLs rotate, tunnel processes sleep, or local machines restart.
- `langgraph dev` is a local development server, not a durable production service.
- The local agent tests use `InMemorySaver`; production approval/resume flows need durable thread checkpointing. LangGraph Cloud is the fastest pilot path for durable threads and reliable human approval resumes.
- `infra/terraform` is currently a placeholder. The first pilot uses manual Render, Vercel, and LangGraph Cloud resources. Introduce Terraform before onboarding multiple merchants or maintaining additional environments.

## Target URLs

Replace `<domain>` with the real pilot domain:

```bash
API_BASE_URL=https://api.<domain>
CONSOLE_URL=https://console.<domain>
```

Provider-facing URLs:

- Shopify OAuth callback: `https://api.<domain>/v1/integrations/shopify/callback`
- Stripe webhook: `https://api.<domain>/v1/webhooks/stripe`
- Gorgias webhook: `https://api.<domain>/v1/webhooks/gorgias`
- Gorgias app URL: `https://api.<domain>/v1/integrations/gorgias/install`
- Gorgias OAuth redirect: `https://api.<domain>/v1/integrations/gorgias/callback`

## API Docker Image

The API image builds from the repo root:

```bash
docker build -f apps/api/Dockerfile -t ecom-agent-api .
docker run --rm -p 8000:8000 --env-file .env ecom-agent-api
curl http://localhost:8000/health
```

Expected health payload:

```json
{"service":"api","status":"ok","version":"0.1.0"}
```

## Database And Migrations

Create managed Postgres and Redis before starting the Render service:

- Render Postgres for `DATABASE_URL`
- Render Redis or Upstash Redis for `REDIS_URL`

Run migrations from the repo root against the managed database:

```bash
DATABASE_URL=<managed-postgres-url> uv run alembic -c infra/alembic.ini upgrade head
```

If the Render service cannot run one-off commands from the repo root, use a Render job with the same image and command:

```bash
uv run alembic -c infra/alembic.ini upgrade head
```

Acceptance:

- `merchants`, `cases`, `case_events`, `integration_credentials`, and `webhook_sources` exist.
- The API starts with the managed `DATABASE_URL`.
- Webhook source lookup works before tenant scope is known.

## Render API

Create a Render Web Service from the repository with:

- Environment: Docker
- Dockerfile path: `apps/api/Dockerfile`
- Docker context: repo root
- Custom domain: `api.<domain>`

Set these environment variables:

```bash
ENVIRONMENT=production
API_BASE_URL=https://api.<domain>
DATABASE_URL=<managed-postgres-url>
REDIS_URL=<managed-redis-url>

CLERK_ALLOW_UNVERIFIED_JWT=false
CLERK_ISSUER=<clerk-issuer>
CLERK_JWKS_URL=<clerk-jwks-url>
CLERK_AUDIENCE=<optional>

LANGGRAPH_STUDIO_URL=<LangGraph Cloud deployment API URL>
LANGGRAPH_ASSISTANT_ID=order-exception
LANGGRAPH_RUN_WEBHOOK_SECRET=<strong-shared-secret>

SHOPIFY_CLIENT_ID=<shopify-client-id>
SHOPIFY_CLIENT_SECRET=<shopify-client-secret>
SHOPIFY_WEBHOOK_SECRET=<shopify-client-secret-or-webhook-secret>
SHOPIFY_ADMIN_API_VERSION=2025-10
SHOPIFY_OAUTH_SCOPES=read_orders,write_orders,read_customers,read_fulfillments,write_fulfillments,read_order_edits,write_order_edits

STRIPE_SECRET_KEY=<sk_test for staging, sk_live only for intentional live money movement>
STRIPE_WEBHOOK_SECRET=<whsec from Stripe endpoint>
STRIPE_ACCOUNT_ID=<acct_...>

GORGIAS_CLIENT_ID=<gorgias-client-id>
GORGIAS_CLIENT_SECRET=<gorgias-client-secret>
GORGIAS_OAUTH_SCOPES=openid email profile offline tickets:read tickets:write customers:read integrations:read
GORGIAS_WEBHOOK_SECRET=<strong-secret>

ANTHROPIC_API_KEY=<key>
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<key>
LANGSMITH_PROJECT=ecom-agent-prod

APP_KMS_KEY_ID=<production-kms-key-or-compatible-value>
LOCAL_KMS_MASTER_KEY=<required until real KMS is wired>
```

Acceptance:

```bash
curl https://api.<domain>/health
```

## LangGraph Cloud

Do not deploy `langgraph dev` to production.

Create a LangGraph Cloud / LangSmith deployment from the GitHub repo:

- Config file: `apps/agents/langgraph.json`
- Graph name: `order-exception`

Set LangGraph env:

```bash
ANTHROPIC_API_KEY=<key>
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<key>
LANGSMITH_PROJECT=ecom-agent-prod
API_BASE_URL=https://api.<domain>
LANGGRAPH_RUN_WEBHOOK_SECRET=<same as API>
```

Then set API env:

```bash
LANGGRAPH_STUDIO_URL=<LangGraph Cloud deployment API URL>
LANGGRAPH_ASSISTANT_ID=order-exception
LANGGRAPH_RUN_WEBHOOK_SECRET=<same-secret>
```

Acceptance:

- API can create a LangGraph thread.
- Webhook-created cases store `langgraph_thread_id`.
- Human approval resumes the same interrupted thread.
- Pending approval survives an API restart.

## Vercel Console

Deploy `apps/console` to Vercel:

- Project root: `apps/console`
- Custom domain: `console.<domain>`

Set env:

```bash
CONSOLE_DATA_MODE=api
CONSOLE_REQUIRE_CLERK_AUTH=true
INTERNAL_API_BASE_URL=https://api.<domain>
NEXT_PUBLIC_API_BASE_URL=https://api.<domain>
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=<pk_live_or_pk_test>
CLERK_SECRET_KEY=<sk_live_or_sk_test>
```

The console uses Clerk session JWTs for normal production operator requests. `INTERNAL_CONSOLE_BEARER_TOKEN` is only a local/internal fallback and is ignored when `ENVIRONMENT=production` or `VERCEL_ENV=production`.

Acceptance:

- `https://console.<domain>` shows the `Live` pill.
- Fixture fallback is disabled when `CONSOLE_DATA_MODE=api`.
- Approve/reject calls the hosted API.
- API rejects missing or invalid JWTs.

## Clerk Tenant Mapping

For every design partner:

1. Create a Clerk organization.
2. Insert a merchant row:

```sql
insert into merchants (clerk_org_id, name, tier)
values ('<clerk_org_id>', '<merchant_name>', 'pilot');
```

Acceptance:

- A signed-in user in Clerk org A sees only org A merchant cases.
- A signed-in user in Clerk org B sees only org B merchant cases.
- `GET /v1/cases` is tenant-scoped by the API.

## Security Checklist

Before live production:

- Keep `CLERK_ALLOW_UNVERIFIED_JWT=false`.
- Use Stripe test keys in staging.
- Use `sk_live` only when intentionally allowing real refunds.
- Confirm every write action still passes through human approval.
- Rotate any secrets used in screenshots or local testing.
- Keep `.env` out of Git and Docker images.
- Add/confirm audit events for credential installs, approvals, webhook receipt, tool calls, and credential changes.
- Move from `LOCAL_KMS_MASTER_KEY` to real KMS before scaling beyond pilot.

## End-To-End Acceptance

Shopify:

- Trigger a signed test or real Shopify webhook.
- Case appears in the console.
- Approval records `case.decision_submitted`.
- LangGraph resumes and `tool_calls` are recorded.

Stripe:

- Send a Stripe Dashboard test event.
- Stripe shows HTTP 200.
- Case appears in the console.
- Do not test live refunds with `sk_live` unless intentionally moving money.

Gorgias:

- Manual credential install works.
- OAuth callback stores a credential and `webhook_sources` mapping.
- Gorgias webhook creates a case.
- Draft reply tool works after credential install.

Console:

- Hosted console uses real API mode.
- No shared production static token is needed for normal operators.
- Tenant isolation works across Clerk orgs.

## Required Checks

Run before promoting the pilot deployment:

```bash
uv run ruff check .
uv run mypy
uv run pytest
pnpm --filter @ecom-agent/console typecheck
pnpm --filter @ecom-agent/console build
```
