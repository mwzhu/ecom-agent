from __future__ import annotations

from alembic import op

revision = "0003_webhook_sources"
down_revision = "0002_eval_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists webhook_sources (
          id uuid primary key default gen_random_uuid(),
          merchant_id uuid not null references merchants(id) on delete cascade,
          provider text not null,
          external_account_id text not null,
          created_at timestamptz not null default now(),
          constraint uq_webhook_sources_provider_external
            unique (provider, external_account_id)
        )
        """
    )
    op.execute(
        """
        create index if not exists ix_webhook_sources_merchant_provider
          on webhook_sources (merchant_id, provider)
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists webhook_sources cascade")
