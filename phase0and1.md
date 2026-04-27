# Technical Implementation Plan — Phase 0 & Phase 1

## Context

The repo currently contains only `product_roadmap.md`. We're greenfield-building the agentic ops platform it describes — wedge is the **Order Exception Agent** for Shopify DTC brands ($1M–$50M GMV). This plan covers the first 6 months:

- **Phase 0 (W0–8):** prove the wedge with 5–8 design partners, land a working prototype on 1 partner, build the agent harness + integration layer + eval infra.
- **Phase 1 (M2–6):** ship the paid MVP with the FOP engine, case console, Slack, full observability, and billing. Convert design partners + new sign-ups to 15–25 paying merchants.

### Locked architectural decisions (from clarifying Qs)

1. **Runtime:** LangGraph **Python** SDK.
2. **Deployment:** **LangGraph Platform** (managed) for agent graphs + checkpointing + HITL.
3. **Tenancy:** **Multi-tenant from day 1** (merchant_id scoping everywhere).
4. **Phase 1 surfaces:** **Full Next.js web console + Slack**.
5. **Observability:** LangSmith for tracing, datasets, experiments, alerts.

All Phase 0 code is forward-compatible with Phase 1 — no rewrites.

-----

## Stack

| Layer | Choice | Reason |
|---|---|---|
| Agent runtime | LangGraph (Python) on LangGraph Platform | Managed threads, checkpointing, `interrupt()` HITL, native LangSmith integration |
| Primary model | Claude Opus 4.7 (reasoning) + Claude Sonnet 4.6 (cheap subtasks) via `langchain-anthropic` | Roadmap specifies Claude-first; use prompt caching on system + FOP context |
| Observability | LangSmith (tracing, datasets, experiments, online evals) | Roadmap-specified; tightest LangGraph integration |
| Backend API | FastAPI (Python 3.12), uvicorn | Same runtime as agents; webhooks + REST for console |
| DB | Postgres 16 on **Neon** (single host through Phase 1 and beyond) | Serverless Postgres, branching for eval sandboxes + preview envs, SOC 2 Type 2, scales past Phase 2 without a migration. LangGraph Platform keeps its own Postgres for checkpoints; this is our app DB |
| Cache/queue | Redis (Upstash) | Rate limiting, webhook dedupe, Slack signature window |
| Frontend | Next.js 15 App Router, TypeScript, shadcn/ui, Tailwind | Industry default; server components reduce surface area |
| Auth | Clerk (orgs = merchants, SSO-ready) | Multi-tenant from day 1 without building identity |
| Secrets (app) | Doppler | Team-friendly envs |
| Secrets (merchant tokens) | AWS KMS envelope encryption in `integration_credentials` table | Per-merchant data key, app key in KMS — meets SOC 2 Type I requirement |
| Hosting | LangGraph Platform (agents), Fly.io (API + webhooks), Vercel (console), Neon (DB) | Small blast radius, SOC 2-friendly vendors, no mid-plan migrations |
| CI | GitHub Actions: ruff, mypy, pytest, LangSmith eval regression gate | |
| Billing | Stripe (M5) with metered usage per resolved case | Roadmap tiers ($500/$1,500/$3,500) |

-----

## Repo layout (monorepo)

```
ecom-agent/
  apps/
    agents/              # LangGraph Python package — deployed to LangGraph Platform
      src/agents/
        order_exception/ # supervisor graph + subagents
        shared/          # state schemas, HITL primitives, FOP evaluator
      langgraph.json     # LangGraph Platform config (graphs, env, dependencies)
      pyproject.toml
    api/                 # FastAPI — webhooks, REST for console, Slack events
      src/api/
        webhooks/        # shopify, stripe, gorgias
        routes/          # cases, fops, merchants, billing
        slack/           # Bolt app
        integrations/    # shopify, stripe, gorgias, shipbob, shipstation, klaviyo, gmail
        auth/            # Clerk JWT verification, merchant scoping middleware
    console/             # Next.js 15 — merchant-facing case console
  packages/
    shared-schemas/      # pydantic + zod generated from single source (datamodel-code-generator)
    eval-datasets/       # ground-truth scenarios, fixtures, LangSmith dataset IDs
  infra/
    alembic/             # DB migrations
    docker-compose.yml   # local dev: Postgres + Redis + langgraph dev server
    terraform/           # Phase 1 M4: Fly, RDS, KMS, S3 for audit blobs
  .github/workflows/
```

Two deployables: `apps/agents` ships to LangGraph Platform; `apps/api` + `apps/console` ship to Fly + Vercel. They communicate via the LangGraph Platform SDK (`langgraph-sdk` from the API to trigger/resume runs) and webhooks in reverse.

-----

## Phase 0 — Foundation (Weeks 0–8)

### W0–1: Bootstrap

- Init monorepo (`uv` for Python workspaces, `pnpm` for TS).
- `docker-compose`: Postgres 16, Redis, `langgraph dev` (local LangGraph server) for local agent iteration.
- Create LangSmith project `ecom-agent-dev`, `-staging`, `-prod`. Wire `LANGSMITH_TRACING=true` + project env vars.
- Create LangGraph Platform project; set up staging deployment from `main` branch.
- Stripe/Shopify/Gorgias developer apps registered.
- CI skeleton: ruff + mypy (strict) + pytest + LangSmith eval job (gated, see W6–8).
- Doppler set up, secrets pulled into all envs.
- Clerk tenant created, orgs feature enabled.

**Exit:** `pnpm dev` brings up the full stack locally; a stub LangGraph prints a trace in LangSmith.

### W1–2: Multi-tenant data model + auth plumbing

Postgres schema (all tables have `merchant_id uuid not null` except `merchants`):

- `merchants` (id, clerk_org_id, name, tier, created_at)
- `integration_credentials` (merchant_id, provider, encrypted_token, encrypted_refresh, expires_at, kms_key_id)
- `cases` (id, merchant_id, type, status, subject_ref jsonb, langgraph_thread_id, created_at, resolved_at, resolution jsonb)
- `case_events` (id, case_id, merchant_id, kind, payload jsonb, langsmith_run_id, actor, created_at) — the audit log
- `fops` (id, merchant_id, version, nl_text, structured jsonb, status, parent_id, created_by, created_at)
- `webhook_events` (id, provider, event_id unique, merchant_id, payload jsonb, processed_at) — dedupe
- `tool_calls` (id, case_id, merchant_id, tool, input jsonb, output jsonb, idempotency_key, status, created_at) — idempotency + retry ledger

Row-level isolation: FastAPI dependency sets `SET LOCAL app.merchant_id = $1` inside a per-request transaction; every query filters on it. Clerk JWT → org_id → merchant lookup → scope.

KMS helper: `encrypt(merchant_id, plaintext) -> ciphertext` using per-merchant data key (generated on merchant create), app-encrypted under a single KMS CMK.

**Exit:** auth round-trip works from Clerk → API → DB with merchant scoping; tenant leakage test suite passes (two merchants, assert no cross-reads).

### W2–3: Integration layer v1 (the tools)

One Python module per provider under `apps/api/src/api/integrations/`. Each module exports:

- Typed pydantic request/response models.
- `async` client class with per-merchant credential injection.
- A `@tool` wrapper (LangGraph tool) that validates input, logs to `tool_calls` with an idempotency key, and surfaces errors in a normalized taxonomy (`RETRYABLE`, `AUTH_EXPIRED`, `RATE_LIMITED`, `FATAL`).

Priority order (top 5 for the Exception Agent v0):

1. **Shopify GraphQL Admin API** — orders, customers, fulfillments, risk assessments, order updates, refunds, notes. OAuth install flow + offline tokens. First-class.
2. **Stripe** — charges, refunds, disputes. Read-only for Phase 0; writes gated.
3. **Gorgias** — tickets, customer context, reply drafting.
4. **ShipBob + ShipStation** — order status, tracking, fulfillment holds.
5. **Gmail** — via OAuth, read incoming customer mail thread history (for address correction workflows).

Webhook receivers (`apps/api/src/api/webhooks/`):

- FastAPI routes with provider HMAC verification.
- Dedupe on `webhook_events.event_id`.
- Enqueue → trigger LangGraph run via `langgraph_sdk.get_client().runs.create(thread_id, assistant_id, input=...)`.

**Reusable pattern (don't skip):** every write tool takes an `idempotency_key` (sha256 of case_id + intent + stable payload fields) and the integration layer checks `tool_calls` before re-executing. This is what makes replay safe.

**Klaviyo** and **Outlook** deferred to Phase 1 M3.

**Exit:** end-to-end write action works — staging merchant gets a test refund issued via Stripe from an agent-triggered run, visible in LangSmith trace, logged in `tool_calls` + `case_events`.

### W3–5: Order Exception Agent v0

LangGraph structure (one graph, deployed as assistant `order-exception`):

```
Supervisor (routes by exception type)
 ├─ address_validation_subagent
 ├─ fraud_triage_subagent
 ├─ payment_failure_subagent
 ├─ high_value_review_subagent
 └─ inventory_conflict_subagent
```

State (`TypedDict`):

```python
class OrderExceptionState(TypedDict):
    merchant_id: str
    case_id: str
    exception_type: Literal[...]
    order: dict                 # Shopify order snapshot
    context: dict               # customer, prior orders, risk, etc.
    active_fops: list[FOP]      # loaded at graph start
    tool_calls_so_far: list
    proposed_action: dict | None
    human_decision: dict | None # populated after interrupt
    resolution: dict | None
```

Key mechanics:

- **Checkpointer:** LangGraph Platform's built-in Postgres checkpointer. Thread = case.
- **HITL:** every write tool is wrapped in an `interrupt()` node that posts a proposal (human-readable summary + structured diff) to `case_events` and notifies the console/Slack. `Command(resume=...)` applies `human_decision`.
- **Memory:** LangGraph `Store` namespace per merchant (`("merchant", merchant_id)`) for learned preferences — used only as context, not authoritative (authoritative = FOPs).
- **FOPs (Phase 0):** hardcoded YAML per merchant under `apps/agents/fops/<merchant_slug>.yaml`. Parsed at graph start into `active_fops`. No NL parser yet — that's Phase 1 M2. Evaluator returns `(constraints, required_approvals, auto_actions)` and injects constraints as a system-prompt block for the subagent.
- **Model routing:** Opus 4.7 for supervisor routing + fraud_triage + high_value_review (complex judgment); Sonnet 4.6 for address_validation + payment_failure (mechanical). Prompt caching on the static system + FOP block.
- **Guardrails:** money-movement tools (refund, capture, cancel) also hit a Python validator that checks FOP constraints at call time — defense in depth against the LLM ignoring instructions.

**Exit:** agent runs on staging Shopify shop, resolves 3 canned scenarios end-to-end (invalid address → customer draft → hold; fraud score 85 → cancel + refund per FOP; OOS inventory → partial ship proposal).

### W5–6: Internal admin panel (becomes case console in Phase 1)

Minimal Next.js app at `apps/console`:

- Case list + detail, with LangSmith trace iframe (use LangSmith's shareable run URL).
- Approve/Modify/Reject buttons → API → `langgraph_sdk.runs.create(thread_id, command={"resume": ...})`.
- "Correct this" UI: records ground-truth resolution into `eval_corrections` table → nightly job appends to LangSmith dataset.
- FOP YAML viewer (read-only in Phase 0).
- Multi-merchant switcher (internal-only for now).

This is explicitly admin-only UI to unblock the team; Phase 1 polishes it into the merchant-facing console. Same codebase, same routes.

**Exit:** we can run the agent on a real design partner order and review/correct from the panel.

### W6–8: Evals

Eval infrastructure (the hiring/credibility moat the roadmap calls out):

- LangSmith dataset `order-exception-v0` with 20+ scenarios (from design-partner interviews + synthetic variants). Each example: input order + context + active FOPs + expected resolution.
- Eval harness in `apps/agents/tests/evals/`:
  - Deterministic assertions where possible (e.g., "must call `cancel_order` with reason X").
  - LLM-as-judge (Claude Opus 4.7) for decision-quality scoring with a rubric tied to FOPs.
  - Regression gate: on every PR, run dataset experiment via LangSmith; fail CI if pass rate drops >2pp vs. `main`.
- Online evals: LangSmith online evaluator on `-prod` project tags any run where the judge flags low confidence → queues for review in admin panel.

**Phase 0 exit criteria (roadmap-aligned):**

- 1 design partner actively using the agent.
- Eval suite covers top 10 exception types at >85% accuracy (measured via LangSmith experiments).
- Documented scenario library of 20+ exception types.
- First engineer hired or contracted.

-----

## Phase 1 — MVP Order Exception Agent (Months 2–6)

### M2: FOP engine — full parser + execution

Moves FOPs from YAML config to a live, merchant-editable system. This is the core Phase 1 differentiator.

**Parser (`apps/api/src/api/fops/parser.py`):**

- Input: NL text, merchant context (integrations, known fields).
- LLM (Opus 4.7 + structured output) → `ParsedFOP` pydantic: `{scope, conditions[], actions[], thresholds, priority_hint, confidence}`.
- **Confirmation-before-activation:** parser returns a plain-English re-rendering; merchant sees "I understood this as: WHEN … THEN …" in the console. Nothing activates without explicit confirm.
- Static conflict checker runs at confirm time: diff against active FOPs; surface overlaps ("Rule #12 covers VIP returns; this new rule contradicts").
- Versioning: every save = new row with `parent_id` pointer; `status` transitions (`draft → active → disabled/superseded`); prior versions retained.

**Executor (`apps/agents/src/agents/shared/fop_evaluator.py`):**

- Called at supervisor entry. Loads all `active` FOPs for the merchant, filters to those whose `scope` matches the current workflow + context.
- Returns `(system_prompt_block, hard_constraints, required_approvals)` consumed by subagents.
- `hard_constraints` are also evaluated in Python at each write-tool call site — guardrail that doesn't trust the LLM.

**Data:**

- `fops` table (versioned as W1–2).
- `fop_executions` (fop_id, case_id, matched_at, action_influenced) — powers the impact report on a FOP.

**Console UI for FOPs:** list, create (NL textarea → parse → confirm), view (shows parse + execution history + diff from parent), disable, rollback.

### M2–3: Case console v1 (merchant-facing polish)

Starting from the W5–6 admin panel:

- Clerk auth, merchant org scoping.
- Case list with filters (type, status, date, value).
- Case detail: timeline of `case_events`, embedded LangSmith trace, one-click approve/modify/reject, FOPs that influenced the decision linked inline.
- Audit log: every action, tool call, reasoning step (reasoning collapsed by default); export to CSV for compliance.
- FOP builder UI (from M2).
- Weekly digest: scheduled LangGraph run on Monday 9am merchant-local — pulls resolved cases, time-saved estimate, exceptions needing attention — emails + posts to Slack.

### M2–3: Slack integration

- Slack Bolt for Python app, OAuth install → writes to `integration_credentials`.
- Event subscriptions: `app_mention`, `message.im`, interactivity.
- Exception cards posted to merchant ops channel w/ Block Kit — approve/modify/reject buttons call back into a signed endpoint that resumes the LangGraph run.
- Approval replay: the interrupt in LangGraph accepts either `{source: "console", decision: ...}` or `{source: "slack", decision: ..., user: ...}`; both paths logged in `case_events`.

### M3–4: Broader exception coverage

Add subagents for the remaining roadmap categories:

- Fulfillment: split-shipment, stuck orders, 3PL mismatch.
- Post-ship: stuck shipments, delivery exceptions, "delivered but not received" claims.

Each is a new subagent under the supervisor with dedicated tool sets. Expand `order-exception-v0` dataset → `order-exception-v1` with 100+ scenarios spanning all 10 exception types. Target: >85% accuracy on all, >90% on the top 5.

Add **Klaviyo** (for customer comms templates), **ShipStation** parity with ShipBob, **Outlook** parity with Gmail.

### M4–5: Scale + reliability

- Per-merchant per-tool rate limiting via Redis token bucket.
- LangGraph retry policies on transient errors (`RETRYABLE`, `RATE_LIMITED`).
- Integration health dashboard (admin): last-success, error rate, token expiry per merchant/provider.
- Secret rotation cron: refresh OAuth tokens proactively.
- PII redaction in `case_events` payloads (email, name, address) with per-merchant toggle — defaults on.
- **SOC 2 Type I prep** with Vanta: policies, access reviews, backups, encryption. Roadmap wants Type I by end of Phase 1.
- On-call runbook + alerting: PagerDuty, LangSmith online-eval drop alert, webhook backlog alert, token-expiry alert.

### M5–6: Billing + self-serve launch

- Stripe billing: Starter $500 / Growth $1,500 / Scale $3,500; usage metering on `case.resolved` event; overage SKU per action above tier.
- Self-serve onboarding:
  1. Sign up → Clerk creates org → merchant row.
  2. Shopify OAuth install (our app must be published private-listing for Phase 1; public listing is a Phase 2 goal).
  3. Auto-detect installed apps via Shopify — suggest ShipBob/Gorgias/Klaviyo OAuths.
  4. FOP onboarding: 5 templated starter FOPs (by ICP vertical, preset but editable).
  5. First exception surfaced in <10 minutes from install.
- Landing page + docs (Mintlify).
- Launch to 15–25 paying merchants (design partners convert + warm pipeline).

**Phase 1 exit criteria (roadmap-aligned):**

- 15–25 paying merchants, $15–40k MRR.
- 70%+ exceptions fully resolved autonomously, measured from `case.resolved` with `human_touches == 0`.
- >90% policy-compliance accuracy, measured via LangSmith experiments on a monthly-refreshed eval set.
- <30s median detection-to-action (p50 `webhook_events.created_at → first case_event`).
- 3 public case studies.

-----

## Cross-cutting concerns

**Observability:**

- Every LangGraph run ends with a LangSmith `run_id` written to `case_events.langsmith_run_id`. The console links out to the run. Traces include tool inputs/outputs, intermediate reasoning, and interrupts.
- Structured logs (JSON) everywhere with `merchant_id`, `case_id`, `run_id` — shipped to Datadog from M4.
- Custom LangSmith feedback scores written back on human correction (via console "Correct this") — feeds both evals and future fine-tune data.

**Evals (first-class, not afterthought):**

- Every workflow has a dataset. CI gates merges on regression. This is the moat.
- New cases automatically proposed as dataset candidates via an online evaluator ("novel pattern detected"); human review accepts them → dataset grows organically.
- Public-facing eval scoreboard (M6): "our chargeback agent has 94% accuracy on 1,200 ground-truth cases" — sales artifact per roadmap.

**Security:**

- Merchant tokens: AWS KMS envelope encryption, per-merchant data keys, rotated on OAuth refresh.
- Agent write tools require the merchant's token, scoped OAuth (never admin-of-admin).
- Row-level tenant scoping enforced at the DB session level (not just app code) — `SET LOCAL app.merchant_id` inside a per-request transaction.
- No merchant data leaves our infra in prompts except what's needed; prompt caching configured to avoid cross-merchant cache sharing (cache key includes merchant_id).
- SOC 2 Type I by end of Phase 1.

**Idempotency:**

- Every write tool uses an idempotency key derived from `(case_id, intent, payload_hash)`; the `tool_calls` ledger is checked before execution. Critical for LangGraph replay after HITL.

-----

## Critical files to create (path map)

| Concern | Path |
|---|---|
| Supervisor graph | `apps/agents/src/agents/order_exception/graph.py` |
| Subagents | `apps/agents/src/agents/order_exception/subagents/{address,fraud,payment,high_value,inventory_conflict}.py` |
| HITL helpers | `apps/agents/src/agents/shared/hitl.py` |
| FOP evaluator | `apps/agents/src/agents/shared/fop_evaluator.py` |
| LangGraph config | `apps/agents/langgraph.json` |
| FOP parser | `apps/api/src/api/fops/parser.py` |
| FOP CRUD routes | `apps/api/src/api/routes/fops.py` |
| Webhooks | `apps/api/src/api/webhooks/{shopify,stripe,gorgias}.py` |
| Integration clients | `apps/api/src/api/integrations/{shopify,stripe,gorgias,shipbob,shipstation,klaviyo,gmail}.py` |
| Auth + tenant middleware | `apps/api/src/api/auth/{clerk,tenant}.py` |
| KMS encryption helper | `apps/api/src/api/auth/crypto.py` |
| Slack app | `apps/api/src/api/slack/app.py` |
| DB migrations | `infra/alembic/versions/` |
| Console case list/detail | `apps/console/app/(merchant)/cases/` |
| Console FOP builder | `apps/console/app/(merchant)/fops/` |
| Eval harness | `apps/agents/tests/evals/run_dataset.py` |
| Eval datasets | `packages/eval-datasets/order_exception/` |

-----

## Risks & open questions (to revisit)

1. **LangGraph Platform vendor lock-in.** Mitigation: all graphs are pure `langgraph` library code — Platform only hosts them. Can fall back to self-host in a week if needed.
2. **HITL latency on Slack.** Median approval time may blow the <30s SLA. Mitigation: track `interrupt_waiting_time` in LangSmith; publish to merchants as "your approval latency = X" so they see the bottleneck is on their side.
3. **Integration churn.** Shopify/Stripe/Gorgias APIs change. Mitigation: contract tests against each provider's sandbox run nightly; alert on schema drift.
4. **Cross-tenant leakage in prompts.** Mitigation: tenant-scoped prompt caching keys; static analyzer in CI that flags any global `PromptTemplate` interpolated with merchant data without a merchant_id key.
5. **Eval label rot.** Design-partner ground truth may drift from their own policy. Mitigation: attach FOP version + merchant policy snapshot to each dataset example; re-validate quarterly.
6. **Buyer persona (roadmap open Q).** Phase 0 interviews should answer: founder vs head of ops vs CX lead. Plan: embed this question in the design partner interview template.

-----

## Verification

**Phase 0 acceptance (end of W8):**

- `pnpm dev` boots all services locally; `curl localhost:8000/healthz` → 200.
- Run `make eval` → LangSmith experiment reports ≥85% on the top-10 eval set.
- Script `scripts/e2e_address_correction.py` against staging Shopify shop → verifies: webhook received → case created → agent proposes draft email → admin approves → Gmail sends → case resolved. Trace visible in LangSmith. `case_events` shows the full audit chain.
- Tenant isolation test (`pytest apps/api/tests/test_tenancy.py`) — creates two merchants, asserts zero cross-reads across all tables.
- Design partner #1 has resolved ≥10 real cases with agent assistance over ≥2 weeks.

**Phase 1 acceptance (end of M6):**

- ≥15 paying merchants live with self-serve install.
- Stripe billing reports ≥$15k MRR.
- 30-day production metrics: ≥70% auto-resolved (no human touch), ≥90% policy-compliant, p50 detect-to-action <30s.
- LangSmith public-ish scoreboard shows current dataset + accuracy per agent.
- SOC 2 Type I audit kicked off with observation window started.
- 3 case studies drafted with named customers.
