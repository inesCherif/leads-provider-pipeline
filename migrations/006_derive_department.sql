-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 006 — Derive department from postal code in the query views
-- ─────────────────────────────────────────────────────────────────────────────
-- Views only. staging.sites.department is NOT modified: the source data genuinely
-- lacks it, and deriving in the view keeps the raw value honest and the fix
-- reversible. If we later decide to persist it, this expression is the reference.
--
-- WHY: 119,786 of 128,267 sites (93.4%) have department IS NULL, which makes
-- "broken down by department" — the Milestone-1 acceptance question — unanswerable
-- for almost the whole dataset. But 93,388 of those DO have a postal_code, so the
-- department is recoverable for ~78% of the gap.
--
-- The four cases, in order (order matters):
--   1. 4-digit postal ('7200', '1000')  → leading zero lost in Excel's numeric
--      conversion. Left-pad to 5 then take 2: '7200' -> '07200' -> '07' (Ardeche).
--      This is the clean_postal_code() bug in m1_s3_ingest.py surfacing in the data.
--   2. 97xxx / 98xxx  → DOM-TOM departments are THREE digits (971 Guadeloupe,
--      972 Martinique, 974 Reunion...). Taking 2 would collapse them all to '97'.
--   3. 20xxx  → Corsica has no numeric department. 200xx/201xx = 2A (Corse-du-Sud),
--      202xx-206xx = 2B (Haute-Corse). This split is the standard convention but is
--      an approximation at the boundary; ~173 rows are affected.
--   4. any other 5-digit  → metropolitan France, first 2 digits.
--
-- department_source records which branch produced the value, so a consumer can
-- tell a real department from an inferred one.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW public.v_qualified_contacts AS
SELECT
    -- ── original 001 columns, order preserved (positions 1-25) ───────────────
    co.siren, co.legal_name, co.trade_name, co.naf_code, co.naf_label,
    co.employee_bracket, co.qualification_status,
    s.siret, s.address_line1, s.postal_code, s.city, s.department, s.region,
    s.website_domain, s.is_active AS site_active,
    c.first_name, c.last_name, c.full_name, c.job_title, c.job_function,
    c.phone_main, c.enrichment_status,
    e.email_address, e.verification_status AS email_status, e.is_primary AS email_is_primary,
    -- ── appended by 005 ──────────────────────────────────────────────────────
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
    -- ── department_code: real value first, else derived from postal (006) ────
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
    -- ── appended by 006 ──────────────────────────────────────────────────────
    CASE
        WHEN nullif(btrim(s.department), '') IS NOT NULL THEN 'sites.department'
        WHEN btrim(s.postal_code) ~ '^[0-9]{4}$'         THEN 'derived: postal, leading zero restored'
        WHEN btrim(s.postal_code) ~ '^9[78][0-9]{3}$'    THEN 'derived: postal, DOM-TOM 3-digit'
        WHEN btrim(s.postal_code) ~ '^20[0-9]{3}$'       THEN 'derived: postal, Corsica 2A/2B (approx)'
        WHEN btrim(s.postal_code) ~ '^[0-9]{5}$'         THEN 'derived: postal, metropolitan'
        ELSE 'unknown'
    END AS department_source
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

-- v_leads_by_department now groups on the derived code, not the mostly-NULL column.
CREATE OR REPLACE VIEW public.v_leads_by_department AS
SELECT
    department,
    tier,
    count(DISTINCT company_id)                         AS companies,
    count(*)                                           AS contacts,
    count(*) FILTER (WHERE email_address IS NOT NULL)  AS with_email,
    count(*) FILTER (WHERE email_verified)             AS with_verified_email,
    count(*) FILTER (WHERE phone_main IS NOT NULL)     AS with_phone,
    department_code
FROM public.v_qualified_contacts
GROUP BY department_code, department, tier;

COMMENT ON VIEW public.v_leads_by_department IS
    'Qualified lead counts per French department and qualification tier. Group on '
    'department_code (derived from postal code where sites.department is NULL), not '
    'on department, which is NULL for 93% of sites. NOTE: with_verified_email is 0 '
    'until M1-S6 runs - every email is still verification_status=candidate by design.';
