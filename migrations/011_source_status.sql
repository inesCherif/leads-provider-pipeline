-- 011_source_status.sql
-- M1-S9a — recover the source activity status lost at ingestion.
--
-- WHY: the source Excel carries a `Statut_Activite` column ('Actif', 'Fermé',
-- 'Introuvable', 'Non trouvé'). For file A ("Copie de agriculteurs total.xlsx")
-- the COL_MAP in m1_s3_ingest.py named it "Statut_Entreprise", which is not a
-- header in that file, so row.get() returned None and the status silently became
-- NULL for every one of its rows — 3,498 'Fermé' among them. File C mapped the
-- same column correctly, which is why 2,947 sites already carry is_active=FALSE.
--
-- The data was never lost: raw.ingest_rows.raw_json holds every source column
-- verbatim. This migration adds somewhere to put it back.
--
-- WHY A NEW COLUMN AND NOT is_active: 'Fermé' here is the data provider's own
-- enrichment verdict, not a registry check we performed. Writing it into
-- is_active would drop those businesses out of v_qualified_contacts entirely
-- (the view filters `is_active IS DISTINCT FROM false`), silently shrinking the
-- deliverable on someone else's say-so. source_status records what the source
-- claimed; the decision to exclude stays a business call, taken with the number
-- in front of you. m1_s8_export.py --exclude-closed acts on it when you choose.
--
-- Reversal:
--   UPDATE staging.sites SET source_status = NULL, source_status_backfilled_at = NULL;
--   -- and replay migration 010 to drop the two view columns.

-- ── 1. Storage ───────────────────────────────────────────────────────────────

ALTER TABLE staging.sites
    ADD COLUMN IF NOT EXISTS source_status               TEXT,
    ADD COLUMN IF NOT EXISTS source_status_backfilled_at TIMESTAMPTZ;

COMMENT ON COLUMN staging.sites.source_status IS
    'Verbatim Statut_Activite from the source file (Actif / Fermé / Introuvable / '
    'Non trouvé). Backfilled from raw.ingest_rows.raw_json by m1_s9a_status_backfill.py. '
    'NOT a registry verdict — INSEE is not consulted here. Deliberately kept separate '
    'from sites.is_active so that flagging a business does not silently remove it.';

COMMENT ON COLUMN staging.sites.source_status_backfilled_at IS
    'Checkpoint for the idempotent backfill. NULL = never processed.';

CREATE INDEX IF NOT EXISTS idx_sites_source_status ON staging.sites(source_status);

-- ── 2. Expose it in the query view ───────────────────────────────────────────
-- Appends two columns at the end; every existing column keeps its position, so
-- CREATE OR REPLACE stays legal and the NL-query layer is unaffected.

CREATE OR REPLACE VIEW public.v_qualified_contacts AS
WITH addr AS (
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
    ag.group_key AS shared_address_group,
    ag.entities  AS shared_address_entities,
    -- new in 011
    s.source_status,
    (s.source_status IS NOT NULL
     AND (s.source_status ILIKE 'Ferm%'
          OR s.source_status ILIKE 'Cess%'
          OR s.source_status ILIKE 'Radi%')) AS source_closed
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
dedupe on it at send time to avoid mailing the same decision-maker twice.
source_closed flags businesses the SOURCE FILE reported as closed; they are still
returned here on purpose — filter them at export time (m1_s8_export.py
--exclude-closed) rather than hiding them from the base.';
