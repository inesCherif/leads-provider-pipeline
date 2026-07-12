-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 003 — Add SIRENE enrichment tracking columns to staging.companies
-- ─────────────────────────────────────────────────────────────────────────────
-- Adds:
--   sirene_last_checked_at  TIMESTAMPTZ : NULL = not yet enriched (idempotency anchor)
--   sirene_etat             CHAR(1)     : 'A' = active, 'F' = fermé (closed)
--   creation_date           DATE        : company creation date from INSEE
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='staging' AND table_name='companies' AND column_name='sirene_last_checked_at') THEN
        ALTER TABLE staging.companies ADD COLUMN sirene_last_checked_at TIMESTAMPTZ;
        COMMENT ON COLUMN staging.companies.sirene_last_checked_at IS 'Timestamp of last successful SIRENE API lookup. NULL = not yet enriched.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='staging' AND table_name='companies' AND column_name='sirene_etat') THEN
        ALTER TABLE staging.companies ADD COLUMN sirene_etat CHAR(1) CHECK (sirene_etat IN ('A','F'));
        COMMENT ON COLUMN staging.companies.sirene_etat IS 'INSEE administrative state: A=active, F=fermé (closed).';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='staging' AND table_name='companies' AND column_name='creation_date') THEN
        ALTER TABLE staging.companies ADD COLUMN creation_date DATE;
        COMMENT ON COLUMN staging.companies.creation_date IS 'Legal entity creation date from SIRENE.';
    END IF;
END
$$;

-- Partial index for fast "not yet enriched" lookups
CREATE INDEX IF NOT EXISTS idx_companies_not_enriched
    ON staging.companies(siren)
    WHERE siren IS NOT NULL AND sirene_last_checked_at IS NULL;

-- Verify
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'staging' AND table_name = 'companies'
ORDER BY ordinal_position;
