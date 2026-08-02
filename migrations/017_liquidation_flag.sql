-- 017_liquidation_flag.sql
-- =============================================================================
-- Surface the registry's "this business is being wound up" signal.
--
-- MEASURED 2026-08-02 (S9-H): 446 businesses in the deliverable have a
-- liquidator, mandataire or administrateur judiciaire as their RNE officer, and
-- **ZERO of them are flagged FERME by the provider**. The two signals do not
-- overlap at all: the source file says these businesses are trading, while the
-- official registry says a liquidator has been appointed.
--
-- That is exactly the independent, registry-sourced answer Sam's ask #3 wanted,
-- and it is strictly additive to the 1,643 already flagged FERME. 8 of the 446
-- currently hold an email.
--
-- DECISION: FLAG, DO NOT EXCLUDE — the same call taken for FERME on 2026-07-28.
-- A liquidator means winding up, which is usually terminal but not always
-- instant, and writing it into is_active would silently shrink the base. The
-- export gains a column and an opt-in --exclude-liquidation flag, so whether to
-- drop these rows stays a run-time decision for Ines and Sam.
--
-- WHY business_id AND NOT company_id: dedup merges entities, and the liquidator
-- may be recorded on the merged-away duplicate. Matching on
-- COALESCE(duplicate_of_company_id, id) catches those; matching on company_id
-- would silently miss them.
--
-- SAFE: CREATE OR REPLACE with in_liquidation APPENDED as the final column, so
-- the existing column list and order are untouched and v_enrichment_queue
-- (which selects explicit columns) is unaffected.
--
-- REVERSAL: replay migration 012, which holds the previous definition.
-- =============================================================================

CREATE OR REPLACE VIEW public.v_deliverable_businesses AS
WITH per_business AS (
    SELECT DISTINCT ON (q.business_id)
           q.siren, q.legal_name, q.trade_name, q.naf_code, q.naf_label,
           q.employee_bracket, q.qualification_status, q.siret, q.address_line1,
           q.postal_code, q.city, q.department, q.region, q.website_domain,
           q.site_active, q.first_name, q.last_name, q.full_name, q.job_title,
           q.job_function, q.phone_main, q.enrichment_status, q.email_address,
           q.email_status, q.email_is_primary, q.tier,
           q.qualification_rule_version, q.qualified_at, q.legal_form,
           q.sirene_etat, q.creation_date, q.email_verified, q.collected_at,
           q.data_source, q.company_id, q.department_code, q.department_source,
           q.business_id, q.is_duplicate, q.shared_address_group,
           q.shared_address_entities, q.source_status, q.source_closed
    FROM public.v_qualified_contacts q
    WHERE NOT q.is_duplicate
    ORDER BY q.business_id, q.source_closed,
             (q.email_address IS NOT NULL) DESC,
             (q.tier = 'tier1_official_naf') DESC,
             q.email_verified DESC NULLS LAST,
             q.full_name, q.email_address, q.phone_main, q.siret
), per_family AS (
    SELECT DISTINCT ON (COALESCE(p.shared_address_group, p.business_id::text))
           p.*
    FROM per_business p
    ORDER BY COALESCE(p.shared_address_group, p.business_id::text),
             p.source_closed,
             (p.email_address IS NOT NULL) DESC,
             (p.tier = 'tier1_official_naf') DESC,
             p.business_id
), liq AS (
    -- One row per business that the registry says is being wound up.
    SELECT DISTINCT COALESCE(co.duplicate_of_company_id, co.id) AS business_id
    FROM staging.contacts lc
    JOIN staging.companies co ON co.id = lc.company_id
    WHERE lc.name_source = 'rne_dirigeant'
      AND (lc.job_title ILIKE '%liquidateur%'
        OR lc.job_title ILIKE '%mandataire%'
        OR lc.job_title ILIKE '%administrateur judiciaire%')
)
SELECT pf.siren, pf.legal_name, pf.trade_name, pf.naf_code, pf.naf_label,
       pf.employee_bracket, pf.qualification_status, pf.siret, pf.address_line1,
       pf.postal_code, pf.city, pf.department, pf.region, pf.website_domain,
       pf.site_active, pf.first_name, pf.last_name, pf.full_name, pf.job_title,
       pf.job_function, pf.phone_main, pf.enrichment_status, pf.email_address,
       pf.email_status, pf.email_is_primary, pf.tier,
       pf.qualification_rule_version, pf.qualified_at, pf.legal_form,
       pf.sirene_etat, pf.creation_date, pf.email_verified, pf.collected_at,
       pf.data_source, pf.company_id, pf.department_code, pf.department_source,
       pf.business_id, pf.is_duplicate, pf.shared_address_group,
       pf.shared_address_entities, pf.source_status, pf.source_closed,
       COALESCE(NULLIF(btrim(pf.legal_name), ''), pf.trade_name) AS display_name,
       (l.business_id IS NOT NULL) AS in_liquidation
FROM per_family pf
LEFT JOIN liq l ON l.business_id = pf.business_id;

COMMENT ON VIEW public.v_deliverable_businesses IS
    'The single definition of the deliverable population. in_liquidation '
    '(migration 017) is a REGISTRY signal - an RNE officer who is a liquidator '
    'or judicial administrator - and is independent of source_closed, which is '
    'the provider''s own Fermé flag. Measured overlap between the two: zero.';
