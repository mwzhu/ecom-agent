# Provider Setup

Use these steps after `https://api.flowlabshq.com` is live and healthy. See `docs/permanent-urls.md` for the named tunnel/DNS setup.

## Dashboard Links

- Shopify Dev Dashboard / Partners: `https://partners.shopify.com`
- Stripe webhooks dashboard: `https://dashboard.stripe.com/webhooks`
- Gorgias Developer Portal: `https://partners.gorgias.com/login`
- Gorgias OAuth docs: `https://developers.gorgias.com/docs/oauth2-authentication-for-creating-apps-with-gorgias`

## Shopify

In the Shopify app dashboard, configure the allowed OAuth redirect URL:

```text
https://api.flowlabshq.com/v1/integrations/shopify/callback
```

The API starts installation for the current Clerk organization:

```bash
curl -H "Authorization: Bearer <clerk-jwt>" \
  "https://api.flowlabshq.com/v1/integrations/shopify/install?shop=<shop>.myshopify.com"
```

Open the returned `install_url` in a browser and approve the app.

Verify:

- `integration_credentials` contains a `shopify` credential.
- `/v1/integrations/health` reports `healthy` or clearly lists missing Shopify scopes.
- `webhook_registry` contains the required Shopify topics. Set
  `PROVIDER_WEBHOOK_REGISTRATION_MODE=live` in production to create provider webhooks
  automatically; leave `record_only` for local smoke tests.
- `webhook_sources` maps the Shopify shop domain to the merchant.
- Shopify webhooks target `https://api.flowlabshq.com/v1/webhooks/shopify`.

## Stripe

Preferred install is Stripe Connect. Configure:

```bash
STRIPE_CONNECT_CLIENT_ID=ca_...
STRIPE_SECRET_KEY=sk_live_...
```

Start Connect onboarding for the current Clerk organization:

```bash
curl -H "Authorization: Bearer <clerk-jwt>" \
  "https://api.flowlabshq.com/v1/integrations/stripe/connect/install"
```

As a backup, use a restricted API key install form. The key must allow reads for
charges/disputes and writes for refunds if refund automation is enabled.

If automatic webhook registration is not enabled yet, create a webhook endpoint:

```text
https://api.flowlabshq.com/v1/webhooks/stripe
```

Enable these events:

- `charge.dispute.created`
- `charge.dispute.updated`
- `charge.dispute.closed`
- `charge.refunded`
- `refund.created`
- `refund.failed`
- `payment_intent.payment_failed`
- `payment_intent.succeeded`

Set API env:

```bash
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_ACCOUNT_ID=acct_...
```

Install the Stripe credential for the current Clerk organization:

```bash
curl -X POST https://api.flowlabshq.com/v1/integrations/stripe/install \
  -H "Authorization: Bearer <clerk-jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "access_token": "sk_test_or_sk_live",
    "metadata": {
      "stripe_account_id": "acct_...",
      "installed_by": "production_setup"
    }
  }'
```

Verify:

- `/v1/integrations/health` reports the Stripe account id, mode, and missing scopes.
- `webhook_registry` contains Stripe events and records the endpoint id/secret when live registration is enabled.
- Stripe Dashboard test event returns HTTP 200.
- `webhook_sources` maps `acct_...` to the merchant.
- No live refund events are tested with `sk_live` unless real money movement is intended.

## Gorgias Manual Install

Use manual install first if the public app OAuth flow is not approved yet:

```bash
curl -X POST https://api.flowlabshq.com/v1/integrations/gorgias/install \
  -H "Authorization: Bearer <clerk-jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "access_token": "<gorgias-access-token>",
    "refresh_token": "<gorgias-refresh-token-if-any>",
    "metadata": {
      "gorgias_domain": "<subdomain>.gorgias.com",
      "account_domain": "<subdomain>.gorgias.com",
      "installed_by": "production_setup"
    }
  }'
```

Register the Gorgias webhook URL:

```text
https://api.flowlabshq.com/v1/webhooks/gorgias
```

Use `GORGIAS_WEBHOOK_SECRET` as the shared HMAC secret expected by the API.

Verify:

- `integration_credentials` contains a `gorgias` credential.
- `/v1/integrations/health` reports missing Gorgias scopes before automation starts.
- `webhook_registry` contains the Gorgias HTTP integration records when live registration is enabled.
- `webhook_sources` maps `<subdomain>.gorgias.com` to the merchant.
- A signed Gorgias webhook creates a case.
- `gorgias_draft_reply` can create a draft reply with the installed credential.

## Disconnect

Disconnect a provider from the current merchant:

```bash
curl -X DELETE https://api.flowlabshq.com/v1/integrations/shopify \
  -H "Authorization: Bearer <clerk-jwt>"
```

Disconnect marks credentials revoked, disables webhook registry rows, stops the
merchant/provider webhook mapping, and records an audit event. Cached provider data
should be purged or retention-marked by the data-retention worker for the merchant SLA.

## Production KMS

Production and staging must not boot with the local Fernet-only KMS. Configure:

```bash
APP_KMS_PROVIDER=managed
MANAGED_KMS_KEY_ID=arn:aws:kms:<region>:<account>:key/<id>
```

Local/test environments can use `APP_KMS_PROVIDER=local` with `LOCAL_KMS_MASTER_KEY`.

## Gorgias OAuth

For public app install, configure the Gorgias app fields:

- App URL: `https://api.flowlabshq.com/v1/integrations/gorgias/install`
- Whitelisted redirect URL: `https://api.flowlabshq.com/v1/integrations/gorgias/callback`

Gorgias calls the app URL with the account/subdomain. The API builds an account-specific authorization URL:

```text
https://<subdomain>.gorgias.com/oauth/authorize
```

The callback exchanges the code against:

```text
https://<subdomain>.gorgias.com/oauth/token
```

Required API env:

```bash
GORGIAS_CLIENT_ID=<gorgias-client-id>
GORGIAS_CLIENT_SECRET=<gorgias-client-secret>
API_BASE_URL=https://api.flowlabshq.com
```

The default scopes are:

```text
openid email profile offline tickets:read tickets:write customers:read integrations:read
```

Verify:

- The install endpoint returns or redirects to the Gorgias authorization URL.
- The callback stores `access_token`, `refresh_token`, `expires_at`, and metadata.
- Metadata includes `gorgias_domain`, `account_domain`, `scope`, and `installed_by=gorgias_oauth`.
- `webhook_sources` contains the Gorgias domain/account id.
