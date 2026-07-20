-- 010_multi_entity_flag.sql
-- Decision 3 (2026-07-20): multi-entity families.
--
-- One farmer often operates through several legal entities at a single address
-- (sole trader + EARL, or EARL + SCI holding the land). They are legally
-- distinct, carry different SIRENs, and are deliberately NOT merged — but they
-- usually share one decision-maker, so a campaign that mails all of them sends
-- two or three near-identical emails to the same person.
--
-- Measured 2026-07-20: 1,400 addresses hold 2,915 distinct legal entities.
--
-- The decision was to FLAG, not merge: marking is non-destructive and reversible,
-- keeps the legal distinction intact, and lets the campaign choose at send time
-- whether to contact one entity per address or all of them.
--
-- Adds two columns at the end of public.v_qualified_contacts:
--   shared_address_group    stable key for the address, NULL when not shared
--   shared_address_entities how many distinct legal entities sit at it
--
-- Appending at the end keeps CREATE OR REPLACE VIEW legal and leaves every
-- existing column position untouched, so the NL-query layer is unaffected.

CREATE OR REPLACE VIEW public.v_qualified_contacts AS
WITH addr AS (
    -- Company-level address key. Computed before the contacts join so that a
    -- company with several contacts is still counted as ONE entity.
    SELECT
        co.id AS company_id,
        co.siren,
        btrim(s.postal_code::text) AS pc,
        lower(regexp_replace(coalesce(s.address_line1, ''), '[^a-zA-Z0-9]+', ' ', 'g')) AS addr_norm
    FROM staging.companies co
    JOIN staging.sites s ON s.company_id = co.id
    WHERE co.qualification_status IN ('qualified', 'qualified_unverified')
      AND co.duplicate_of_company_id IS NULL
      AND co.siren IS NOT NULL
      AND btrim(coalesce(s.postal_code::text, '')) <> ''
      AND btrim(coalesce(s.address_line1, '')) <> ''
),
addr_groups AS (
    SELECT
        pc,
        addr_norm,
        md5(pc || '|' || addr_norm) AS group_key,
        count(DISTINCT siren) AS entities
    FROM addr
    GROUP BY pc, addr_norm
    HAVING count(DISTINCT siren) > 1
)
SELECT
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
        WHEN 'qualified'::text THEN 'tier1_official_naf'::text
        WHEN 'qualified_unverified'::text THEN 'tier2_source_label'::text
        ELSE NULL::text
    END AS tier,
    co.qualification_rule_version,
    co.qualified_at,
    co.legal_form,
    co.sirene_etat,
    co.creation_date,
    e.verification_status = 'valid'::text AS email_verified,
    sf.collected_at,
    sf.data_source,
    co.id AS company_id,
    COALESCE(NULLIF(btrim(s.department::text), ''::text),
        CASE
            WHEN btrim(s.postal_code::text) ~ '^[0-9]{4}$'::text THEN "left"(lpad(btrim(s.postal_code::text), 5, '0'::text), 2)
            WHEN btrim(s.postal_code::text) ~ '^9[78][0-9]{3}$'::text THEN "left"(btrim(s.postal_code::text), 3)
            WHEN btrim(s.postal_code::text) ~ '^20[0-9]{3}$'::text THEN
                CASE
                    WHEN substr(btrim(s.postal_code::text), 3, 1) = ANY (ARRAY['0'::text, '1'::text]) THEN '2A'::text
                    ELSE '2B'::text
                END
            WHEN btrim(s.postal_code::text) ~ '^[0-9]{5}$'::text THEN "left"(btrim(s.postal_code::text), 2)
            ELSE NULL::text
        END) AS department_code,
    CASE
        WHEN NULLIF(btrim(s.department::text), ''::text) IS NOT NULL THEN 'sites.department'::text
        WHEN btrim(s.postal_code::text) ~ '^[0-9]{4}$'::text THEN 'derived: postal, leading zero restored'::text
        WHEN btrim(s.postal_code::text) ~ '^9[78][0-9]{3}$'::text THEN 'derived: postal, DOM-TOM 3-digit'::text
        WHEN btrim(s.postal_code::text) ~ '^20[0-9]{3}$'::text THEN 'derived: postal, Corsica 2A/2B (approx)'::text
        WHEN btrim(s.postal_code::text) ~ '^[0-9]{5}$'::text THEN 'derived: postal, metropolitan'::text
        ELSE 'unknown'::text
    END AS department_source,
    COALESCE(co.duplicate_of_company_id, co.id) AS business_id,
    co.duplicate_of_company_id IS NOT NULL AS is_duplicate,
    -- new in 010
    ag.group_key AS shared_address_group,
    ag.entities  AS shared_address_entities
FROM staging.companies co
    JOIN staging.sites s ON s.company_id = co.id
    JOIN staging.contacts c ON c.site_id = s.id
    JOIN staging.source_files sf ON sf.id = co.source_file_id
    LEFT JOIN LATERAL (
        SELECT em.email_address, em.verification_status, em.is_primary
        FROM staging.emails em
        WHERE em.contact_id = c.id
        ORDER BY (em.verification_status = 'valid'::text) DESC, em.is_primary DESC, em.created_at
        LIMIT 1
    ) e ON true
    LEFT JOIN addr_groups ag
        ON ag.pc = btrim(s.postal_code::text)
       AND ag.addr_norm = lower(regexp_replace(coalesce(s.address_line1, ''), '[^a-zA-Z0-9]+', ' ', 'g'))
WHERE (co.qualification_status = ANY (ARRAY['qualified'::text, 'qualified_unverified'::text]))
  AND s.is_active IS DISTINCT FROM false;

COMMENT ON VIEW public.v_qualified_contacts IS
'Campaign-ready qualified contacts. shared_address_group is non-NULL when several
distinct legal entities share one address (the multi-entity farmer pattern) —
dedupe on it at send time to avoid mailing the same decision-maker twice.';
