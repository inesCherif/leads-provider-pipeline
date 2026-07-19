-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 008 — Resolve duplicates in the query views
-- ─────────────────────────────────────────────────────────────────────────────
-- Views only. Nothing is deleted or hidden.
--
-- Adds two columns to v_qualified_contacts:
--   business_id   coalesce(duplicate_of_company_id, id) - the deduplicated identity.
--                 Count DISTINCT business_id to count real businesses.
--   is_duplicate  TRUE if this row is a duplicate of another company.
--
-- WHY ROWS ARE KEPT RATHER THAN FILTERED OUT: 44 duplicate groups have an email
-- on the duplicate that the survivor does NOT have. Filtering duplicates out of
-- the view would silently discard those contacts. Keeping the contact rows and
-- deduplicating only the *identity* means the business is counted once while
-- every reachable contact stays reachable.
--
-- Consequence for consumers: v_qualified_contacts has always been one row per
-- CONTACT, not per company. That has not changed. What changed is that counting
-- companies must now use count(DISTINCT business_id), not count(DISTINCT siren)
-- (NULL for all of tier 2) or count(DISTINCT company_id) (double-counts duplicates).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW public.v_qualified_contacts AS
SELECT
    co.siren, co.legal_name, co.trade_name, co.naf_code, co.naf_label,
    co.employee_bracket, co.qualification_status,
    s.siret, s.address_line1, s.postal_code, s.city, s.department, s.region,
    s.website_domain, s.is_active AS site_active,
    c.first_name, c.last_name, c.full_name, c.job_title, c.job_function,
    c.phone_main, c.enrichment_status,
    e.email_address, e.verification_status AS email_status, e.is_primary AS email_is_primary,
    CASE co.qualification_status
        WHEN 'qualified'            THEN 'tier1_official_naf'
        WHEN 'qualified_unverified' THEN 'tier2_source_label'
    END AS tier,
    co.qualification_rule_version,
    co.qualified_at,
    co.legal_form,
    co.sirene_etat,
    co.creation_date,
    (e.verification_status = 'valid') AS email_verified,
    sf.collected_at,
    sf.data_source,
    co.id AS company_id,
    coalesce(
        nullif(btrim(s.department), ''),
        CASE
            WHEN btrim(s.postal_code) ~ '^[0-9]{4}$'
                THEN left(lpad(btrim(s.postal_code), 5, '0'), 2)
            WHEN btrim(s.postal_code) ~ '^9[78][0-9]{3}$'
                THEN left(btrim(s.postal_code), 3)
            WHEN btrim(s.postal_code) ~ '^20[0-9]{3}$'
                THEN CASE WHEN substr(btrim(s.postal_code), 3, 1) IN ('0','1')
                          THEN '2A' ELSE '2B' END
            WHEN btrim(s.postal_code) ~ '^[0-9]{5}$'
                THEN left(btrim(s.postal_code), 2)
        END
    ) AS department_code,
    CASE
        WHEN nullif(btrim(s.department), '') IS NOT NULL THEN 'sites.department'
        WHEN btrim(s.postal_code) ~ '^[0-9]{4}$'         THEN 'derived: postal, leading zero restored'
        WHEN btrim(s.postal_code) ~ '^9[78][0-9]{3}$'    THEN 'derived: postal, DOM-TOM 3-digit'
        WHEN btrim(s.postal_code) ~ '^20[0-9]{3}$'       THEN 'derived: postal, Corsica 2A/2B (approx)'
        WHEN btrim(s.postal_code) ~ '^[0-9]{5}$'         THEN 'derived: postal, metropolitan'
        ELSE 'unknown'
    END AS department_source,
    -- ── appended by 008 ──────────────────────────────────────────────────────
    coalesce(co.duplicate_of_company_id, co.id)        AS business_id,
    (co.duplicate_of_company_id IS NOT NULL)           AS is_duplicate
FROM staging.companies co
JOIN staging.sites        s  ON s.company_id = co.id
JOIN staging.contacts     c  ON c.site_id    = s.id
JOIN staging.source_files sf ON sf.id        = co.source_file_id
LEFT JOIN LATERAL (
    SELECT em.email_address, em.verification_status, em.is_primary
    FROM staging.emails em
    WHERE em.contact_id = c.id
    ORDER BY (em.verification_status = 'valid') DESC, em.is_primary DESC, em.created_at
    LIMIT 1
) e ON TRUE
WHERE co.qualification_status IN ('qualified', 'qualified_unverified')
  AND s.is_active IS DISTINCT FROM FALSE;

COMMENT ON VIEW public.v_qualified_contacts IS
    'Main query view for the AI/MCP layer. One row per qualified CONTACT (not per '
    'company). To count businesses use count(DISTINCT business_id) - siren is NULL '
    'for all of tier 2, and company_id double-counts duplicates. tier1_official_naf '
    '= qualified on the official INSEE NAF code. tier2_source_label = no SIREN, '
    'qualified on the source activity label alone, lower confidence. Rows with '
    'is_duplicate = TRUE are kept deliberately: some hold an email the survivor '
    'lacks.';

CREATE OR REPLACE VIEW public.v_leads_by_department AS
SELECT
    department,
    tier,
    count(DISTINCT business_id)                        AS companies,
    count(*)                                           AS contacts,
    count(*) FILTER (WHERE email_address IS NOT NULL)  AS with_email,
    count(*) FILTER (WHERE email_verified)             AS with_verified_email,
    count(*) FILTER (WHERE phone_main IS NOT NULL)     AS with_phone,
    department_code
FROM public.v_qualified_contacts
GROUP BY department_code, department, tier;

COMMENT ON VIEW public.v_leads_by_department IS
    'Qualified lead counts per French department and tier. companies counts '
    'DISTINCT business_id, so duplicates are collapsed. Group on department_code '
    '(derived from postal where sites.department is NULL), not department, which '
    'is NULL for 93% of sites. NOTE: with_verified_email is 0 until M1-S6 runs.';
