-- 018_multi_sector_contact_points.sql
-- M4 -- the schema upgrade that must land BEFORE any second sector is loaded.
--
-- WHY (audit of 2026-09-07, docs/m4_ingestion_plan.md):
--   1. Sector lived only on staging.source_files.sector. Companies, sites,
--      contacts, emails and every public view had no sector, and
--      `INSERT ... ON CONFLICT (siren) DO UPDATE` never touched source_file_id.
--      So a business present in two sectors' files kept the FIRST sector's
--      attribution and the second sector's qualification pass never saw it.
--      Measured: 336 SIRETs of the new tourism file are already in the
--      agriculture base (farm stays). Sector is MEMBERSHIP, not identity.
--   2. Contacts hold exactly two phone slots with no provenance. The M2/M3AG
--      deliverables carry per-phone source + corroboration, non-dialable
--      "piste" numbers, e-mail source/confidence, 2,267 (SIRET, domain)
--      website verdicts, social links, and 186 PROVEN-INVALID e-mails that
--      were deliberately withheld from the files and that the DB must remember
--      so they are never re-proposed. None of it had a home.
--   3. Businesses without a SIRET (imprimerie 9,182 rows, viticulteurs, 14k
--      tourism rows, 66 bio operators) had no idempotency key: every re-import
--      of a re-exported file would duplicate them.
--
-- WHAT: four additive tables + seven columns. Nothing existing is rewritten.
-- Every statement is IF NOT EXISTS / guarded, so the file is replayable.
--
-- REVERSAL (complete):
--   DROP TABLE staging.company_attributes, staging.company_identifiers,
--              staging.contact_points, staging.company_sources;
--   ALTER TABLE staging.sites     DROP COLUMN address_line2, DROP COLUMN website_url,
--       DROP COLUMN website_verdict, DROP COLUMN website_confidence,
--       DROP COLUMN website_checked_at;
--   ALTER TABLE staging.companies DROP COLUMN naf_code_source,
--       DROP COLUMN nature_juridique_code;
--   ALTER TABLE staging.emails    DROP COLUMN source;
--   ALTER TABLE raw.ingest_rows   DROP CONSTRAINT ingest_rows_source_file_fk;
--   DROP INDEX IF EXISTS staging.uq_contacts_site_fullname;

-- ---- 1. company_sources: which file(s) a company came from -------------------
-- One row per (company, file, source row). The SIREN merge path and the
-- no-SIRET fallback path both write here, so a company that appears in the
-- agriculture file AND the tourism file has two rows and belongs to both
-- sectors. m1_s5_qualify.py scopes on this table from M4 on, not on
-- companies.source_file_id (which stays as "first file that created the row").

CREATE TABLE IF NOT EXISTS staging.company_sources (
    company_id      UUID        NOT NULL REFERENCES staging.companies(id) ON DELETE CASCADE,
    source_file_id  UUID        NOT NULL REFERENCES staging.source_files(id),
    row_index       INTEGER     NOT NULL DEFAULT -1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, source_file_id, row_index)
);

COMMENT ON TABLE staging.company_sources IS
    'Membership of a company in a source file (and therefore in a sector). '
    'A company can have rows for several files. Written by every ingest path.';
COMMENT ON COLUMN staging.company_sources.row_index IS
    'raw.ingest_rows.row_index of the source row. -1 = backfilled by migration 018 '
    'for rows ingested before M4, whose row index was not tracked per company.';

CREATE INDEX IF NOT EXISTS idx_company_sources_file ON staging.company_sources(source_file_id);

-- Backfill: every existing company belongs to the file that created it.
INSERT INTO staging.company_sources (company_id, source_file_id, row_index)
SELECT id, source_file_id, -1 FROM staging.companies
ON CONFLICT DO NOTHING;

-- ---- 2. contact_points: every phone / e-mail / site / social CLAIM, with provenance
-- The multi-witness table the boulangerie and agriculteurs work proved is the
-- real shape of enrichment: many witnesses per fact, each with a source and a
-- confidence, ranked by MEASURED agreement. contacts.phone_main, staging.emails
-- and sites.website_domain remain the best-pick projection written by loaders.
-- A claim with is_dialable = FALSE (search snippets, one-witness "piste"
-- numbers, premium-rate lines) must never be promoted to phone_main; a
-- quality check enforces that.

CREATE TABLE IF NOT EXISTS staging.contact_points (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id      UUID        NOT NULL REFERENCES staging.companies(id) ON DELETE CASCADE,
    site_id         UUID        REFERENCES staging.sites(id)    ON DELETE SET NULL,
    contact_id      UUID        REFERENCES staging.contacts(id) ON DELETE SET NULL,
    kind            TEXT        NOT NULL
                    CHECK (kind IN ('phone','email','website','facebook','instagram','linkedin')),
    value_norm      TEXT        NOT NULL,   -- phone: 10-digit national; email: lower; website: domain; social: url
    value_raw       TEXT,                   -- exactly as the source wrote it
    source          TEXT        NOT NULL,   -- 'client_file:TELEPHONE', 'pagesjaunes', 'osm', 'agencebio', 'site/confirme', 'corrobore', ...
    confidence      TEXT,                   -- source vocabulary: confirme | probable | faible | piste | ...
    verdict         TEXT,                   -- email: valid|invalid|risky|unknown|malformed ; website: valide|annuaire|reseau|hors_sujet|mort|parked|non_verifiable
    is_dialable     BOOLEAN,                -- phones only: may this number reach phone_main?
    is_surtaxe      BOOLEAN,                -- phones only: premium-rate 08xx
    rank_hint       INTEGER,                -- the loader's PHONE_RANK / EMAIL_RANK position (lower = better)
    evidence        JSONB,                  -- witnesses, match method, distance_m, ownership, priority column, ...
    observed_at     DATE,
    source_file_id  UUID        NOT NULL REFERENCES staging.source_files(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (company_id, kind, value_norm, source)
);

COMMENT ON TABLE staging.contact_points IS
    'One row per (business, kind, value, source) claim. Keeps every witness; '
    'the best pick is a projection, never the only copy. Invalid/malformed '
    'e-mails and non-dialable phones live here so they are never re-proposed.';

CREATE INDEX IF NOT EXISTS idx_contact_points_company ON staging.contact_points(company_id);
CREATE INDEX IF NOT EXISTS idx_contact_points_value   ON staging.contact_points(kind, value_norm);
CREATE INDEX IF NOT EXISTS idx_contact_points_source  ON staging.contact_points(source);

-- ---- 3. company_identifiers: non-SIREN keys ---------------------------------
-- numero_bio (Agence Bio, unique per operator), pj_listing_id (Pages Jaunes),
-- file_row ('<file_hash>:<row_index>' for rows with no other key). This is the
-- idempotency key for businesses without a SIRET, and the correct model for
-- "two bio operators registered on one SIRET" (one company, two identifiers).

CREATE TABLE IF NOT EXISTS staging.company_identifiers (
    company_id      UUID        NOT NULL REFERENCES staging.companies(id) ON DELETE CASCADE,
    id_type         TEXT        NOT NULL,
    id_value        TEXT        NOT NULL,
    source_file_id  UUID        NOT NULL REFERENCES staging.source_files(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id_type, id_value)
);

COMMENT ON TABLE staging.company_identifiers IS
    'External identifiers other than SIREN/SIRET. UNIQUE per (type, value): '
    'looking one up is how a loader finds an existing no-SIRET company.';

CREATE INDEX IF NOT EXISTS idx_company_identifiers_company ON staging.company_identifiers(company_id);

-- ---- 4. company_attributes: the sector-specific long tail -------------------
-- Bio certificates and their AB/conversion state, productions, sales channels,
-- the provider's EFFECTIF as written, SOURCE_FILE, flag_hors_agri,
-- procedure_collective. JSONB on purpose: no per-sector columns, and the
-- NL-query layer can read JSON.

CREATE TABLE IF NOT EXISTS staging.company_attributes (
    company_id      UUID        NOT NULL REFERENCES staging.companies(id) ON DELETE CASCADE,
    sector          TEXT        NOT NULL,
    attrs           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    source_file_id  UUID        NOT NULL REFERENCES staging.source_files(id),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, sector)
);

COMMENT ON TABLE staging.company_attributes IS
    'Sector-specific facts that do not deserve a column. One JSON document per '
    '(company, sector). Loaders merge with || so a re-run never loses keys.';

CREATE INDEX IF NOT EXISTS idx_company_attributes_attrs ON staging.company_attributes USING gin(attrs);

-- ---- 5. Small columns -------------------------------------------------------

ALTER TABLE staging.sites
    ADD COLUMN IF NOT EXISTS address_line2       TEXT,
    ADD COLUMN IF NOT EXISTS website_url         TEXT,
    ADD COLUMN IF NOT EXISTS website_verdict     TEXT,
    ADD COLUMN IF NOT EXISTS website_confidence  TEXT,
    ADD COLUMN IF NOT EXISTS website_checked_at  TIMESTAMPTZ;

COMMENT ON COLUMN staging.sites.website_verdict IS
    'Per-(SIRET, domain) validation verdict from the site validator: valide | '
    'non_verifiable | annuaire | reseau | hors_sujet | parked | mort. Only '
    'valide/non_verifiable may ship. NULL = never validated (pre-M4 rows).';

ALTER TABLE staging.companies
    ADD COLUMN IF NOT EXISTS naf_code_source        CHAR(6),
    ADD COLUMN IF NOT EXISTS nature_juridique_code  CHAR(4);

COMMENT ON COLUMN staging.companies.naf_code_source IS
    'NAF code as the SOURCE FILE wrote it. Never copied into naf_code (owned by '
    'the SIRENE enrichment) nor into naf_label (a free-text label the tier-2 '
    'regexes run against). Qualification may fall back to it when naf_code is NULL.';
COMMENT ON COLUMN staging.companies.nature_juridique_code IS
    'INSEE nature juridique code (e.g. 5710). legal_form holds the label; the code '
    'makes relabelling a one-line re-derivation (migration 016 lesson).';

ALTER TABLE staging.emails
    ADD COLUMN IF NOT EXISTS source TEXT;

COMMENT ON COLUMN staging.emails.source IS
    'Where the address came from: NULL = client Excel (pre-M4), else the loader '
    'vocabulary (site/confirme, agencebio, pagesjaunes, rdap, pattern, ...).';

-- ---- 6. The FK migration 001 only wrote in a comment ------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ingest_rows_source_file_fk') THEN
        ALTER TABLE raw.ingest_rows
            ADD CONSTRAINT ingest_rows_source_file_fk
            FOREIGN KEY (source_file_id) REFERENCES staging.source_files(id) NOT VALID;
        ALTER TABLE raw.ingest_rows VALIDATE CONSTRAINT ingest_rows_source_file_fk;
    END IF;
END;
$$;

-- ---- 7. Contacts: the key the ingest script always assumed ------------------
-- m1_s3 keys contacts on (site_id, full_name) in Python but nothing in the DB
-- enforced it. Create the unique index only if the live data has no violation;
-- otherwise say so and leave it to a later repair. Replayable either way.
DO $$
DECLARE dup_count INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'staging'
                   AND indexname = 'uq_contacts_site_fullname') THEN
        SELECT count(*) INTO dup_count FROM (
            SELECT site_id, full_name FROM staging.contacts
            WHERE full_name IS NOT NULL
            GROUP BY site_id, full_name HAVING count(*) > 1) d;
        IF dup_count = 0 THEN
            CREATE UNIQUE INDEX uq_contacts_site_fullname
                ON staging.contacts(site_id, full_name) WHERE full_name IS NOT NULL;
            RAISE NOTICE 'uq_contacts_site_fullname created';
        ELSE
            RAISE NOTICE 'uq_contacts_site_fullname NOT created: % duplicate (site_id, full_name) groups', dup_count;
        END IF;
    END IF;
END;
$$;

-- ---- 8. RLS, same shape as 014 (replayable, consults pg_policies) -----------
ALTER TABLE staging.company_sources     ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging.contact_points      ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging.company_identifiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE staging.company_attributes  ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('staging','company_sources'), ('staging','contact_points'),
        ('staging','company_identifiers'), ('staging','company_attributes')
    ) AS t(sch, tbl)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_policies
                       WHERE schemaname = r.sch AND tablename = r.tbl
                         AND policyname = 'service_role_all') THEN
            EXECUTE format('CREATE POLICY "service_role_all" ON %I.%I FOR ALL USING (true)',
                           r.sch, r.tbl);
        END IF;
    END LOOP;
END;
$$;

-- Verification:
--   SELECT count(*) FROM staging.company_sources;            -- = count(*) FROM staging.companies
--   SELECT count(*) FROM staging.company_sources cs
--     JOIN staging.companies c ON c.id = cs.company_id
--    WHERE cs.source_file_id <> c.source_file_id;            -- 0 right after backfill
--   SELECT conname, convalidated FROM pg_constraint WHERE conname = 'ingest_rows_source_file_fk';
--   SELECT schemaname, tablename FROM pg_policies WHERE policyname = 'service_role_all'; -- 11 rows
