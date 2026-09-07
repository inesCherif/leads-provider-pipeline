-- 019_sector_in_views.sql
-- M4 -- the public views learn which sector(s) a business belongs to.
--
-- WHY: until 018, sector lived only on staging.source_files and no view
-- exposed it, so m1_s8_export.py could not filter by sector (it would have
-- exported every sector into one agriculture file) and check_data_quality.py
-- could only assert GLOBAL bands. With staging.company_sources (018) a company
-- can belong to several files, so the view exposes:
--     sectors         TEXT[]  every staging.source_files.sector the company
--                             appears in, sorted (deterministic)
--     primary_sector  TEXT    the sector of the file that CREATED the company
--                             row (companies.source_file_id) -- stable, never
--                             changes when a later file re-lists the business
--
-- SAFETY: every view is CREATE OR REPLACE with the two columns APPENDED as the
-- final columns, so existing column names, order and types are untouched and
-- every existing consumer keeps working. v_enrichment_queue used `SELECT *`
-- through two CTEs; it now enumerates its output columns explicitly so the
-- new columns can be appended without reordering.
--
-- REVERSAL: replay 015 (v_qualified_contacts), 017 (v_deliverable_businesses)
-- and 012 (v_enrichment_queue); DROP VIEW public.v_sector_summary.

-- ---- 1. v_qualified_contacts (body of 015 + sectors) -----------------------
CREATE OR REPLACE VIEW public.v_qualified_contacts AS
WITH addr AS (
    SELECT co_1.id AS company_id,
           co_1.siren,
           btrim(s_1.postal_code::text) AS pc,
           lower(regexp_replace(COALESCE(s_1.address_line1, ''), '[^a-zA-Z0-9]+', ' ', 'g')) AS addr_norm
    FROM staging.companies co_1
    JOIN staging.sites s_1 ON s_1.company_id = co_1.id
    WHERE co_1.qualification_status = ANY (ARRAY['qualified', 'qualified_unverified'])
      AND co_1.duplicate_of_company_id IS NULL
      AND co_1.siren IS NOT NULL
      AND btrim(COALESCE(s_1.postal_code::text, '')) <> ''
      AND btrim(COALESCE(s_1.address_line1, '')) <> ''
), addr_groups AS (
    SELECT addr.pc,
           addr.addr_norm,
           md5((addr.pc || '|') || addr.addr_norm) AS group_key,
           count(DISTINCT addr.siren) AS entities
    FROM addr
    GROUP BY addr.pc, addr.addr_norm
    HAVING count(DISTINCT addr.siren) > 1
), sect AS (
    -- Every sector a company appears in (migration 018 membership table).
    SELECT cs.company_id,
           array_agg(DISTINCT sf2.sector ORDER BY sf2.sector) AS sectors
    FROM staging.company_sources cs
    JOIN staging.source_files sf2 ON sf2.id = cs.source_file_id
    WHERE sf2.sector IS NOT NULL
    GROUP BY cs.company_id
)
SELECT co.siren,
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
       s.is_active AS site_active,
       c.first_name,
       c.last_name,
       c.full_name,
       c.job_title,
       c.job_function,
       c.phone_main,
       c.enrichment_status,
       e.email_address,
       e.verification_status AS email_status,
       e.is_primary AS email_is_primary,
       CASE co.qualification_status
           WHEN 'qualified' THEN 'tier1_official_naf'
           WHEN 'qualified_unverified' THEN 'tier2_source_label'
           ELSE NULL::text
       END AS tier,
       co.qualification_rule_version,
       co.qualified_at,
       co.legal_form,
       co.sirene_etat,
       co.creation_date,
       e.verification_status = 'valid' AS email_verified,
       sf.collected_at,
       sf.data_source,
       co.id AS company_id,
       COALESCE(NULLIF(btrim(s.department::text), ''),
           CASE
               WHEN btrim(s.postal_code::text) ~ '^[0-9]{4}$'    THEN "left"(lpad(btrim(s.postal_code::text), 5, '0'), 2)
               WHEN btrim(s.postal_code::text) ~ '^9[78][0-9]{3}$' THEN "left"(btrim(s.postal_code::text), 3)
               WHEN btrim(s.postal_code::text) ~ '^20[0-9]{3}$'  THEN
                   CASE WHEN substr(btrim(s.postal_code::text), 3, 1) = ANY (ARRAY['0', '1']) THEN '2A' ELSE '2B' END
               WHEN btrim(s.postal_code::text) ~ '^[0-9]{5}$'    THEN "left"(btrim(s.postal_code::text), 2)
               ELSE NULL::text
           END) AS department_code,
       CASE
           WHEN NULLIF(btrim(s.department::text), '') IS NOT NULL THEN 'sites.department'
           WHEN btrim(s.postal_code::text) ~ '^[0-9]{4}$'    THEN 'derived: postal, leading zero restored'
           WHEN btrim(s.postal_code::text) ~ '^9[78][0-9]{3}$' THEN 'derived: postal, DOM-TOM 3-digit'
           WHEN btrim(s.postal_code::text) ~ '^20[0-9]{3}$'  THEN 'derived: postal, Corsica 2A/2B (approx)'
           WHEN btrim(s.postal_code::text) ~ '^[0-9]{5}$'    THEN 'derived: postal, metropolitan'
           ELSE 'unknown'
       END AS department_source,
       COALESCE(co.duplicate_of_company_id, co.id) AS business_id,
       co.duplicate_of_company_id IS NOT NULL AS is_duplicate,
       ag.group_key AS shared_address_group,
       ag.entities AS shared_address_entities,
       s.source_status,
       s.source_status IS NOT NULL
           AND (s.source_status ILIKE 'Ferm%' OR s.source_status ILIKE 'Cess%'
                OR s.source_status ILIKE 'Radi%') AS source_closed,
       -- appended by migration 019
       COALESCE(sx.sectors, ARRAY[sf.sector]::text[]) AS sectors,
       sf.sector AS primary_sector
FROM staging.companies co
JOIN staging.sites s          ON s.company_id = co.id
JOIN staging.contacts c       ON c.site_id = s.id
JOIN staging.source_files sf  ON sf.id = co.source_file_id
LEFT JOIN sect sx             ON sx.company_id = co.id
LEFT JOIN LATERAL (
    SELECT em.email_address,
           em.verification_status,
           em.is_primary
    FROM staging.emails em
    WHERE em.contact_id = c.id
      AND em.verification_status <> 'invalid'
    ORDER BY (em.verification_status = 'valid') DESC,
             em.is_primary DESC,
             em.created_at,
             em.id
    LIMIT 1
) e ON true
LEFT JOIN addr_groups ag
       ON ag.pc = btrim(s.postal_code::text)
      AND ag.addr_norm = lower(regexp_replace(COALESCE(s.address_line1, ''), '[^a-zA-Z0-9]+', ' ', 'g'))
WHERE co.qualification_status = ANY (ARRAY['qualified', 'qualified_unverified'])
  AND s.is_active IS DISTINCT FROM false;

COMMENT ON VIEW public.v_qualified_contacts IS
    'Qualified companies joined to their best contact and best NON-INVALID '
    'email (migration 015). sectors / primary_sector (migration 019) come from '
    'staging.company_sources: a business can belong to several sectors.';

-- ---- 2. v_deliverable_businesses (body of 017 + sectors) -------------------
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
           q.shared_address_entities, q.source_status, q.source_closed,
           q.sectors, q.primary_sector
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
       (l.business_id IS NOT NULL) AS in_liquidation,
       -- appended by migration 019
       pf.sectors,
       pf.primary_sector
FROM per_family pf
LEFT JOIN liq l ON l.business_id = pf.business_id;

COMMENT ON VIEW public.v_deliverable_businesses IS
    'The single definition of the deliverable population. in_liquidation '
    '(migration 017) is a registry signal independent of source_closed. '
    'sectors / primary_sector (migration 019): filter with '
    '''<sector> = ANY(sectors)'' - a business can belong to several sectors.';

-- ---- 3. v_enrichment_queue (body of 012, explicit output list + sectors) ---
CREATE OR REPLACE VIEW public.v_enrichment_queue AS
WITH base AS (
    SELECT
        business_id,
        siren,
        siret,
        display_name,
        legal_name,
        trade_name,
        legal_form,
        naf_code,
        naf_label,
        employee_bracket,
        postal_code,
        city,
        department_code,
        tier,
        full_name,
        phone_main,
        email_address,
        website_domain,
        lower(NULLIF(split_part(COALESCE(email_address, ''), '@', 2), '')) AS email_domain,
        sectors,
        primary_sector
    FROM public.v_deliverable_businesses
    WHERE NOT source_closed
),
flagged AS (
    SELECT
        *,
        (btrim(COALESCE(full_name, '')) <> '')  AS has_contact_name,
        (email_address IS NOT NULL)             AS has_email,
        (email_domain IS NOT NULL AND email_domain NOT IN (
            'orange.fr','gmail.com','wanadoo.fr','hotmail.fr','yahoo.fr','free.fr',
            'sfr.fr','laposte.net','hotmail.com','aol.com','outlook.fr','live.fr',
            'bbox.fr','neuf.fr','yahoo.com','outlook.com','msn.com','gmailcom',
            'me.com','icloud.com','mail.com','gmx.fr','numericable.fr','dbmail.com'
        )) AS domain_is_corporate
    FROM base
)
SELECT
    business_id, siren, siret, display_name, legal_name, trade_name, legal_form,
    naf_code, naf_label, employee_bracket, postal_code, city, department_code,
    tier, full_name, phone_main, email_address, website_domain, email_domain,
    has_contact_name, has_email, domain_is_corporate,
    (NOT has_contact_name AND siren IS NOT NULL) AS needs_dirigeant,
    (NOT has_contact_name AND siren IS NULL)     AS needs_name_parse,
    (domain_is_corporate AND has_contact_name)   AS can_mine_pattern,
    (NOT domain_is_corporate)                    AS needs_domain,
    CASE
        WHEN naf_code ~ '^(10|11|35)' THEN 1
        WHEN employee_bracket ~ '^[0-9]+$' AND employee_bracket::int >= 3 THEN 1
        WHEN legal_form ~* '^(SAS|SARL|SA$|SCEA|COOP|SCA)' THEN 2
        WHEN legal_form ~* '^(EARL|GAEC)' THEN 3
        WHEN siren IS NOT NULL THEN 4
        ELSE 5
    END AS enrichment_priority,
    -- appended by migration 019
    sectors,
    primary_sector
FROM flagged;

COMMENT ON VIEW public.v_enrichment_queue IS
'Work queue for enrichment. One row per LIVE deliverable business with boolean
columns saying what each still needs. enrichment_priority 1..5 orders the
rate-limited scraping steps. sectors / primary_sector (migration 019) let a
step select one sector''s queue.';

-- ---- 4. v_sector_summary: one row per source-file sector -------------------
CREATE OR REPLACE VIEW public.v_sector_summary AS
WITH members AS (
    SELECT sf.sector, cs.company_id, co.qualification_status, co.duplicate_of_company_id
    FROM staging.company_sources cs
    JOIN staging.source_files sf ON sf.id = cs.source_file_id
    JOIN staging.companies co    ON co.id = cs.company_id
    WHERE sf.sector IS NOT NULL
), per_sector AS (
    SELECT sector,
           count(DISTINCT company_id) AS companies,
           count(DISTINCT company_id) FILTER (WHERE qualification_status IN ('qualified','qualified_unverified')) AS qualified,
           count(DISTINCT company_id) FILTER (WHERE qualification_status = 'unqualified') AS unqualified,
           count(DISTINCT company_id) FILTER (WHERE duplicate_of_company_id IS NOT NULL) AS duplicates
    FROM members GROUP BY sector
)
SELECT ps.sector, ps.companies, ps.qualified, ps.unqualified, ps.duplicates,
       d.deliverable, d.with_phone, d.with_email, d.joignable
FROM per_sector ps
LEFT JOIN LATERAL (
    SELECT count(*) AS deliverable,
           count(*) FILTER (WHERE v.phone_main IS NOT NULL)    AS with_phone,
           count(*) FILTER (WHERE v.email_address IS NOT NULL) AS with_email,
           count(*) FILTER (WHERE v.phone_main IS NOT NULL OR v.email_address IS NOT NULL) AS joignable
    FROM public.v_deliverable_businesses v
    WHERE ps.sector = ANY (v.sectors)
) d ON true
ORDER BY ps.sector;

COMMENT ON VIEW public.v_sector_summary IS
    'One row per staging.source_files.sector: companies, qualified, duplicates, '
    'and the deliverable counts (phone / e-mail / joignable). Slow-ish: it '
    'evaluates the deliverable view once per sector.';

-- Verification:
--   SELECT column_name FROM information_schema.columns
--    WHERE table_schema='public' AND table_name='v_deliverable_businesses'
--    ORDER BY ordinal_position;                 -- ends with ..., in_liquidation, sectors, primary_sector
--   SELECT primary_sector, count(*) FROM public.v_deliverable_businesses GROUP BY 1;
--   SELECT * FROM public.v_sector_summary;
