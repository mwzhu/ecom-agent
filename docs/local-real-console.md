# Local Real Operator Console

Use this flow when `http://localhost:3000` should use real API/LangGraph state instead of fixture cases.

## 1. Configure the repo-root `.env`

```bash
ENVIRONMENT=development
CONSOLE_DATA_MODE=api
API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
INTERNAL_API_BASE_URL=http://localhost:8000

CLERK_ALLOW_UNVERIFIED_JWT=true
CLERK_DEV_JWT_SECRET=local-dev-secret-for-hs256-signatures
SHOPIFY_WEBHOOK_SECRET=local-dev-shopify-webhook-secret

LANGGRAPH_STUDIO_URL=http://localhost:2024
LANGGRAPH_ASSISTANT_ID=order-exception
LANGGRAPH_RUN_WEBHOOK_SECRET=local-dev-langgraph-run-webhook-secret
```

Then create the local console bearer token:

```bash
export INTERNAL_CONSOLE_BEARER_TOKEN="$(
  uv run python scripts/local_console_token.py \
    --org-id org_local_demo
)"
```

For separate local terminals, paste that token into the repo-root `.env` as
`INTERNAL_CONSOLE_BEARER_TOKEN=...`. The API and console both read that file for
server-side local settings.

## 2. Start the stack

```bash
pnpm run dev:services
pnpm run db:migrate
pnpm run dev:agents
pnpm run dev:api
pnpm --filter @ecom-agent/console exec next dev --port 3000
```

`pnpm run dev` also starts the full stack, but separate terminals are easier for debugging the first time.

## 3. Seed a local merchant and webhook mapping

```bash
uv run python scripts/seed_local_real_console.py \
  --org-id org_local_demo \
  --shop-domain local-test.myshopify.com
```

The JWT `org_id` and the seeded merchant `clerk_org_id` must match.

## 4. Create a real API-backed case

Send a signed local Shopify webhook:

```bash
uv run python scripts/send_local_shopify_webhook.py \
  --shop-domain local-test.myshopify.com \
  --secret "$SHOPIFY_WEBHOOK_SECRET"
```

The API will:

- verify the webhook signature
- resolve the shop domain to the seeded merchant
- create a case in Postgres
- create a LangGraph thread
- trigger the `order-exception` assistant
- write the run/case events back through the LangGraph completion webhook

Refresh `http://localhost:3000`. The console should show the `Live` pill, not `Simulation`.

## What approval does in this mode

When `CONSOLE_DATA_MODE=api`, the console does not fall back to fixtures. Approve/reject calls:

```text
Next console route -> FastAPI /v1/cases/{case_id}/decision -> LangGraph resume
```

The API records `case.decision_submitted` and updates the case. If the approved graph state contains executable tool calls, the API attempts them through the integration layer.

Use fake provider credentials if you only want to test orchestration. Use real staging Shopify/Stripe/Gorgias credentials only when you intentionally want external writes.
