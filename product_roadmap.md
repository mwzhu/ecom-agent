# Product Roadmap — Agentic Ops for Ecommerce

*Version 0.1 · Living document*

-----

## Executive Summary

We're building an AI agent platform for ecommerce operators that automates the operational workflows that today span multiple systems and consume dozens of hours per week.

The existing landscape is split into two categories, neither of which solves the core problem. Customer service AI (Gorgias, Siena, Zowie, Yuma) owns ticket resolution but not operations. Rule-based workflow tools (MESA, Shopify Flow, Zapier) handle simple if-this-then-that but break on the messy cross-system edge cases that actually eat operator time. The only player going after cross-system operations at the agent layer — Duvo — is focused exclusively on enterprise retail with SAP integrations and six-figure contracts.

That leaves a large, underserved middle: DTC brands doing $1M–$50M in GMV who are too small for Duvo but too complex for Zapier, running 10+ SaaS tools with small ops teams drowning in exception handling, returns, reconciliation, and supplier coordination.

Our wedge is **order lifecycle exception handling** — the daily operational fires that span Shopify, 3PLs, payment processors, helpdesks, and email. From there we expand into returns orchestration, inventory replenishment, chargeback defense, and reconciliation, becoming the operational nervous system of the modern ecommerce brand.

-----

## Market Opportunity

**ICP:** DTC brands at $1M–$50M GMV, primarily on Shopify. They typically have 1-5 operations people, use 5-15 SaaS tools, and face enterprise-level operational complexity without enterprise-level resources.

**Why now:**

- Agentic infrastructure maturity — MCP, Shopify MCP, ACP/UCP protocols make cross-system work dramatically cheaper than 12 months ago
- Model capability — frontier models can now reason across messy, real-world operational context reliably enough for production
- Economic pressure — margins are compressing, hiring is constrained; brands are actively looking for operational leverage
- Platform permission — Shopify is explicitly encouraging agentic storefronts and merchant-side agents

**Competitive gap (full breakdown in separate landscape doc):**

- Gorgias / Siena / Zowie / Yuma — ticket resolution only, don't execute cross-system operations
- MESA / Shopify Flow / Zapier — rule-based, break on exception cases
- Duvo — enterprise retail only, SAP-first
- Loop Returns / AfterShip — single-workflow SaaS, rule-based
- Alloy Automation — infrastructure layer for builders, not end-user product

-----

## Product Vision

A digital operations teammate that lives inside every merchant's existing stack. Merchants grant it access to their tools, express their policies in plain language, and the agent autonomously executes operational work — flagging exceptions, resolving issues, and acting across systems with full audit trails. Over time, the agent learns the merchant's specific playbook and becomes the operational brain of the business.

The long-term bet: every ecommerce brand will have a team of specialized agents handling operations, and we're the platform that team lives on.

-----

## Design Principles

1. **Cross-system first.** Every workflow we build must touch 3+ systems. Single-system automation is commodity and doesn't earn the price point we need.
1. **Agents, not bots.** Reasoning and judgment over rigid rules. Our moat is handling the messy edge cases that break rule-based tools.
1. **Human-in-the-loop by default.** Risky actions (refunds, public posts, permanent deletions, supplier-facing comms) require approval until confidence is earned, then unlock autonomy.
1. **Auditable by design.** Every action has a full trace. Ops leaders must be able to see what happened and why, and replay it.
1. **Policy-driven, not workflow-driven.** Merchants express rules in natural language ("always hold first-time international orders over $300"); agents translate to actions. This is what separates us from Zapier.
1. **Learn from outcomes.** Every human correction improves the agent. Evals are a first-class product surface, not internal tooling.
1. **Fast time-to-value.** A working automation in a week of integration work, not a quarter. If onboarding takes months, we lose.

-----

## FOPs — The Finance/Fulfillment Operating Procedures Engine

The FOPs engine is our core technical differentiator. Everything else in the product (integrations, case console, workflow packs) can be replicated by a competent team. The FOPs engine — and the infrastructure that makes it work in production — is what separates this from Zapier + Claude, from Workato Genie, and from Duvo's custom-build model.

This is the Decagon AOP equivalent for ecommerce operations. Merchants express policies in natural language; the system compiles them into executable constraints; agents reason under those constraints with code-level precision on critical actions.

### What a FOP is

A Finance/Fulfillment Operating Procedure is a merchant-defined rule that governs how the agent behaves in a specific situation. Expressed in plain English, stored as both natural language (for display and editing) and structured logic (for execution).

Examples across the workflow packs:

**Order exceptions:**
- "Hold any order over $500 from a first-time international customer for manual review"
- "If a fraud score is above 80, cancel automatically and refund. Between 50-80, flag for review. Below 50, proceed."
- "Orders from countries on our blocklist (Russia, Nigeria, Belarus) get canceled and refunded — don't escalate to me"

**Returns:**
- "Refunds under $25 — just issue the refund, don't require the item back"
- "Returned clothing that appears worn goes to Write-Offs, not Restock"
- "VIP customers (5+ orders or $1000+ LTV) get free return shipping regardless of reason"

**Reconciliation:**
- "Stripe processing fees get coded to GL 6500, not netted against revenue"
- "Shopify POS transactions are Retail Revenue; online orders are E-commerce Revenue"
- "Amazon FBA fees go to Marketplace Fees, not Shipping"

**Inventory:**
- "When any SKU drops below 2 weeks of supply at current velocity, send me a Slack alert"
- "Never oversell the Limited Edition line — if warehouse shows 0, take the listing down even if Shopify shows stock"

**Chargebacks:**
- "Chargebacks under $50 — accept automatically, not worth the evidence compilation time"
- "For 'product not received' chargebacks with delivery confirmation, auto-submit the full evidence package"

### Architecture

**Storage layer.** Each FOP stored as a versioned record containing:
- Natural language text (canonical, editable)
- Parsed structured form (conditions, actions, thresholds, scope)
- Execution metadata (firing count, last fired, accuracy from corrections)
- Audit trail (who created, who edited, when, why)
- Status (draft, active, disabled, superseded)

**Parsing layer.** LLM takes natural language input and produces structured logic. The critical property: the parser's output is shown back to the merchant in plain English before activation. "I understood this as: WHEN order.country IN ['RU','NG','BY'] THEN cancel_order() AND refund_payment() AND skip_escalation. Correct?" Merchant confirms, edits, or rejects. Nothing compiles into production without explicit confirmation.

**Execution layer.** When the agent considers an action, it:
1. Pulls all active FOPs scoped to the current workflow
2. Evaluates which FOPs match the current context
3. Applies matching FOPs as constraints on the agent's decision space
4. Executes within those constraints, with code-level guardrails for critical operations (money movement, customer communication, inventory writes)

**Conflict resolution.** When FOPs conflict, surface it to the merchant rather than silently picking one. "Rule #12 says VIP customers get free returns; Rule #28 says international returns charge the customer. Customer Sarah Chen is both VIP and international. Which takes priority?" Once resolved, the priority ordering is stored as a meta-rule.

**Versioning and rollback.** Every FOP change creates a new version. Prior versions retained. Merchants can see what changed, when, why, and roll back if a rule change causes problems. Each FOP version has an execution log of the actions it influenced, so the impact of a change is measurable.

### The learning loop

FOPs are not static. Three sources of new/updated rules:

**Merchant-authored.** The baseline — merchant types a rule, confirms the parse, activates it.

**Document extraction.** Merchant uploads their existing SOPs, returns policy, expense guidelines, accounting manual. LLM extracts candidate FOPs, presents each for confirmation. Especially powerful in onboarding: a new merchant with a prior bookkeeper's notes bootstraps 20-40 rules in minutes.

**Correction-derived.** When the agent is overridden 3+ times with the same pattern, the system surfaces it: "You've recategorized Stripe POS revenue to Retail Revenue 5 times in the last week. Create a FOP?" Merchant confirms, and the system generates the rule text, presents it for review, and activates. This is the continual learning mechanism — the product gets measurably smarter for each merchant as they use it.

### Why this is the moat

**Policies are sticky.** A merchant who has defined 50 FOPs over six months has encoded their entire operational playbook into our system. Switching costs aren't about data portability — they're about losing the institutional knowledge that lives in those rules.

**Policies compound.** Every FOP a merchant creates makes their specific agent better for them. Every correction-derived FOP proves the learning loop works. Over time, each merchant's agent is uniquely trained to their business in a way no competitor can replicate without the same deployment history.

**Policies are the training data.** Across hundreds of merchants, the FOPs library becomes the richest structured dataset of real-world ecommerce operations logic in existence. New merchants can be offered starter templates ("here are the 15 most common FOPs for beauty brands at $3M GMV"). This seeds faster onboarding and strengthens the network effect.

**Code-level guardrails on critical actions.** Even with natural language flexibility, money movement, refunds, order cancellations, and customer communication hit hard-coded validation checks. Merchants get the speed of natural language configuration and the reliability of traditional engineering on the actions that matter.

### Build sequence

**Phase 0:** Minimal FOP engine for the design partner prototype. Hardcoded FOPs in config files. Parser layer deferred. Goal: prove the concept that natural language rules drive agent behavior.

**Phase 1 (with MVP launch):** Ship the full parsing layer, UI for creating/editing/disabling FOPs, confirmation flow, and execution integration into the Order Exception Agent. This ships with the MVP — FOPs are not an add-on, they're core to how the product works from day one.

**Phase 2 (with workflow pack expansion):** Document upload and extraction. Correction-derived FOP suggestions. Conflict detection. Per-pack FOP libraries.

**Phase 3:** Cross-merchant FOP templates (anonymized). Starter FOP packs by vertical (beauty, apparel, supplements). Advanced scoping and inheritance (brand-level FOPs that child entities inherit).

-----

## Phase 0 — Foundation (Weeks 0–8)

### Goal

Validate the wedge with 5–8 design partner merchants and build the core agent harness and integration layer.

### Design partner motion

- Target profile: $2M–$20M GMV Shopify brands with existing ops pain
- 5–8 partners, free in exchange for: deep process interviews, stack access, rapid feedback loops, case study rights
- Recruit through founder network, DTC Slack communities (Chief, Operators Guild), LinkedIn outreach, and warm intros from Bay Area DTC scene
- Interview focus: map the last 30 days of exceptions, quantify time spent, identify top 5 recurring fire drills

### Technical foundations

- **Integration layer v1:** Shopify (first-class), ShipBob + ShipStation (top two 3PLs by ICP usage), Gorgias + Zendesk (top two helpdesks), Stripe, Klaviyo, Gmail/Outlook
- **Agent harness:** Claude as primary model, structured tool use, per-workflow subagent pattern, memory layer for merchant-specific context
- **Eval infrastructure:** From day one. Every workflow has a ground-truth eval set. Regression testing on every model/prompt change. This is both a product moat and a hiring/credibility signal.
- **Admin observability:** Internal panel for observing agent runs, correcting mistakes, and reviewing decisions — this becomes the merchant-facing audit log in Phase 1.

### Deliverables

- Working end-to-end prototype on at least 1 design partner
- Documented scenario library of 20+ exception types with ground-truth resolutions
- Metrics framework: time saved per workflow, exception auto-resolution rate, merchant-reported accuracy, NPS
- Tech stack locked in; first engineering hire or contractor identified

### Exit criteria

- At least 1 design partner actively relying on the agent for real work
- Eval suite covers the top 10 exception types at >85% accuracy
- Clear 6-month product plan based on design partner signal

-----

## Phase 1 — MVP: The Order Exception Agent (Months 2–6)

### Wedge product

An agent that monitors the merchant's order lifecycle and handles exceptions autonomously across systems.

**Pre-fulfillment exceptions:**

- Address validation and correction (flag invalid/ambiguous addresses, draft customer message, update order on reply)
- Fraud flag triage (pull order context, risk signals, customer history; recommend approve/hold/cancel per merchant policy)
- Payment issues (declined cards, failed captures; coordinate reauth request or cancellation flow)
- High-value order review (custom policy per merchant — "flag anything over $500 from first-time international customers")
- Inventory conflicts (item went OOS between checkout and fulfillment — decide partial ship, hold, or cancel)

**Fulfillment exceptions:**

- Split shipment decisions when items are in different 3PL warehouses
- Stuck orders (not picked in 24h, missing required data, courier label generation failures)
- Inventory mismatches between Shopify and 3PL systems

**Post-ship exceptions:**

- Stuck shipments (no tracking update in 3+ days)
- Delivery exceptions (failed delivery, return to sender, address correction needed)
- "Delivered but not received" claims with tracking + carrier confirmation workup

### Key product surfaces

- **Case console:** every exception is a card with full context, reasoning trace, recommended action, and one-click approve/modify/reject
- **Slack integration:** exceptions surface in the merchant's ops channel; approvals happen in Slack thread
- **Policy builder:** natural language rules that compile into agent constraints
- **Audit log:** every action taken, every tool called, every reasoning step (collapsible)
- **Weekly digest:** auto-generated report of what the agent handled, what needed human input, time saved

### Success metrics

- 70%+ of exceptions fully resolved autonomously (no human touch)
- 90%+ accuracy on policy-compliant decisions (measured against merchant spot-review)
- <30s median time from exception detection to action/recommendation
- 10+ hours/week saved per merchant on exception handling (self-reported + logged)
- NRR >110% from design partners converting to paid

### Pricing (MVP)

- **Starter:** $500/mo — up to 500 exceptions handled
- **Growth:** $1,500/mo — up to 2,500 exceptions handled
- **Scale:** $3,500/mo — up to 10,000 exceptions handled, Slack + API access, priority support
- Overages metered per action above tier limits

### Phase 1 exit criteria

- 15–25 paying merchants
- $15–40k MRR
- At least 3 public case studies with named customers
- Auto-resolution rate stable at 70%+ across merchants

-----

## Phase 2 — Expand Core Workflows (Months 6–12)

The shift: from a single-workflow product to a suite of workflow packs. Merchants land on exceptions, expand into adjacent packs as trust is earned.

### New workflow packs

**Returns & Exchanges Pack**

- Intake from email, chat, returns portal
- Apply return policy (eligibility windows, reason codes, condition checks)
- Generate RMA, shipping label, customer instructions
- Handle exchanges (reserve new item, create replacement order, coordinate with fulfillment)
- Coordinate refund with payment processor on return receipt
- Update inventory on restock/destroy decision
- Direct competitive positioning vs. Loop Returns: we're agentic across the full lifecycle, not a returns SaaS

**Inventory Replenishment Pack**

- Monitor sell-through velocity and stock levels across warehouses and channels
- Calculate reorder quantities using lead time + safety stock + demand forecast
- Draft POs to suppliers (email or supplier portal)
- Follow up on PO acknowledgment, track expected delivery dates
- Update storefront restock dates on product pages
- Pause ads for products that will stock out before replenishment

**Chargeback Defense Pack**

- Detect chargebacks from Stripe/payment processor webhooks
- Auto-compile evidence package: order confirmation, shipping proof, delivery confirmation, customer communication history, refund policy
- Draft response within deadline window
- Submit evidence (with merchant approval until trust is earned)
- Track win rates and iterate on evidence templates

**Reconciliation Pack**

- Daily and weekly reconciliation across Shopify orders, Stripe payouts, 3PL invoices, ad spend, subscription billing
- Margin-per-order and per-SKU calculation with anomaly detection
- Flag outliers: returns rate spike, shipping cost jump, ad spend anomaly, refund volume shifts
- Auto-generate weekly ops summary posted to Slack or emailed

### Platform improvements

- **Self-serve onboarding:** connect Shopify → auto-detect connected tools → suggest relevant workflow packs → activate with 1 click
- **Workflow marketplace v1:** merchants share anonymized templates (e.g., "Beauty brand return policy for $2M GMV stores")
- **Multi-brand support:** holding companies and aggregators manage several brands from one account
- **Webhook + API surface:** for merchants who want to build on top or integrate with custom systems

### Phase 2 exit criteria

- 50 paying merchants
- $50k+ MRR
- NRR >130% (upsell into additional packs is the core growth motion)
- Average merchant on 3+ workflow packs
- Brand recognition in DTC operator communities

-----

## Phase 3 — Platform & Customization (Months 12–18)

### Core shift

From product to platform. Phases 0–2 ship pre-built agents for specific workflows. Phase 3 lets merchants build their own.

**Custom Agent Builder**

- Merchant describes a workflow in natural language ("when a wholesale customer emails an order, pull their SKUs, check inventory, draft a quote, and notify me")
- System proposes a plan: which tools, which data, which approval gates
- Merchant reviews, tweaks, deploys
- Runs alongside pre-built agents, shares same observability and audit surface

**Advanced capabilities**

- **Multi-agent orchestration:** exception agent hands off to customer comms agent hands off to inventory agent; each with specialized context
- **Agent-to-agent protocols:** our agent can talk to supplier agents (ACP/UCP), 3PL agents, payment network agents — this is where the agentic commerce bet pays off
- **Merchant-specific memory:** long-term learning of preferences, corrections, policies that evolve without explicit rule updates
- **Voice interface:** ops leaders can ask questions and issue instructions hands-free

**Enterprise tier**

- SSO/SAML, SOC 2 Type II (kicked off mid-Phase 2), dedicated infrastructure options
- Custom integrations: NetSuite, SAP, Oracle for larger brands crossing up-market
- SLA guarantees, dedicated onboarding and CS
- Price point: custom, $50k–$250k+ ARR

### Pricing evolution

- **Starter:** $500–$2,000/mo (pre-built packs, up to X actions)
- **Growth:** $2,000–$10k/mo (all packs, custom agents, multi-brand)
- **Enterprise:** custom with SLA, dedicated support, specialized integrations

### Phase 3 exit criteria

- 200+ merchants
- $500k+ MRR
- 20%+ of merchants building or modifying custom agents
- First 3–5 enterprise accounts landed and live
- SOC 2 Type II completed

-----

## Phase 4 — Ecosystem & Scale (Months 18–24+)

### Agent Marketplace

- Third parties (agencies, consultants, vertical experts) publish agent templates
- Revenue share model with publishers
- Vertical-specific packs: beauty, apparel, supplements, home goods, food & bev
- Becomes a discovery and expansion engine — merchants find what they need, publishers build distribution

### Platform integrations and distribution

- Shopify App Store presence (launched earlier, now featured)
- BigCommerce, WooCommerce, Magento expansion
- Native integrations with major ecommerce infrastructure (Klaviyo, Triple Whale, Shogun, Recharge)
- Embedded agent SDK so Gorgias, Zendesk, and others can run our agents inside their products

### Agentic commerce layer

- Positions us as the merchant-side agent in emerging agentic commerce protocols (ACP, UCP, AP2)
- Handles agent-initiated purchases, B2B procurement, supplier negotiations
- Our agent represents the merchant when other agents (Perplexity Buy, ChatGPT Shopping, Operator) come to transact
- This is a large outside-shot bet — if agentic commerce takes off, being the merchant-side operational agent is an enormous position

### Phase 4 exit criteria

- 1,000+ merchants
- $5M+ ARR
- 30%+ of revenue from ecosystem (marketplace, embedded, enterprise)
- Recognized category leader in "ecommerce operations AI"

-----

## Core Technical Architecture

**Evolves across phases but directionally:**

- **Agent core:** Claude as primary, with GPT-5 as fallback for specific tasks where it outperforms. Structured tool use, interleaved thinking for complex decisions, extensive eval coverage before any prompt or model change ships.
- **Integration layer:** Hybrid of native API integrations (where APIs are first-class — Shopify, Stripe) and browser automation (for legacy portals — many 3PLs, some suppliers). This is directly the Duvo pattern and is table stakes.
- **Policy engine:** Natural language policies are parsed into structured constraints that limit tool use permissions per workflow. Policies are versioned and evaluated against historical cases before being deployed.
- **Eval infrastructure:** Every workflow has a ground-truth eval set. Regression testing on every model/prompt change. Agent proposes new evals based on novel cases; humans review and accept. This becomes a hiring signal and a customer-facing trust artifact ("our chargeback agent has 94% accuracy on 1,200 ground-truth cases").
- **Memory:** Per-merchant knowledge base of policies, preferences, learned corrections. Per-workflow episodic memory for context on long-running cases.
- **Observability:** Full trace of every agent run with replay capability. Merchant-facing audit log with collapsible reasoning. Internal admin for debugging.
- **Security and compliance:** SOC 2 Type I by end of Phase 1, Type II by mid Phase 2, ISO 27001 on the Phase 3 roadmap. Every write action requires explicit tool-level permission.

-----

## Summary Metrics

|Phase|Timeline|Merchants          |MRR     |Anchor Metric              |
|-----|--------|-------------------|--------|---------------------------|
|0    |W0–8    |5–8 design partners|$0      |Prototype live on 1 partner|
|1    |M2–6    |15–25 paying       |$15–40k |70%+ auto-resolution       |
|2    |M6–12   |50 paying          |$50k+   |3+ packs / merchant        |
|3    |M12–18  |200+               |$500k+  |20% custom agents          |
|4    |M18–24+ |1,000+             |$5M+ ARR|30%+ revenue from ecosystem|

-----

## Risks & Open Questions

1. **Platform risk — Shopify builds this.** Their Sidekick product is adjacent. Mitigation: go broader than Shopify-native; become indispensable via deep cross-system workflows Shopify won't prioritize because it dilutes their platform story.
1. **Incumbent expansion risk.** Gorgias or Klaviyo could expand from their verticals into operations. Mitigation: operations is a fundamentally different product surface; they're not architected for it, and attempting to retrofit would cannibalize their core.
1. **Duvo moving downmarket.** They've raised enough capital to fund this. Mitigation: speed, Shopify-native focus, and SMB-appropriate pricing. Duvo's SAP-first DNA makes downmarket expansion awkward.
1. **Agent reliability in production.** Early bad calls destroy trust fast. Mitigation: aggressive human-in-the-loop on day one; unlock autonomy per-workflow as accuracy crosses thresholds; design the correction UX to be better than the fail-state UX.
1. **Integration maintenance burden.** 10+ integrations means constant breakage as APIs change. Mitigation: invest in integration monitoring and regression tests early; prefer MCP and standardized protocols where available; hire integration eng as second or third hire.
1. **Pricing model.** Flat vs. usage-based affects margins and growth shape differently. Open question: revisit after Phase 1 data on action volume distribution across merchants.
1. **Who actually buys this?** Is it the founder, the head of ops, or the CX lead? Initial hypothesis is founder at <$10M GMV, head of ops at $10M+. Validate in Phase 0 interviews.

-----

## Immediate Next Actions (Next 2 Weeks)

1. **Design partner outreach.** Goal: 8 committed merchant partners by end of week 2. Leverage DTC operator communities, warm intros, LinkedIn.
1. **Technical spike.** Build the integration scaffolding for Shopify + ShipBob + Gorgias + Stripe. Goal: end-to-end write action on a test merchant account by end of week 2.
1. **Wedge agent v0.** Build the order exception agent with the top 5 exception types. Evaluate against 50-case benchmark.
1. **First hire.** Integration-focused engineer or experienced contractor to own the integration layer.
1. **Brand + landing.** Register domain, build waitlist page, write the one-pager for design partner conversations.

-----

*This is a living document. Iterate on it monthly. Nothing here is load-bearing except the design principles and the wedge.*