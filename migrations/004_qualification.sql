-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 004 — Qualification infrastructure for M1-S5
-- ─────────────────────────────────────────────────────────────────────────────
-- Adds the columns and constraints the qualification pass needs. Additive only:
-- no existing data is modified, no column is dropped. All 128,116 companies keep
-- qualification_status = 'unqualified' until scripts/m1_s5_qualify.py runs.
--
-- Adds:
--   qualification_status      : new allowed value 'qualified_unverified'
--   qualified_at              TIMESTAMPTZ : NULL = not yet qualified (idempotency anchor,
--                                           mirrors sirene_last_checked_at from 003)
--   qualification_rule_version TEXT       : which rule version produced the verdict,
--                                           so a rule change can trigger re-qualification
--   disqualification_reason   : controlled vocabulary (was unconstrained TEXT)
--
-- Why 'qualified_unverified': 83,344 companies (65%) have no SIREN and so can
-- never carry an official NAF code, but 83,326 of them have a contact and 19,443
-- have an email. Routing them to 'pending' would shelve most of the reachable
-- dataset. They are qualified on the source label alone and kept in a separate,
-- lower-confidence tier so downstream consumers can tell the two apart.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
BEGIN
    -- ── Widen qualification_status vocabulary ────────────────────────────────
    -- Drop-and-recreate is required: Postgres has no ALTER CONSTRAINT for CHECK.
    -- Safe because every existing row is 'unqualified', which stays valid.
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid = 'staging.companies'::regclass
                 AND conname  = 'companies_qualification_status_check') THEN
        ALTER TABLE staging.companies DROP CONSTRAINT companies_qualification_status_check;
    END IF;

    ALTER TABLE staging.companies ADD CONSTRAINT companies_qualification_status_check
        CHECK (qualification_status IN (
            'unqualified',           -- not yet processed by M1-S5
            'qualified',             -- tier 1: official INSEE NAF code in scope
            'qualified_unverified',  -- tier 2: no SIREN, qualified on source label only
            'disqualified',          -- rule says no, see disqualification_reason
            'pending'                -- cannot decide yet, see disqualification_reason
        ));

    -- ── Checkpoint column ────────────────────────────────────────────────────
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='staging' AND table_name='companies'
                     AND column_name='qualified_at') THEN
        ALTER TABLE staging.companies ADD COLUMN qualified_at TIMESTAMPTZ;
        COMMENT ON COLUMN staging.companies.qualified_at IS
            'Timestamp of last qualification pass. NULL = never qualified.';
    END IF;

    -- ── Rule traceability ────────────────────────────────────────────────────
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='staging' AND table_name='companies'
                     AND column_name='qualification_rule_version') THEN
        ALTER TABLE staging.companies ADD COLUMN qualification_rule_version TEXT;
        COMMENT ON COLUMN staging.companies.qualification_rule_version IS
            'RULE_VERSION from config/sector_rules.py that produced this verdict. '
            'Rows whose version differs from the current one are re-qualified on the next run.';
    END IF;

    -- ── Controlled reason vocabulary ─────────────────────────────────────────
    -- Covers both 'disqualified' and 'pending' outcomes: the column explains any
    -- non-qualified verdict, not only rejections. NULL for qualified rows.
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid = 'staging.companies'::regclass
                 AND conname  = 'companies_disqualification_reason_check') THEN
        ALTER TABLE staging.companies DROP CONSTRAINT companies_disqualification_reason_check;
    END IF;

    ALTER TABLE staging.companies ADD CONSTRAINT companies_disqualification_reason_check
        CHECK (disqualification_reason IS NULL OR disqualification_reason IN (
            -- disqualified
            'public_administration',  -- NAF 84.11Z: communes/town halls, never a prospect
            'naf_out_of_scope',       -- has an official NAF code, not in target sectors
            'label_out_of_scope',     -- no SIREN, and source label is an excluded activity
            'business_closed',        -- sirene_etat = 'F'
            -- pending
            'sirene_not_found',       -- has SIREN but SIRENE lookup returned nothing
            'no_siren_no_label'       -- no SIREN and no usable source label
        ));
END
$$;

-- ── Partial index for fast "needs (re-)qualification" lookups ────────────────
-- Mirrors idx_companies_not_enriched from 003.
CREATE INDEX IF NOT EXISTS idx_companies_not_qualified
    ON staging.companies(id)
    WHERE qualified_at IS NULL;

-- Supports the tier-2 label matching, which scans no-SIREN rows by naf_label.
CREATE INDEX IF NOT EXISTS idx_companies_no_siren_label
    ON staging.companies(naf_label)
    WHERE siren IS NULL;

-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT conname, pg_get_constraintdef(oid) AS def
FROM pg_constraint
WHERE conrelid = 'staging.companies'::regclass AND contype = 'c'
ORDER BY conname;
