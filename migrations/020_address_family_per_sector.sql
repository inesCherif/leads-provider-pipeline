-- 020_address_family_per_sector.sql
-- M4 -- the shared-address family is computed WITHIN the primary sector.
--
-- WHY (found by reading the regenerated agriculture export, 2026-09-07):
-- v_deliverable_businesses keeps ONE winner per address family
-- (shared_address_group, migration 010: several legal entities of one farmer
-- at one address). The family was computed over every qualified company of
-- every sector. Once tourism and bio operators were loaded, a gîte at a farm's
-- address joined the farm's family and could win it; the winner is exported
-- under ITS primary sector (tourisme), so the farm disappeared from the
-- agriculture export and appeared in no other: 258 farms lost to tourism
-- businesses, 9 to bio operators, 9 to bakeries, measured by diffing the
-- export against the morning's archive by business id.
--
-- Ines's rule (2026-09-07): a company is qualified and exported under its
-- PRIMARY sector. The family collapse therefore has to be per primary sector
-- too: the group key gains the sector, and the join matches on it.
--
-- SAFE: CREATE OR REPLACE with the identical column list of migration 019.
-- v_deliverable_businesses and v_enrichment_queue are untouched (they read
-- shared_address_group as an opaque key).
--
-- REVERSAL: replay migration 019 (section 1).

-- The family key is the RULE SET, not the file's sector value: 'agriculture'
-- and 'livestock' are two client Excels under one rule set
-- (agriculture_livestock), and migration 010's families deliberately span
-- them (an EARL in one file, its GAEC in the other). Must mirror
-- config.sector_rules.SECTORS[*].source_sectors; check_data_quality asserts
-- every sector value has a rule set, which is where a drift would surface.

CREATE OR REPLACE VIEW public.v_qualified_contacts AS
WITH addr AS (
    SELECT co_1.id AS company_id,
           co_1.siren,
           CASE WHEN sf_1.sector IN ('agriculture', 'livestock') THEN 'agriculture_livestock'
                ELSE sf_1.sector END AS sector,
           btrim(s_1.postal_code::text) AS pc,
           lower(regexp_replace(COALESCE(s_1.address_line1, ''), '[^a-zA-Z0-9]+', ' ', 'g')) AS addr_norm
    FROM staging.companies co_1
    JOIN staging.sites s_1 ON s_1.company_id = co_1.id
    JOIN staging.source_files sf_1 ON sf_1.id = co_1.source_file_id
    WHERE co_1.qualification_status = ANY (ARRAY['qualified', 'qualified_unverified'])
      AND co_1.duplicate_of_company_id IS NULL
      AND co_1.siren IS NOT NULL
      AND btrim(COALESCE(s_1.postal_code::text, '')) <> ''
      AND btrim(COALESCE(s_1.address_line1, '')) <> ''
), addr_groups AS (
    SELECT addr.pc,
           addr.addr_norm,
           addr.sector,
           md5((addr.pc || '|') || addr.addr_norm || '|' || COALESCE(addr.sector, '')) AS group_key,
           count(DISTINCT addr.siren) AS entities
    FROM addr
    GROUP BY addr.pc, addr.addr_norm, addr.sector
    HAVING count(DISTINCT addr.siren) > 1
), sect AS (
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
      AND ag.sector IS NOT DISTINCT FROM
          (CASE WHEN sf.sector IN ('agriculture', 'livestock') THEN 'agriculture_livestock'
                ELSE sf.sector END)
WHERE co.qualification_status = ANY (ARRAY['qualified', 'qualified_unverified'])
  AND s.is_active IS DISTINCT FROM false;

COMMENT ON VIEW public.v_qualified_contacts IS
    'Qualified companies joined to their best contact and best NON-INVALID '
    'email (015). sectors / primary_sector (019). The shared-address family '
    '(shared_address_group) is computed WITHIN the primary sector (020): a '
    'gite at a farm''s address must not win the farm''s family.';

-- Verification: the agriculture export loses no business to another sector.
--   SELECT primary_sector, count(*) FROM public.v_deliverable_businesses GROUP BY 1;
