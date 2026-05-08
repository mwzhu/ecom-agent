from __future__ import annotations

from alembic import op

revision = "0007_production_automation_core"
down_revision = "0006_agent_run_executions"
branch_labels = None
depends_on = None


TENANT_TABLES = (
    "audit_events",
    "webhook_registry",
    "normalized_events",
    "scheduled_monitor_cursors",
    "automation_policies",
    "approval_requests",
    "message_templates",
    "generated_message_audits",
)


def upgrade() -> None:
    op.execute(
        """
        alter table integration_credentials
          add column if not exists status text not null default 'active',
          add column if not exists provider_account_id text,
          add column if not exists granted_scopes jsonb not null default '[]'::jsonb,
          add column if not exists last_health_status text not null default 'unknown',
          add column if not exists last_health_error jsonb,
          add column if not exists last_health_checked_at timestamptz,
          add column if not exists disconnected_at timestamptz
        """
    )
    op.execute(
        """
        alter table cases
          drop constraint if exists ck_cases_status,
          add constraint ck_cases_status check (
            status in (
              'open',
              'pending_approval',
              'executing',
              'partially_executed',
              'needs_human_recovery',
              'resolved',
              'failed',
              'canceled'
            )
          )
        """
    )
    op.execute(
        """
        create table if not exists audit_events (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          actor_type text not null,
          actor_id text,
          action text not null,
          provider text,
          case_id uuid references cases(id) on delete set null,
          payload jsonb not null default '{}'::jsonb,
          created_at timestamptz not null default now()
        )
        """
    )
    op.execute(
        """
        create table if not exists webhook_registry (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          provider text not null,
          topic text not null,
          external_webhook_id text,
          signing_secret_ref text,
          previous_signing_secret_ref text,
          status text not null default 'pending',
          callback_url text not null,
          last_verified_at timestamptz,
          secret_rotated_at timestamptz,
          previous_secret_expires_at timestamptz,
          created_at timestamptz not null default now(),
          constraint uq_webhook_registry_merchant_provider_topic
            unique (merchant_id, provider, topic)
        )
        """
    )
    op.execute(
        """
        create table if not exists normalized_events (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          source_type text not null,
          provider text,
          source_event_id text not null,
          event_type text not null,
          payload jsonb not null default '{}'::jsonb,
          dedupe_key text not null,
          observed_at timestamptz not null default now(),
          processed_at timestamptz,
          case_id uuid references cases(id) on delete set null,
          created_at timestamptz not null default now(),
          constraint uq_normalized_events_dedupe unique (merchant_id, dedupe_key)
        )
        """
    )
    op.execute(
        """
        create table if not exists scheduled_monitor_cursors (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          provider text not null,
          monitor_type text not null,
          cursor jsonb not null default '{}'::jsonb,
          cadence_seconds integer not null default 3600,
          next_run_at timestamptz not null default now(),
          last_run_at timestamptz,
          backoff_until timestamptz,
          rate_limit_state jsonb not null default '{}'::jsonb,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_scheduled_monitor_cursors_monitor
            unique (merchant_id, provider, monitor_type)
        )
        """
    )
    op.execute(
        """
        create table if not exists automation_policies (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          exception_type text not null,
          tool text not null default '*',
          approval_mode text not null default 'approval_required',
          customer_message_mode text not null default 'draft_only',
          thresholds jsonb not null default '{}'::jsonb,
          limits jsonb not null default '{}'::jsonb,
          enabled boolean not null default true,
          created_by text not null default 'system',
          updated_by text not null default 'system',
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_automation_policies_scope unique (merchant_id, exception_type, tool)
        )
        """
    )
    op.execute(
        """
        create table if not exists approval_requests (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          case_id uuid not null references cases(id) on delete cascade,
          assigned_group text not null,
          primary_approver text,
          escalation_group text,
          requested_action jsonb not null default '{}'::jsonb,
          due_at timestamptz not null,
          escalated_at timestamptz,
          expired_at timestamptz,
          status text not null default 'pending',
          timeout_behavior text not null default 'no_auto_approve',
          created_at timestamptz not null default now()
        )
        """
    )
    op.execute(
        """
        create table if not exists message_templates (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          template_key text not null,
          body text not null,
          channel text not null default 'email',
          brand_voice jsonb not null default '{}'::jsonb,
          enabled boolean not null default true,
          created_at timestamptz not null default now(),
          constraint uq_message_templates_key unique (merchant_id, template_key)
        )
        """
    )
    op.execute(
        """
        create table if not exists generated_message_audits (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          case_id uuid not null references cases(id) on delete cascade,
          provider text,
          channel text not null default 'email',
          body text not null,
          policy_decision jsonb not null default '{}'::jsonb,
          sent_at timestamptz,
          created_at timestamptz not null default now()
        )
        """
    )
    _create_indexes()
    _enable_rls()


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f"drop table if exists {table} cascade")
    op.execute(
        """
        alter table integration_credentials
          drop column if exists status,
          drop column if exists provider_account_id,
          drop column if exists granted_scopes,
          drop column if exists last_health_status,
          drop column if exists last_health_error,
          drop column if exists last_health_checked_at,
          drop column if exists disconnected_at
        """
    )
    op.execute("alter table cases drop constraint if exists ck_cases_status")


def _create_indexes() -> None:
    statements = (
        "create index if not exists ix_audit_events_merchant_created on audit_events (merchant_id, created_at desc)",
        "create index if not exists ix_audit_events_actor_created on audit_events (actor_type, created_at desc)",
        "create index if not exists ix_webhook_registry_provider_status on webhook_registry (provider, status)",
        "create index if not exists ix_webhook_registry_merchant_provider on webhook_registry (merchant_id, provider)",
        "create index if not exists ix_normalized_events_merchant_processed on normalized_events (merchant_id, processed_at, observed_at desc)",
        "create index if not exists ix_normalized_events_source_provider on normalized_events (source_type, provider)",
        "create index if not exists ix_scheduled_monitor_cursors_next_run on scheduled_monitor_cursors (next_run_at)",
        "create index if not exists ix_automation_policies_merchant_exception on automation_policies (merchant_id, exception_type)",
        "create index if not exists ix_approval_requests_merchant_status_due on approval_requests (merchant_id, status, due_at)",
        "create index if not exists ix_approval_requests_case_created on approval_requests (case_id, created_at desc)",
        "create index if not exists ix_generated_message_audits_case_created on generated_message_audits (case_id, created_at desc)",
    )
    for statement in statements:
        op.execute(statement)


def _enable_rls() -> None:
    for table in TENANT_TABLES:
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(f"drop policy if exists tenant_isolation_{table} on {table}")
        op.execute(
            f"""
            create policy tenant_isolation_{table} on {table}
              using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
              with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
            """
        )
