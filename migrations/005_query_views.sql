-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 005 — Query views for the NL/MCP layer (M1-S7)
-- ─────────────────────────────────────────────────────────────────────────────
-- Views only. No table, column, or row is touched. Reversible by re-running
-- 001's view definitions.
--
-- Changes:
--   1. v_qualified_contacts now exposes BOTH qualification tiers, with a `tier`
--      column to tell them apart. Previously it filtered to 'qualified' only,
--      which hid all 61,944 tier-2 companies.
--   2. Email join replaced with a LATERAL "best email per contact" pick.
--   3. Exposes legal_form, sirene_etat, creation_date, qualified_at,
--      qualification_rule_version, collected_at, data_source — none of which any
--      view surfaced before.
--   4. New v_leads_by_department so the Milestone-1 acceptance question can be
--      answered directly.
--
-- COLUMN ORDER IS DELIBERATE. The first 25 columns are kept in their original
-- 001 order and new ones are appended, because CREATE OR REPLACE VIEW cannot
-- rename or reorder existing columns — reordering would force a DROP VIEW.
-- Keeping the order means this migration needs no DROP at all, so nothing
-- depending on the view can break. Add future columns at the END for the same
-- reason.
--
-- ON THE EMAIL JOIN — the original view had:
--     LEFT JOIN staging.emails e ON e.contact_id = c.id AND e.is_primary = TRUE
-- This is NOT currently dropping any data: all 25,506 emails are is_primary=TRUE,
-- exactly one per contact, and no contact has more than one. So today the join is
-- harmless. It becomes wrong the moment M1-S6 adds a second, verified address to a
-- contact that already has a primary one — the verified address would be invisible.
-- The LATERAL below fixes it ahead of that, preferring verified over merely
-- primary. Behaviour is identical on today's data.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW public.v_qualified_contacts AS
SELECT
    -- ── original 001 columns, order preserved (positions 1-25) ───────────────
    co.siren,
    co.legal_name,
    co.trade_name,
    co.naf_code,
    co.naf_label,
    co.employee_bracket,
    co.qualification_status,
    s.siret,
    s.address_line1,
    s.postal_code,
    s.city,
    s.department,
    s.region,
    s.website_domain,
    s.is_active                           AS site_active,
    c.first_name,
    c.last_name,
    c.full_name,
    c.job_title,
    c.job_function,
    c.phone_main,
    c.enrichment_status,
    e.email_address,
    e.verification_status                 AS email_status,
    e.is_primary                          AS email_is_primary,
    -- ── appended by 005 ──────────────────────────────────────────────────────
    CASE co.qualification_status
        WHEN 'qualified'            THEN 'tier1_official_naf'
        WHEN 'qualified_unverified' THEN 'tier2_source_label'
    END                                   AS tier,
    co.qualification_rule_version,
    co.qualified_at,
    co.legal_form,
    co.sirene_etat,
    co.creation_date,
    (e.verification_status = 'valid')     AS email_verified,
    sf.collected_at,
    sf.data_source,
    -- company_id, not siren, is the correct identity key: tier-2 companies have
    -- no SIREN at all, so count(DISTINCT siren) silently returns 0 for them.
    co.id                                 AS company_id,
    -- department is CHAR(3) and comes back space-padded ('85 '). A natural-language
    -- query asking for department 85 would not match it. Trimmed copy for querying;
    -- the padded original is kept above so the column type never changes.
    btrim(s.department)                   AS department_code
FROM staging.companies co
JOIN staging.sites        s  ON s.company_id = co.id
JOIN staging.contacts     c  ON c.site_id    = s.id
JOIN staging.source_files sf ON sf.id        = co.source_file_id
-- best available email for this contact: verified beats primary beats anything
LEFT JOIN LATERAL (
    SELECT em.email_address, em.verification_status, em.is_primary
    FROM staging.emails em
    WHERE em.contact_id = c.id
    ORDER BY (em.verification_status = 'valid') DESC,
             em.is_primary DESC,
             em.created_at
    LIMIT 1
) e ON TRUE
WHERE co.qualification_status IN ('qualified', 'qualified_unverified')
  AND s.is_active IS DISTINCT FROM FALSE;  -- keep NULL (unknown), drop confirmed closed

COMMENT ON VIEW public.v_qualified_contacts IS
    'Main query view for the AI/MCP layer. One row per qualified contact, with the '
    'best available email. tier1_official_naf = qualified on the official INSEE NAF '
    'code (has a SIREN). tier2_source_label = no SIREN, qualified on the data '
    'provider''s activity label alone - usable but lower confidence. Filter on tier '
    'or qualification_status to separate them.';


CREATE OR REPLACE VIEW public.v_leads_by_department AS
SELECT
    department,                                        -- CHAR(3), space-padded
    tier,
    count(DISTINCT company_id)                         AS companies,
    count(*)                                           AS contacts,
    count(*) FILTER (WHERE email_address IS NOT NULL)  AS with_email,
    count(*) FILTER (WHERE email_verified)             AS with_verified_email,
    count(*) FILTER (WHERE phone_main IS NOT NULL)     AS with_phone,
    btrim(department)                                  AS department_code
FROM public.v_qualified_contacts
GROUP BY department, tier;

COMMENT ON VIEW public.v_leads_by_department IS
    'Qualified lead counts per French department and qualification tier. '
    'NOTE: with_verified_email is 0 until M1-S6 runs - every email is still '
    'verification_status=candidate by design, since verification happens at '
    'campaign send time, not at ingestion.';
