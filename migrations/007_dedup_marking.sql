-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 007 — Duplicate marking infrastructure
-- ─────────────────────────────────────────────────────────────────────────────
-- Additive only. No row is deleted, no company is merged destructively.
--
-- APPROACH: duplicates are MARKED, not removed. A duplicate row keeps all its
-- data and gains a pointer to the survivor it belongs with. Deduplication then
-- happens at the view layer via coalesce(duplicate_of_company_id, id).
--
-- Why marking rather than deleting/merging:
--   - Fully reversible: UPDATE ... SET duplicate_of_company_id = NULL undoes it.
--   - No data loss. 44 duplicate groups have an email on the tier-2 record that
--     the tier-1 survivor does NOT have. A naive "hide the duplicate" would throw
--     those away; resolving identity in the view keeps the contact while still
--     counting the business once.
--   - Matches the project convention of preferring reversible operations.
--
-- BACKGROUND (see docs 5.2): 4,365 duplicates measured, 2,990 groups spanning
-- both qualification tiers - the same farm present once with a SIRET (tier 1) and
-- once without (tier 2). M1-S3 deduplicated on SIRET only, so they never merged.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='staging' AND table_name='companies'
                     AND column_name='duplicate_of_company_id') THEN
        ALTER TABLE staging.companies
            ADD COLUMN duplicate_of_company_id UUID REFERENCES staging.companies(id);
        COMMENT ON COLUMN staging.companies.duplicate_of_company_id IS
            'If set, this row is a duplicate of the referenced company. The row is '
            'kept intact; deduplication happens in the views via '
            'coalesce(duplicate_of_company_id, id). NULL = this row is a survivor.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='staging' AND table_name='companies'
                     AND column_name='dedup_method') THEN
        ALTER TABLE staging.companies ADD COLUMN dedup_method TEXT;
        COMMENT ON COLUMN staging.companies.dedup_method IS
            'How the duplicate link was established, e.g. exact_name_postal. '
            'Lets a weaker matching method be re-evaluated later without redoing '
            'the confident ones.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='staging' AND table_name='companies'
                     AND column_name='dedup_checked_at') THEN
        ALTER TABLE staging.companies ADD COLUMN dedup_checked_at TIMESTAMPTZ;
        COMMENT ON COLUMN staging.companies.dedup_checked_at IS
            'Timestamp of last dedup pass. NULL = never checked (idempotency anchor).';
    END IF;
END
$$;

-- A company cannot be its own duplicate.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='staging.companies'::regclass
                     AND conname='companies_not_self_duplicate') THEN
        ALTER TABLE staging.companies ADD CONSTRAINT companies_not_self_duplicate
            CHECK (duplicate_of_company_id IS NULL OR duplicate_of_company_id <> id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_companies_duplicate_of
    ON staging.companies(duplicate_of_company_id)
    WHERE duplicate_of_company_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_companies_not_dedup_checked
    ON staging.companies(id)
    WHERE dedup_checked_at IS NULL;

-- The trigram index from 001:118 is on legal_name, which is NULL for 92% of
-- tier-2 companies (only 5,016 of 61,944 have one). Tier 2 carries trade_name on
-- all 61,944 rows, so the index built for fuzzy dedup never covered the rows that
-- actually need it. This adds the one that does.
CREATE INDEX IF NOT EXISTS idx_companies_trade_name_trgm
    ON staging.companies USING gin(trade_name gin_trgm_ops);
