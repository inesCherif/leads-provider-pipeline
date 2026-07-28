-- 012_enrichment_queue.sql
-- M1-S9b — one definition of "a business in our deliverable", and a work queue
-- built on it that every enrichment step reads from.
--
-- WHY: m1_s8_export.py collapses v_qualified_contacts to one row per business
-- with two DISTINCT ON passes (best contact per business, then one entity per
-- multi-entity farmer family). Every enrichment script needs exactly that same
-- population — and if each one re-implements the collapse, they drift, and the
-- scraper ends up working on businesses the export never ships. So the collapse
-- moves here, once, and the export reads it too.
--
-- Two views:
--   v_deliverable_businesses  the collapsed population, one row per business
--   v_enrichment_queue        the same rows + what each one still needs
--
-- Neither view writes anything. Both are safe to replace.

-- ── 1. The deliverable population ────────────────────────────────────────────

CREATE OR REPLACE VIEW public.v_deliverable_businesses AS
WITH per_business AS (
    -- Best contact per business. The LATERAL in v_qualified_contacts already
    -- picked the best email within a contact; this picks the best contact.
    SELECT DISTINCT ON (business_id) *
    FROM public.v_qualified_contacts
    WHERE NOT is_duplicate
    ORDER BY
        business_id,
        -- never let a site the source reported closed represent a business
        -- that also has a live one
        source_closed ASC,
        (email_address IS NOT NULL) DESC,
        (tier = 'tier1_official_naf') DESC,
        email_verified DESC NULLS LAST,
        -- no contact_id is exposed, so tiebreak on the contact's own fields
        full_name NULLS LAST, email_address NULLS LAST, phone_main NULLS LAST,
        -- Final, total tiebreak. A company with several establishments can have
        -- two sites identical on every column above (same contact, same phone,
        -- same email) and differing only in the SIRET's NIC. Without this the
        -- winner depends on the query plan, so the same data exports different
        -- bytes run to run — caught 2026-07-28 when refactoring this collapse
        -- into a view flipped one row from ...00017 to ...00025.
        siret NULLS LAST
),
per_family AS (
    -- One entity per address-sharing family. A farmer often operates as sole
    -- trader + EARL + SCI at one address; they share a decision-maker, so the
    -- campaign contacts them once. Businesses with no shared address are their
    -- own group via the COALESCE.
    SELECT DISTINCT ON (COALESCE(shared_address_group, business_id::text)) *
    FROM per_business
    ORDER BY
        COALESCE(shared_address_group, business_id::text),
        source_closed ASC,
        (email_address IS NOT NULL) DESC,
        (tier = 'tier1_official_naf') DESC,
        business_id
)
SELECT
    *,
    -- legal_name is only populated where SIRENE supplied it: every tier-2 row
    -- (no SIREN, so no official denomination) plus ~7,700 tier-1 ones lack it,
    -- but all carry the source file's trade_name. Exactly 1 business in the
    -- whole base has neither.
    COALESCE(NULLIF(btrim(legal_name), ''), trade_name) AS display_name
FROM per_family;

COMMENT ON VIEW public.v_deliverable_businesses IS
'One row per real business, after dropping duplicates and collapsing multi-entity
farmer families. THE definition of the deliverable population — m1_s8_export.py
and every M1-S9 enrichment step read from here so they cannot drift apart.
Closed businesses are included and flagged (source_closed); filter at use time.';

-- ── 2. The enrichment work queue ─────────────────────────────────────────────
-- Excludes businesses the source reported closed: crawl budget and API quota
-- should never be spent on them. Everything else is classified by what it still
-- needs, so each M1-S9 script selects its own slice with a single WHERE.

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
        lower(NULLIF(split_part(COALESCE(email_address, ''), '@', 2), '')) AS email_domain
    FROM public.v_deliverable_businesses
    WHERE NOT source_closed
),
flagged AS (
    SELECT
        *,
        (btrim(COALESCE(full_name, '')) <> '')  AS has_contact_name,
        (email_address IS NOT NULL)             AS has_email,
        -- A corporate domain is one we could actually build addresses on.
        -- 76.5 pct of our emails are personal ISP mailboxes (measured
        -- 2026-07-28) and no pattern can be generated for those: knowing
        -- someone uses orange.fr tells you nothing about their colleague.
        (email_domain IS NOT NULL AND email_domain NOT IN (
            'orange.fr','gmail.com','wanadoo.fr','hotmail.fr','yahoo.fr','free.fr',
            'sfr.fr','laposte.net','hotmail.com','aol.com','outlook.fr','live.fr',
            'bbox.fr','neuf.fr','yahoo.com','outlook.com','msn.com','gmailcom',
            'me.com','icloud.com','mail.com','gmx.fr','numericable.fr','dbmail.com'
        )) AS domain_is_corporate
    FROM base
)
SELECT
    *,
    -- The dirigeants array from recherche-entreprises.api.gouv.fr can only be
    -- fetched for a company we can identify, i.e. one that has a SIREN.
    (NOT has_contact_name AND siren IS NOT NULL) AS needs_dirigeant,
    -- No SIREN and no name: the only free lever left is parsing the business
    -- name itself, which for a sole trader is usually the person's name.
    (NOT has_contact_name AND siren IS NULL)     AS needs_name_parse,
    -- We know the domain, so its pattern can be mined and applied.
    (domain_is_corporate AND has_contact_name)   AS can_mine_pattern,
    -- Nothing to build an address on: this is the scraping target.
    (NOT domain_is_corporate)                    AS needs_domain,
    -- Priority for the expensive, rate-limited steps (domain discovery, site
    -- crawling). Ordered by how likely the business is to have a website at
    -- all — the binding constraint in agriculture, where most farms have none.
    CASE
        -- On-farm transformation, beverages, electricity generation: these sell
        -- to the public or to the grid, so they market themselves.
        WHEN naf_code ~ '^(10|11|35)' THEN 1
        -- 6+ employees. Big enough to have a web presence and a real org chart.
        WHEN employee_bracket ~ '^[0-9]+$' AND employee_bracket::int >= 3 THEN 1
        -- Incorporated forms: a company, not a sole trader.
        WHEN legal_form ~* '^(SAS|SARL|SA$|SCEA|COOP|SCA)' THEN 2
        -- Collective farm structures — often have a shared office and identity.
        WHEN legal_form ~* '^(EARL|GAEC)' THEN 3
        WHEN siren IS NOT NULL THEN 4
        ELSE 5
    END AS enrichment_priority
FROM flagged;

COMMENT ON VIEW public.v_enrichment_queue IS
'Work queue for M1-S9. One row per LIVE deliverable business (closed ones are
excluded — never spend API quota or crawl budget on them) with boolean columns
saying what each still needs: needs_dirigeant, needs_name_parse, can_mine_pattern,
needs_domain. enrichment_priority 1..5 orders the rate-limited scraping steps by
how likely the business is to have a website at all.';
