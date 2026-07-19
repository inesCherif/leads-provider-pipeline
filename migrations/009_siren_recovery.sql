-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 009 — SIREN recovery tracking (M1-S4b)
-- ─────────────────────────────────────────────────────────────────────────────
-- Additive only. Adds the columns that make the recovery run resumable, auditable
-- and REVERSIBLE.
--
-- Reversibility is the point of siren_recovered_at: the run writes SIREN onto
-- rows that never had one, which is the most consequential write in the project.
-- Undo the entire run, and nothing else, with:
--
--   UPDATE staging.companies
--      SET siren = NULL, siren_recovered_at = NULL, siren_recovery_method = NULL
--    WHERE siren_recovered_at IS NOT NULL;
--
-- Without the marker there would be no way to tell a recovered SIREN from one
-- that came from the source files, and the run could not be undone.
--
-- COLLISION HANDLING: staging.companies.siren is UNIQUE (001:87). If a recovered
-- SIREN already belongs to another company, that is not an error to work around -
-- it is proof the two rows are the same business. The script marks the row as a
-- duplicate of the existing SIREN holder instead of writing the SIREN, using
-- dedup_method = 'siren_recovery_collision'. Recovery doubles as deduplication.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='staging' AND table_name='companies'
                     AND column_name='siren_recovery_checked_at') THEN
        ALTER TABLE staging.companies ADD COLUMN siren_recovery_checked_at TIMESTAMPTZ;
        COMMENT ON COLUMN staging.companies.siren_recovery_checked_at IS
            'Timestamp of last SIREN recovery attempt, successful or not. NULL = never '
            'attempted. Resume anchor: a killed run picks up from here.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='staging' AND table_name='companies'
                     AND column_name='siren_recovered_at') THEN
        ALTER TABLE staging.companies ADD COLUMN siren_recovered_at TIMESTAMPTZ;
        COMMENT ON COLUMN staging.companies.siren_recovered_at IS
            'Set when SIREN was recovered by matching name + postal against the registry, '
            'rather than coming from the source files. Makes the run reversible and marks '
            'these SIRENs as inferred, not provided.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='staging' AND table_name='companies'
                     AND column_name='siren_recovery_method') THEN
        ALTER TABLE staging.companies ADD COLUMN siren_recovery_method TEXT;
        COMMENT ON COLUMN staging.companies.siren_recovery_method IS
            'Which matching rule recovered the SIREN, e.g. confident_name_postal. '
            'Lets a weaker rule be re-evaluated later without redoing confident ones.';
    END IF;
END
$$;

-- Resume support: find the not-yet-attempted rows fast.
CREATE INDEX IF NOT EXISTS idx_companies_siren_recovery_pending
    ON staging.companies(id)
    WHERE siren IS NULL AND siren_recovery_checked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_companies_siren_recovered
    ON staging.companies(siren_recovered_at)
    WHERE siren_recovered_at IS NOT NULL;
