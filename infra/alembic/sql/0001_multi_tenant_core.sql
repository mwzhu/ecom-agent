-- Phase 0 W1-2: multi-tenant core data model.
-- Apply with: psql "$DATABASE_URL" -f infra/alembic/versions/0001_multi_tenant_core.sql

create extension if not exists pgcrypto;

create table if not exists merchants (
  id uuid primary key default gen_random_uuid(),
  clerk_org_id text not null unique,
  name text not null,
  tier text not null default 'internal',
  created_at timestamptz not null default now()
);

create table if not exists integration_credentials (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references merchants(id) on delete cascade,
  provider text not null,
  encrypted_token text not null,
  encrypted_refresh text,
  expires_at timestamptz,
  kms_key_id text not null,
  created_at timestamptz not null default now(),
  constraint uq_integration_credentials_provider unique (merchant_id, provider)
);

create table if not exists cases (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references merchants(id) on delete cascade,
  type text not null,
  status text not null default 'open',
  subject_ref jsonb not null default '{}'::jsonb,
  langgraph_thread_id text,
  created_at timestamptz not null default now(),
  resolved_at timestamptz,
  resolution jsonb
);

create table if not exists case_events (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references cases(id) on delete cascade,
  merchant_id uuid not null references merchants(id) on delete cascade,
  kind text not null,
  payload jsonb not null default '{}'::jsonb,
  langsmith_run_id text,
  actor text not null,
  created_at timestamptz not null default now(),
  constraint ck_case_events_actor check (actor in ('agent', 'human', 'webhook', 'system'))
);

create table if not exists fops (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references merchants(id) on delete cascade,
  version integer not null default 1,
  nl_text text not null,
  structured jsonb not null default '{}'::jsonb,
  status text not null default 'draft',
  parent_id uuid references fops(id) on delete set null,
  created_by text not null,
  created_at timestamptz not null default now()
);

create table if not exists webhook_events (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  event_id text not null,
  merchant_id uuid not null references merchants(id) on delete cascade,
  payload jsonb not null default '{}'::jsonb,
  processed_at timestamptz,
  created_at timestamptz not null default now(),
  constraint uq_webhook_events_provider_event unique (provider, event_id)
);

create table if not exists tool_calls (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references cases(id) on delete cascade,
  merchant_id uuid not null references merchants(id) on delete cascade,
  tool text not null,
  input jsonb not null default '{}'::jsonb,
  output jsonb,
  idempotency_key text not null,
  status text not null default 'pending',
  created_at timestamptz not null default now(),
  constraint uq_tool_calls_idempotency unique (merchant_id, idempotency_key)
);

create table if not exists eval_corrections (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references cases(id) on delete cascade,
  merchant_id uuid not null references merchants(id) on delete cascade,
  expected_resolution jsonb not null default '{}'::jsonb,
  notes text not null default '',
  created_by text not null,
  status text not null default 'queued',
  created_at timestamptz not null default now()
);

create table if not exists eval_review_items (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references cases(id) on delete cascade,
  merchant_id uuid not null references merchants(id) on delete cascade,
  langsmith_run_id text,
  score integer not null,
  passed boolean not null default false,
  reason text not null,
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'queued',
  created_at timestamptz not null default now(),
  constraint uq_eval_review_items_run unique (merchant_id, case_id, langsmith_run_id)
);

create index if not exists ix_merchants_clerk_org_id on merchants (clerk_org_id);
create index if not exists ix_integration_credentials_merchant_provider
  on integration_credentials (merchant_id, provider);
create index if not exists ix_cases_merchant_status_created
  on cases (merchant_id, status, created_at desc);
create index if not exists ix_case_events_case_created
  on case_events (case_id, created_at);
create index if not exists ix_fops_merchant_status_created
  on fops (merchant_id, status, created_at desc);
create index if not exists ix_webhook_events_merchant_processed
  on webhook_events (merchant_id, processed_at);
create index if not exists ix_tool_calls_case_created
  on tool_calls (case_id, created_at);
create index if not exists ix_eval_corrections_merchant_created
  on eval_corrections (merchant_id, created_at desc);
create index if not exists ix_eval_review_items_merchant_status_created
  on eval_review_items (merchant_id, status, created_at desc);

alter table integration_credentials enable row level security;
alter table cases enable row level security;
alter table case_events enable row level security;
alter table fops enable row level security;
alter table webhook_events enable row level security;
alter table tool_calls enable row level security;
alter table eval_corrections enable row level security;
alter table eval_review_items enable row level security;

alter table integration_credentials force row level security;
alter table cases force row level security;
alter table case_events force row level security;
alter table fops force row level security;
alter table webhook_events force row level security;
alter table tool_calls force row level security;
alter table eval_corrections force row level security;
alter table eval_review_items force row level security;

drop policy if exists tenant_isolation_integration_credentials on integration_credentials;
create policy tenant_isolation_integration_credentials on integration_credentials
  using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
  with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid);

drop policy if exists tenant_isolation_cases on cases;
create policy tenant_isolation_cases on cases
  using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
  with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid);

drop policy if exists tenant_isolation_case_events on case_events;
create policy tenant_isolation_case_events on case_events
  using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
  with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid);

drop policy if exists tenant_isolation_fops on fops;
create policy tenant_isolation_fops on fops
  using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
  with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid);

drop policy if exists tenant_isolation_webhook_events on webhook_events;
create policy tenant_isolation_webhook_events on webhook_events
  using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
  with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid);

drop policy if exists tenant_isolation_tool_calls on tool_calls;
create policy tenant_isolation_tool_calls on tool_calls
  using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
  with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid);

drop policy if exists tenant_isolation_eval_corrections on eval_corrections;
create policy tenant_isolation_eval_corrections on eval_corrections
  using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
  with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid);

drop policy if exists tenant_isolation_eval_review_items on eval_review_items;
create policy tenant_isolation_eval_review_items on eval_review_items
  using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
  with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid);
