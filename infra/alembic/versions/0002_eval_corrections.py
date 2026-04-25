from __future__ import annotations

from alembic import op

revision = "0002_eval_corrections"
down_revision = "0001_multi_tenant_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists eval_corrections (
          id uuid primary key default gen_random_uuid(),
          case_id uuid not null references cases(id) on delete cascade,
          merchant_id uuid not null references merchants(id) on delete cascade,
          expected_resolution jsonb not null default '{}'::jsonb,
          notes text not null default '',
          created_by text not null,
          status text not null default 'queued',
          created_at timestamptz not null default now()
        )
        """
    )
    op.execute(
        """
        create index if not exists ix_eval_corrections_merchant_created
          on eval_corrections (merchant_id, created_at desc)
        """
    )
    op.execute("alter table eval_corrections enable row level security")
    op.execute("alter table eval_corrections force row level security")
    op.execute("drop policy if exists tenant_isolation_eval_corrections on eval_corrections")
    op.execute(
        """
        create policy tenant_isolation_eval_corrections on eval_corrections
          using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
          with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists eval_corrections cascade")
