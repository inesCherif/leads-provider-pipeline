# migrations/

SQL migration files applied to the Supabase PostgreSQL database.

Files are numbered sequentially: `001_`, `002_`, etc.
Each file is idempotent (safe to re-run) and uses `IF NOT EXISTS` guards.

| File | Description |
|------|-------------|
| `001_initial_schema.sql` | Core schema: schemas, extensions, all staging/audit tables |
| `002`–`017` | Incremental additions: source metadata, SIRENE columns, qualification, views, department derivation, dedup marking, SIREN recovery, multi-entity flag, source status, enrichment queue, dirigeants, RLS policies, invalid-email exclusion, legal-form labels, liquidation flag |
| `018_multi_sector_contact_points.sql` | M4: `company_sources` (sector = membership), `contact_points` (multi-witness claims with provenance), `company_identifiers`, `company_attributes`, small columns, missing FK on `raw.ingest_rows`, RLS. Additive, replayable |
| `019_sector_in_views.sql` | M4: `sectors TEXT[]` + `primary_sector` appended to `v_qualified_contacts`, `v_deliverable_businesses`, `v_enrichment_queue`; new `v_sector_summary`. CREATE OR REPLACE, columns appended last |
