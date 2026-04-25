from __future__ import annotations

from alembic import op

revision = "0004_webhook_sources_comment"
down_revision = "0003_webhook_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        comment on table webhook_sources is
          'Provider external account id to merchant mapping used before tenant scope is known. '
          'This table intentionally does not use tenant RLS; access should remain limited to '
          'webhook merchant resolution and credential install paths.'
        """
    )


def downgrade() -> None:
    op.execute("comment on table webhook_sources is null")
