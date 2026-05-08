# Permanent URLs

Use `flowlabshq.com` for stable provider OAuth and webhook URLs.

## App URLs

```text
API_BASE_URL=https://api.flowlabshq.com
CONSOLE_BASE_URL=https://console.flowlabshq.com
NEXT_PUBLIC_API_BASE_URL=https://api.flowlabshq.com
INTERNAL_API_BASE_URL=http://localhost:8000
```

`INTERNAL_API_BASE_URL` can stay local when the console process runs on the
same machine as the API. In deployed environments, set it to the private API
origin or `https://api.flowlabshq.com`.

## Cloudflare Named Tunnel

Create a named tunnel once:

```bash
cloudflared tunnel create ecom-agent-dev
cloudflared tunnel route dns ecom-agent-dev api.flowlabshq.com
cloudflared tunnel route dns ecom-agent-dev console.flowlabshq.com
```

Copy the template:

```bash
cp infra/cloudflared/flowlabshq.dev.yml.example ~/.cloudflared/ecom-agent-dev.yml
```

Update `credentials-file` if Cloudflare created the JSON somewhere else, then run:

```bash
cloudflared tunnel --config ~/.cloudflared/ecom-agent-dev.yml run ecom-agent-dev
```

Keep the local API and console running:

```bash
pnpm run dev:api
pnpm --filter @ecom-agent/console dev
```

## Provider Redirect URLs

Add these exact redirect URLs in provider dashboards:

```text
Shopify:
https://api.flowlabshq.com/v1/integrations/shopify/callback

Stripe:
https://api.flowlabshq.com/v1/integrations/stripe/connect/callback

Gorgias:
https://api.flowlabshq.com/v1/integrations/gorgias/callback
```

## Provider Webhook URLs

Use these stable webhook endpoints:

```text
https://api.flowlabshq.com/v1/webhooks/shopify
https://api.flowlabshq.com/v1/webhooks/stripe
https://api.flowlabshq.com/v1/webhooks/gorgias
```

## Smoke Test

```bash
curl https://api.flowlabshq.com/health
open https://console.flowlabshq.com
```

The console onboarding cards should now show `api.flowlabshq.com` callback URLs.
