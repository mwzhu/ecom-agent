from __future__ import annotations

from alembic import op

revision = "0005_eval_review_queue"
down_revision = "0004_webhook_sources_comment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
          constraint uq_eval_review_items_run
            unique (merchant_id, case_id, langsmith_run_id)
        )
        """
    )
    op.execute(
        """
        create index if not exists ix_eval_review_items_merchant_status_created
          on eval_review_items (merchant_id, status, created_at desc)
        """
    )
    op.execute("alter table eval_review_items enable row level security")
    op.execute("alter table eval_review_items force row level security")
    op.execute("drop policy if exists tenant_isolation_eval_review_items on eval_review_items")
    op.execute(
        """
        create policy tenant_isolation_eval_review_items on eval_review_items
          using (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
          with check (merchant_id = nullif(current_setting('app.merchant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists eval_review_items cascade")
