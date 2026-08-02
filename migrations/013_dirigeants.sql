-- 013_dirigeants.sql
-- M1-S9-1 — storage for the dirigeant (company officer) backfill.
--
-- WHY: 14,061 live businesses in v_enrichment_queue have a SIREN and a phone but
-- no contact name. The recherche-entreprises.api.gouv.fr endpoint m1_s4 already
-- calls returns a `dirigeants` array that m1_s4 discards. A measured sample of 80
-- targets (2026-08-02) returned a usable natural person for 80 of 80 — this is the
-- highest-yield free step left in the project.
--
-- No new table is needed: staging.contacts already exists for every one of these
-- businesses (v_qualified_contacts INNER JOINs contacts, so a business with no
-- contact row would not be in the deliverable at all). 14,635 of the 14,637 target
-- contact rows are is_generic_contact = TRUE placeholders holding only a phone.
-- The backfill fills those rows in place. That is deliberate: inserting extra
-- contacts would change v_qualified_contacts' row count and the DISTINCT ON
-- collapse in v_deliverable_businesses, i.e. it could silently reshuffle a
-- shipped export.
--
-- ORDERING SAFETY: v_deliverable_businesses' per_business pass tiebreaks on
-- `full_name NULLS LAST`. The backfill writes the SAME name to every nameless
-- contact of a company, so those rows stay equal on that key and their relative
-- order is unchanged. Companies that already have a name on ANY contact are
-- skipped entirely by the script, so no existing tiebreak can flip.
--
-- Reversal (complete, non-destructive):
--   UPDATE staging.contacts
--      SET first_name = NULL, last_name = NULL, full_name = NULL,
--          job_title = NULL, job_function = NULL,
--          is_generic_contact = TRUE, name_source = NULL
--    WHERE name_source = 'rne_dirigeant';
--   UPDATE staging.companies
--      SET dirigeants_checked_at = NULL, dirigeants_count = NULL
--    WHERE dirigeants_checked_at IS NOT NULL;

-- ── 1. Checkpoint on the company (the API is keyed by SIREN) ─────────────────

ALTER TABLE staging.companies
    ADD COLUMN IF NOT EXISTS dirigeants_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dirigeants_count      INTEGER;

COMMENT ON COLUMN staging.companies.dirigeants_checked_at IS
    'Checkpoint for m1_s9c_dirigeants.py. NULL = never asked. Set even when the '
    'API returned nobody usable, so a re-run does not repeat a call we know is '
    'empty. Same idempotency pattern as sirene_last_checked_at.';

COMMENT ON COLUMN staging.companies.dirigeants_count IS
    'How many usable natural persons the RNE listed, AFTER dropping personne '
    'morale entries (holdings, audit firms) and statutory auditors. 0 is a real '
    'answer, not a failure. >1 means we picked one of several — see contacts.job_title.';

CREATE INDEX IF NOT EXISTS idx_companies_dirigeants_pending
    ON staging.companies(dirigeants_checked_at)
    WHERE dirigeants_checked_at IS NULL;

-- ── 2. Provenance on the contact ─────────────────────────────────────────────
-- Without this there is no way to tell a name we derived from a name the client's
-- source file supplied — which matters both for trust and for the reversal above.

ALTER TABLE staging.contacts
    ADD COLUMN IF NOT EXISTS name_source TEXT;

COMMENT ON COLUMN staging.contacts.name_source IS
    'Where the contact name came from. NULL = the source Excel (every one of the '
    '74,499 pre-existing names). ''rne_dirigeant'' = derived by '
    'm1_s9c_dirigeants.py from the RNE dirigeants array. Never overwrite a row '
    'whose name_source is NULL — that is client-supplied data.';

CREATE INDEX IF NOT EXISTS idx_contacts_name_source ON staging.contacts(name_source);
