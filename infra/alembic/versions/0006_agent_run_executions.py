from __future__ import annotations

from alembic import op

revision = "0006_agent_run_executions"
down_revision = "0005_eval_review_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists agent_run_executions (
          id uuid primary key default gen_random_uuid(),
          case_id uuid not null references cases(id) on delete cascade,
          merchant_id uuid not null references merchants(id) on delete cascade,
          thread_id text not null,
          run_id text not null,
          graph_status text,
          status text not null default 'processing',
          graph_state jsonb not null default '{}'::jsonb,
          execution_results jsonb not null default '[]'::jsonb,
          error jsonb,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_agent_run_executions_run unique (run_id)
        )
        """
    )
    op.execute(
        """
        create index if not exists ix_agent_run_executions_merchant_status_created
          on agent_run_executions (merchant_id, status, created_at desc)
        """
    )
    op.execute(
        """
        create index if not exists ix_agent_run_executions_case_created
          on agent_run_executions (case_id, created_at desc)
        """
    )
    op.execute("alter table agent_run_executions enable row level security")
    op.execute("alter table agent_run_executions force row level security")
    op.execute(
        "drop policy if exists tenant_isolation_agent_run_executions on agent_run_executions"
    )
    op.execute(
        """
        create policy tenant_isolation_agent_run_executions on agent_run_executions
          using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
          with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists agent_run_executions cascade")
