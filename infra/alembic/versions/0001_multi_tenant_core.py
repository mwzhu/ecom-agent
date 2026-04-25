from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_multi_tenant_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "0001_multi_tenant_core.sql"
    for statement in sql_path.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("drop table if exists tool_calls cascade")
    op.execute("drop table if exists webhook_events cascade")
    op.execute("drop table if exists fops cascade")
    op.execute("drop table if exists case_events cascade")
    op.execute("drop table if exists cases cascade")
    op.execute("drop table if exists integration_credentials cascade")
    op.execute("drop table if exists merchants cascade")
