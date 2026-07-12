# migrations/

SQL migration files applied to the Supabase PostgreSQL database.

Files are numbered sequentially: `001_`, `002_`, etc.
Each file is idempotent (safe to re-run) and uses `IF NOT EXISTS` guards.

| File | Description |
|------|-------------|
| `001_initial_schema.sql` | Core schema: schemas, extensions, all staging/audit tables |
