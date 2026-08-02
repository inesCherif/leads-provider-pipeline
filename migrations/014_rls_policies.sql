-- 014_rls_policies.sql
-- Applies the RLS policies migration 001 silently failed to create.
--
-- 001 used `CREATE POLICY IF NOT EXISTS`, which is NOT valid PostgreSQL —
-- Postgres supports IF NOT EXISTS on many object types, but not on policies.
-- The statement errored, so the database ended up with RLS *enabled* on seven
-- tables and *zero* policies on any of them.
--
-- Nothing was broken in practice: every script connects as service_role, which
-- bypasses RLS entirely. The real damage was to replayability — 001 could not
-- be run against a fresh database without erroring, which quietly broke the
-- property the numbered-migration scheme depends on ("replay 001..N to rebuild").
--
-- 001 has now been corrected in place so a fresh database gets this right, and
-- this migration brings the EXISTING database to the same state. Both are
-- needed: editing 001 alone would leave production without policies.
--
-- Idempotent the way Postgres actually supports: consult pg_policies first.
-- Safe to re-run.

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('staging','source_files'), ('staging','companies'), ('staging','sites'),
        ('staging','contacts'),     ('staging','emails'),
        ('raw','ingest_rows'),      ('audit','audit_log')
    ) AS t(sch, tbl)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = r.sch AND tablename = r.tbl
              AND policyname = 'service_role_all'
        ) THEN
            EXECUTE format(
                'CREATE POLICY "service_role_all" ON %I.%I FOR ALL USING (true)',
                r.sch, r.tbl);
        END IF;
    END LOOP;
END;
$$;

-- Verification (7 rows expected):
--   SELECT schemaname, tablename, policyname FROM pg_policies
--   WHERE schemaname IN ('staging','raw','audit') ORDER BY 1,2;
--
-- NOTE: these policies are deliberately permissive (USING (true)). They exist so
-- the schema is replayable and so a non-service-role connection is not silently
-- blind. They are NOT an access-control design — that work belongs with whoever
-- defines the first non-service-role consumer.
