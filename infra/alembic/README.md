# Alembic

Run migrations with Alembic:

```bash
pnpm db:migrate
```

The initial migration creates the multi-tenant core tables and row-level security policies. The SQL body lives in `infra/alembic/sql/0001_multi_tenant_core.sql` so it can be reviewed directly while still being ordered and applied by Alembic.
