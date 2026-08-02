-- =============================================================================
-- Migration 001: Initial Schema
-- Project : leads-provider-data-pipeline
-- Applied to: Supabase (PostgreSQL)
-- Idempotent: yes — all CREATE statements use IF NOT EXISTS
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";        -- uuid_generate_v4()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";          -- fuzzy string matching for dedup fallback
CREATE EXTENSION IF NOT EXISTS "unaccent";         -- accent-insensitive matching (French names)

-- ---------------------------------------------------------------------------
-- 2. Schemas
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS raw;      -- untouched ingested rows
CREATE SCHEMA IF NOT EXISTS staging;  -- normalized, deduped, enriched records
CREATE SCHEMA IF NOT EXISTS audit;    -- append-only change log

-- ---------------------------------------------------------------------------
-- 3. AUDIT SCHEMA
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit.audit_log (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    table_name      TEXT        NOT NULL,
    record_id       UUID,                         -- which row changed (NULL for bulk ops)
    field_changed   TEXT,                         -- NULL = row-level event (insert/delete)
    old_value       TEXT,
    new_value       TEXT,
    changed_by      TEXT        NOT NULL,         -- script name or Supabase auth user
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason          TEXT                          -- human-readable explanation
);

COMMENT ON TABLE audit.audit_log IS
    'Append-only log of all meaningful state changes. Never delete rows here.';

-- ---------------------------------------------------------------------------
-- 4. RAW SCHEMA — one table, stores every imported row as JSONB
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS raw.ingest_rows (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_file_id  UUID        NOT NULL,         -- FK set after source_files insert
    row_index       INTEGER     NOT NULL,         -- 0-based row number in source file
    raw_json        JSONB       NOT NULL,         -- original row, all columns preserved
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_file_id, row_index)
);

COMMENT ON TABLE raw.ingest_rows IS
    'Every source row stored verbatim as JSON. Never modified after insert.';

-- ---------------------------------------------------------------------------
-- 5. STAGING SCHEMA
-- ---------------------------------------------------------------------------

-- ── 5.1 source_files ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.source_files (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    file_name               TEXT        NOT NULL,
    file_hash               CHAR(64)    NOT NULL UNIQUE, -- SHA-256; prevents re-importing same file
    collection_date_approx  DATE,                        -- best-known data collection date; NULL if unknown
    import_date             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    row_count_raw           INTEGER,                     -- rows in source file before any filter
    row_count_imported      INTEGER,                     -- rows successfully written to raw.ingest_rows
    sector                  TEXT,                        -- e.g. 'livestock', 'agrifood', 'bakeries'
    pipeline_stage          TEXT        NOT NULL         -- 'raw' | 'siret_matched' | 'verified' | 'unknown'
                            CHECK (pipeline_stage IN ('raw','siret_matched','verified','unknown')),
    notes                   TEXT,                        -- human observations about this file
    imported_by             TEXT        NOT NULL DEFAULT 'system'
);

COMMENT ON TABLE staging.source_files IS
    'Traceability anchor. Every record in every other table has a FK to this table.';
COMMENT ON COLUMN staging.source_files.file_hash IS
    'SHA-256 of the raw file bytes. Used to detect re-imports of the same file.';
COMMENT ON COLUMN staging.source_files.pipeline_stage IS
    'Where in the enrichment pipeline this file sits: raw input, SIRET-matched, or email-verified.';

-- ── 5.2 companies ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.companies (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    siren                   CHAR(9)     UNIQUE,           -- NULL only when unknown; dedup anchor at company level
    legal_name              TEXT,
    trade_name              TEXT,                         -- common/commercial name if different from legal
    legal_form              TEXT,                         -- SARL, SAS, EARL, GAEC, EI, etc.
    naf_code                CHAR(6),                      -- main APE/NAF code (e.g. '01.41Z')
    naf_label               TEXT,
    employee_bracket        TEXT,                         -- SIRENE tranche: '0','1','2','11','21', etc.
    -- Multi-site / group support (NULL for simple single-site companies)
    group_parent_siren      CHAR(9),                      -- FK self-ref to companies.siren
    is_group_head           BOOLEAN     NOT NULL DEFAULT FALSE,
    -- Pipeline state
    qualification_status    TEXT        NOT NULL DEFAULT 'unqualified'
                            CHECK (qualification_status IN ('unqualified','qualified','disqualified','pending')),
    disqualification_reason TEXT,                         -- e.g. 'multi-site centralized group'
    -- Traceability
    source_file_id          UUID        NOT NULL REFERENCES staging.source_files(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE staging.companies IS
    'One row per legal entity (SIREN level). Single-site and multi-site groups both live here.';
COMMENT ON COLUMN staging.companies.group_parent_siren IS
    'Populated for subsidiaries/branches. NULL for independent single-entity companies.';
COMMENT ON COLUMN staging.companies.employee_bracket IS
    'INSEE tranche effectifs code. 0=0 employees, 1=1-2, 2=3-5, 11=10-19, 21=20-49, etc.';

CREATE INDEX IF NOT EXISTS idx_companies_siren    ON staging.companies(siren);
CREATE INDEX IF NOT EXISTS idx_companies_naf      ON staging.companies(naf_code);
CREATE INDEX IF NOT EXISTS idx_companies_qual     ON staging.companies(qualification_status);
CREATE INDEX IF NOT EXISTS idx_companies_group    ON staging.companies(group_parent_siren);
-- Trigram index for fuzzy name dedup fallback
CREATE INDEX IF NOT EXISTS idx_companies_name_trgm ON staging.companies USING gin(legal_name gin_trgm_ops);

-- ── 5.3 sites ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.sites (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    siret                   CHAR(14)    UNIQUE,           -- NULL only when absent from source
    company_id              UUID        NOT NULL REFERENCES staging.companies(id) ON DELETE CASCADE,
    is_headquarters         BOOLEAN     NOT NULL DEFAULT TRUE,
    is_active               BOOLEAN,                      -- NULL = unknown; FALSE = closed per SIRENE
    -- Address
    address_line1           TEXT,
    postal_code             CHAR(5),
    city                    TEXT,
    department              CHAR(3),                      -- '01'..'976', including DOM-TOM
    region                  TEXT,
    latitude                NUMERIC(9,6),
    longitude               NUMERIC(9,6),
    -- Site-level activity (may differ from parent company)
    naf_code_site           CHAR(6),
    employee_bracket_site   TEXT,
    -- Enrichment fields (filled by later pipeline stages)
    website_domain          TEXT,
    email_format_pattern    TEXT,                         -- e.g. '{first}.{last}@{domain}'
    -- SIRENE refresh tracking
    sirene_last_checked_at  TIMESTAMPTZ,
    -- Traceability
    source_file_id          UUID        NOT NULL REFERENCES staging.source_files(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE staging.sites IS
    'One row per physical establishment (SIRET level). A company with 3 locations = 3 rows here.';
COMMENT ON COLUMN staging.sites.is_active IS
    'NULL = status unknown. FALSE = confirmed closed by SIRENE. Never delete closed sites.';
COMMENT ON COLUMN staging.sites.email_format_pattern IS
    'Discovered email pattern, e.g. {first}.{last}@{domain}. Used by email generation stage.';

CREATE INDEX IF NOT EXISTS idx_sites_siret      ON staging.sites(siret);
CREATE INDEX IF NOT EXISTS idx_sites_company    ON staging.sites(company_id);
CREATE INDEX IF NOT EXISTS idx_sites_postal     ON staging.sites(postal_code);
CREATE INDEX IF NOT EXISTS idx_sites_dept       ON staging.sites(department);
CREATE INDEX IF NOT EXISTS idx_sites_active     ON staging.sites(is_active);

-- ── 5.4 contacts ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.contacts (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    site_id                 UUID        NOT NULL REFERENCES staging.sites(id) ON DELETE CASCADE,
    company_id              UUID        NOT NULL REFERENCES staging.companies(id), -- denormalized for query speed
    -- Identity
    first_name              TEXT,
    last_name               TEXT,
    full_name               TEXT,                         -- raw name when first/last not split
    -- Role
    job_title               TEXT,                         -- raw title from source
    job_function            TEXT                          -- normalized: 'owner'|'site_director'|'energy_manager'|'maintenance'|'generic'
                            CHECK (job_function IS NULL OR job_function IN
                                ('owner','site_director','energy_manager','maintenance','technical_director','generic','unknown')),
    -- Contact info
    phone_main              TEXT,
    phone_alt               TEXT,
    -- Flags
    is_generic_contact      BOOLEAN     NOT NULL DEFAULT FALSE, -- TRUE = company email, no named person
    -- Pipeline state
    enrichment_status       TEXT        NOT NULL DEFAULT 'not_started'
                            CHECK (enrichment_status IN
                                ('not_started','domain_found','candidates_generated','verified','purchased')),
    -- Traceability
    source_file_id          UUID        NOT NULL REFERENCES staging.source_files(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE staging.contacts IS
    'Named individuals (or generic company contacts) at a site. One site can have many contacts.';
COMMENT ON COLUMN staging.contacts.is_generic_contact IS
    'TRUE for small companies where only a company-level email is known, no named person.';
COMMENT ON COLUMN staging.contacts.enrichment_status IS
    'Tracks how far along the email enrichment pipeline this contact has progressed.';

CREATE INDEX IF NOT EXISTS idx_contacts_site        ON staging.contacts(site_id);
CREATE INDEX IF NOT EXISTS idx_contacts_company     ON staging.contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_enrichment  ON staging.contacts(enrichment_status);

-- ── 5.5 emails ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.emails (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    contact_id              UUID        NOT NULL REFERENCES staging.contacts(id) ON DELETE CASCADE,
    email_address           TEXT        NOT NULL,
    is_primary              BOOLEAN     NOT NULL DEFAULT FALSE, -- best/confirmed address for this contact
    -- Verification
    verification_status     TEXT        NOT NULL DEFAULT 'candidate'
                            CHECK (verification_status IN ('candidate','valid','invalid','risky','unknown','purchased')),
    verified_at             TIMESTAMPTZ,
    verifier_tool           TEXT,                         -- e.g. 'millionverifier','hunter','manual'
    -- Traceability
    source_file_id          UUID        NOT NULL REFERENCES staging.source_files(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (contact_id, email_address)
);

COMMENT ON TABLE staging.emails IS
    'Email candidates per contact. Multiple candidates (pattern variants) per contact are normal.';
COMMENT ON COLUMN staging.emails.is_primary IS
    'Only one row per contact should have is_primary=TRUE at any time.';

CREATE INDEX IF NOT EXISTS idx_emails_contact   ON staging.emails(contact_id);
CREATE INDEX IF NOT EXISTS idx_emails_status    ON staging.emails(verification_status);
CREATE INDEX IF NOT EXISTS idx_emails_primary   ON staging.emails(contact_id) WHERE is_primary = TRUE;

-- ── 5.6 campaigns (future-ready, lightweight now) ────────────────────────────
CREATE TABLE IF NOT EXISTS staging.campaigns (
    id          UUID    PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT    NOT NULL,
    client      TEXT,
    sector      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS staging.campaign_contacts (
    campaign_id UUID    NOT NULL REFERENCES staging.campaigns(id),
    contact_id  UUID    NOT NULL REFERENCES staging.contacts(id),
    status      TEXT    NOT NULL DEFAULT 'included'
                CHECK (status IN ('included','sent','bounced','replied','unsubscribed')),
    sent_at     TIMESTAMPTZ,
    response_at TIMESTAMPTZ,
    PRIMARY KEY (campaign_id, contact_id)
);

COMMENT ON TABLE staging.campaigns IS
    'Prospecting campaign definitions. Lightweight placeholder — extend as needed.';

-- ---------------------------------------------------------------------------
-- 6. PUBLIC SCHEMA — read-only views for natural-language querying via MCP
-- ---------------------------------------------------------------------------

-- Drop and recreate views (views are always replaceable)
CREATE OR REPLACE VIEW public.v_qualified_contacts AS
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
    e.is_primary AS email_is_primary
FROM staging.companies co
JOIN staging.sites     s  ON s.company_id = co.id
JOIN staging.contacts  c  ON c.site_id    = s.id
LEFT JOIN staging.emails e ON e.contact_id = c.id AND e.is_primary = TRUE
WHERE co.qualification_status = 'qualified'
  AND s.is_active IS DISTINCT FROM FALSE;  -- include NULL (unknown) but exclude confirmed closed

COMMENT ON VIEW public.v_qualified_contacts IS
    'Main query view for the AI/MCP layer. Returns qualified, non-closed contacts with their best email.';

CREATE OR REPLACE VIEW public.v_pipeline_summary AS
SELECT
    sf.sector,
    sf.pipeline_stage,
    co.qualification_status,
    c.enrichment_status,
    COUNT(DISTINCT co.id) AS company_count,
    COUNT(DISTINCT s.id)  AS site_count,
    COUNT(DISTINCT c.id)  AS contact_count,
    COUNT(DISTINCT e.id) FILTER (WHERE e.verification_status = 'valid') AS verified_email_count
FROM staging.companies co
JOIN staging.source_files sf ON sf.id = co.source_file_id
JOIN staging.sites     s  ON s.company_id = co.id
LEFT JOIN staging.contacts c  ON c.site_id = s.id
LEFT JOIN staging.emails   e  ON e.contact_id = c.id
GROUP BY sf.sector, sf.pipeline_stage, co.qualification_status, c.enrichment_status;

COMMENT ON VIEW public.v_pipeline_summary IS
    'Dashboard-style summary: counts by sector, pipeline stage, qualification, and enrichment status.';

-- ---------------------------------------------------------------------------
-- 7. Row-Level Security (RLS) — enable but permissive for now
--    Real RLS policies added once Supabase Auth users are configured
-- ---------------------------------------------------------------------------
ALTER TABLE staging.source_files    ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging.companies       ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging.sites           ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging.contacts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging.emails          ENABLE ROW LEVEL SECURITY;
ALTER TABLE raw.ingest_rows         ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.audit_log         ENABLE ROW LEVEL SECURITY;

-- Temporary open policies (service role bypasses RLS anyway; restrict later).
--
-- FIXED 2026-08-02. This block previously used `CREATE POLICY IF NOT EXISTS`,
-- which is NOT valid PostgreSQL — Postgres supports IF NOT EXISTS on many
-- object types but not on policies. The statement errored, so RLS ended up
-- ENABLED on every table with ZERO policies. Nothing broke in practice (we
-- connect as service_role, which bypasses RLS entirely), but it meant this
-- migration could not be replayed on a fresh database — quietly breaking the
-- "numbered migrations rebuild the schema" property the whole scheme relies on.
--
-- Idempotent the way Postgres actually supports: check the catalogue first.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('staging','source_files'), ('staging','companies'), ('staging','sites'),
        ('staging','contacts'),     ('staging','emails'),
        ('raw','ingest_rows'),      ('audit','audit_log')
    ) AS t(sch, tbl)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = r.sch AND tablename = r.tbl
              AND policyname = 'service_role_all'
        ) THEN
            EXECUTE format(
                'CREATE POLICY "service_role_all" ON %I.%I FOR ALL USING (true)',
                r.sch, r.tbl);
        END IF;
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- 8. Helper function: auto-update updated_at timestamp
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION staging.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- Attach to all tables that have updated_at
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['companies','sites','contacts'] LOOP
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON staging.%s
             FOR EACH ROW EXECUTE FUNCTION staging.set_updated_at()',
            t, t
        );
    END LOOP;
END;
$$;
