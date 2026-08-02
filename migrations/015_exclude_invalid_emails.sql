-- 015_exclude_invalid_emails.sql
-- =============================================================================
-- Marking an email 'invalid' did not stop it being shipped.
--
-- FOUND 2026-08-02 by S9-F. The best-email picker in v_qualified_contacts is:
--
--     LEFT JOIN LATERAL (
--         SELECT ... FROM staging.emails em
--         WHERE em.contact_id = c.id
--         ORDER BY (em.verification_status = 'valid') DESC,
--                  em.is_primary DESC, em.created_at
--         LIMIT 1) e ON true
--
-- It PREFERS a valid address but never EXCLUDES an invalid one. So a contact
-- whose only address is invalid still ships that address to the client.
--
-- Impact today is small only by luck: S9-E and S9-F both INSERT a repaired
-- replacement before invalidating the original, so the contact had a second row
-- to fall back on. The 2 businesses that surfaced this are the ones where no
-- repair was possible - the domain is dead under every spelling.
--
-- WHY THIS MUST BE FIXED BEFORE ANY VERIFICATION WORK
--     M1-S9 Phase 5 verifies addresses by MX + SMTP RCPT TO and will mark
--     thousands 'invalid' with no replacement to offer. Under the current
--     definition every one of them would keep shipping, and the verification
--     step would silently accomplish nothing. This is a prerequisite, not a
--     tidy-up.
--
-- THE SECOND FIX: a total tiebreak.
--     ORDER BY ... LIMIT 1 has exactly the non-determinism migration 012 fixed
--     for DISTINCT ON. Where status, is_primary and created_at all match, the
--     winner came from the query plan, so the same data could export different
--     bytes. em.id is appended as a guaranteed-unique final tiebreak.
--     (Two runs matching does not prove determinism - it can pass by luck
--     until the plan changes.)
--
-- SAFE TO REPLAY. CREATE OR REPLACE VIEW, identical column list and types, so
-- v_deliverable_businesses and v_enrichment_queue keep working unchanged.
--
-- REVERSAL: replay migration 011, which holds the previous definition.
-- =============================================================================

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
                OR s.source_status ILIKE 'Radi%') AS source_closed
FROM staging.companies co
JOIN staging.sites s          ON s.company_id = co.id
JOIN staging.contacts c       ON c.site_id = s.id
JOIN staging.source_files sf  ON sf.id = co.source_file_id
LEFT JOIN LATERAL (
    SELECT em.email_address,
           em.verification_status,
           em.is_primary
    FROM staging.emails em
    WHERE em.contact_id = c.id
      -- THE FIX. An address we have proven undeliverable must never be
      -- offered to a campaign. Without this, invalidating an email has no
      -- effect on what ships.
      AND em.verification_status <> 'invalid'
    ORDER BY (em.verification_status = 'valid') DESC,
             em.is_primary DESC,
             em.created_at,
             -- guaranteed-unique final tiebreak, per the migration 012 lesson
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
    'email. Emails with verification_status=''invalid'' are excluded outright '
    '(migration 015) - preferring a valid address was not enough, because a '
    'contact whose only address is invalid still shipped it.';
