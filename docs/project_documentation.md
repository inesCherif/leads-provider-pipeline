# Leads Provider — Data Pipeline Documentation

> **Author**: Ines Cherif (Data Engineering Intern)
> **Last updated**: 2026-07-18
> **Repository**: [leads-provider-data-pipeline](https://github.com/inesCherif/leads-provider-data-pipeline)

---

## Table of Contents

1. [Business Context](#1-business-context)
2. [Architecture Overview](#2-architecture-overview)
3. [Database Design](#3-database-design)
   - [Why Three Schemas?](#31-why-three-schemas-raw--staging--audit)
   - [Table-by-Table Rationale](#32-table-by-table-rationale)
   - [Entity Relationship Diagram](#33-entity-relationship-diagram)
   - [Key Design Decisions](#34-key-design-decisions)
4. [Pipeline Stages — What We Did](#4-pipeline-stages--what-we-did)
   - [M1-S1: File Inspection](#m1-s1-file-inspection--classification)
   - [M1-S2: Schema Deployment](#m1-s2-schema-deployment)
   - [M1-S3: Raw Ingest](#m1-s3-raw-ingest--normalize--deduplicate)
   - [M1-S4: SIRENE Enrichment](#m1-s4-sirene-enrichment)
5. [Investigations & Findings](#5-investigations--findings)
   - [NAF Code / Label Mismatch](#51-naf-code-vs-naf_label-mismatch-investigation)
6. [Current Data Profile](#6-current-data-profile)
7. [What's Next — Roadmap](#7-whats-next--roadmap)
8. [Tech Stack](#8-tech-stack)
9. [How to Run](#9-how-to-run)

---

## 1. Business Context

The company is a **B2B lead-generation business**. It sells lists of qualified prospects to a client in the French energy sector — one of the largest utility companies in France — whose products are **energy-efficiency and decarbonization services** (industrial process efficiency, heat recovery, heat pumps, energy audits, energy-savings certificates).

### The Problem

Before this project, the prospecting workflow was **entirely manual**:

- Prospect data lived in scattered Excel files on a CEO's machine
- No single source of truth; duplicates across files were common
- SIRET/SIREN verification was done by hand
- No systematic enrichment with official INSEE data
- Contact email discovery relied on guesswork and individual LinkedIn lookups
- Delivery to the client was ad-hoc CSV exports

### The Goal

Build a **modular, zero-budget data pipeline** that:

1. **Ingests** raw prospect spreadsheets from any source
2. **Normalizes & deduplicates** using SIRET as the canonical anchor
3. **Enriches** with official French business registry data (SIRENE)
4. **Qualifies** prospects via sector-specific rules
5. **Discovers** contact emails and verifies them in batch
6. **Exports** clean, delivery-ready lists
7. Makes the full dataset **queryable in natural language** via Supabase MCP

### Target Sectors

The client targets French businesses that are **heavy energy consumers**:

| Sector | Examples | NAF Codes |
|--------|----------|-----------|
| Agriculture / Livestock | Farms, dairy, poultry, horse breeders | 01.xx |
| Agrifood Processing | Slaughterhouses, cheese factories, bottlers | 10.xx, 11.xx |
| Bakeries | Industrial bakeries | 10.71x |
| Hotels / Spas | Hotels with heating/cooling needs | 55.xx |
| Clinics / Healthcare | Private clinics, labs | 86.xx |

The **pilot scope** focuses on **agriculture and livestock** (farmers + eleveurs).

---

## 2. Architecture Overview

```
+--------------------------------------------------------------------+
|                        DATA SOURCES                                |
|  +-----------+  +---------------+  +------------+  +------------+  |
|  | CEO Excel |  | Web Scraping  |  | INSEE API  |  | Future:    |  |
|  | Files     |  | (future)      |  | (SIRENE)   |  | LinkedIn,  |  |
|  +-----+-----+  +-------+------+  +------+-----+  | Hunter.io  |  |
|        |                |                |         +------------+  |
+--------+----------------+----------------+-------------------------+
         |                |                |
         v                v                v
+--------------------------------------------------------------------+
|                     PYTHON ETL SCRIPTS                             |
|                                                                    |
|  m1_s1_inspect -> m1_s3_ingest -> m1_s4_enrich -> m1_s5_qualify    |
|  (read-only)     (normalize+     (SIRENE API     (business         |
|                   dedup)          enrichment)     rules)           |
+----------------------------+---------------------------------------+
                             |
                             v
+--------------------------------------------------------------------+
|                   SUPABASE (PostgreSQL)                             |
|                                                                    |
|  +--------------+  +---------------+  +---------------+            |
|  |  raw.*       |  |  staging.*    |  |  audit.*      |            |
|  | (verbatim    |  | (normalized,  |  | (change log,  |            |
|  |  JSON rows)  |  |  enriched,    |  |  append-only) |            |
|  |              |  |  queryable)   |  |               |            |
|  +--------------+  +-------+-------+  +---------------+            |
|                            |                                       |
|                    +-------+-------+                               |
|                    |  public.*     |                               |
|                    | (read-only    |                               |
|                    |  views)       |                               |
|                    +---------------+                               |
+----------------------------+---------------------------------------+
                             |
                             v
+--------------------------------------------------------------------+
|                 QUERY / DELIVERY LAYER                              |
|  +-------------------+  +-------------------+                      |
|  | Supabase MCP +    |  | CSV/Excel Export  |                      |
|  | Claude / Cursor    |  | for client        |                      |
|  | (NL queries)      |  | delivery          |                      |
|  +-------------------+  +-------------------+                      |
+--------------------------------------------------------------------+
```

### Why This Architecture?

| Decision | Rationale |
|----------|-----------|
| **Supabase (free tier)** | Zero budget constraint. Gives us managed PostgreSQL, a REST API, and MCP integration out of the box |
| **Python scripts (not Airflow/dbt)** | Intern project with one developer — orchestration overhead is unjustified. Scripts are independently re-runnable |
| **Three schemas** | Clean separation of concerns: immutable raw data, mutable staging, append-only audit trail |
| **SIRET as dedup anchor** | The SIRET is France's universal business establishment identifier. No two businesses share the same SIRET |
| **Async enrichment (aiohttp)** | The SIRENE API rate-limits to ~4 req/sec. Async lets us saturate the limit without blocking |

---

## 3. Database Design

### 3.1 Why Three Schemas? (raw -> staging -> audit)

```
raw.*       ->  "What did the source file actually say?"
staging.*   ->  "What do we believe to be true right now?"
audit.*     ->  "What changed, when, and why?"
```

This three-layer pattern is a standard data engineering practice:

- **`raw`** is **immutable**. Every row from every source file is stored verbatim as JSON. If we ever need to re-derive staging data (e.g., after fixing a bug in the normalization logic), we can replay from raw without needing the original Excel files.
- **`staging`** is **mutable**. This is where normalization, deduplication, enrichment, and qualification happen. It's the "working copy" of the data.
- **`audit`** is **append-only**. Every meaningful state change (qualification decision, enrichment update, manual correction) is logged with who, when, and why. This is critical for traceability — the client needs to know where each lead came from.

### 3.2 Table-by-Table Rationale

#### `raw.ingest_rows`

| Column | Purpose |
|--------|---------|
| `source_file_id` | Links back to which file this row came from |
| `row_index` | Preserves original row order (0-based) |
| `raw_json` | The entire original row as JSONB — every column, no transformation |
| `UNIQUE(source_file_id, row_index)` | Prevents re-importing the same file twice |

**Why JSONB?** Source files have different column structures (the "agriculteurs" file has different headers than the "eleveurs" file). Storing as JSONB means we don't need a different raw table per source format.

---

#### `staging.source_files`

| Column | Purpose |
|--------|---------|
| `file_hash` (SHA-256) | **Idempotency guard** — if the exact same file is re-imported, it's rejected |
| `pipeline_stage` | Classifies the file: `raw`, `siret_matched`, or `verified` |
| `sector` | Which business sector this file targets (e.g., `agriculture`, `livestock`) |
| `collected_at` | The real-world date the data was collected — **NOT** the DB insertion date |
| `data_source` | Who provided this file (e.g., `CEO pilot files`, `web scrape`) |

**Why track `collected_at` separately from `created_at`?** The CEO gave us files on July 5, 2026, but we ingested them on July 12. If we only had `created_at`, we'd lose the provenance timeline. The client may ask "how fresh is this data?" — `collected_at` answers that.

---

#### `staging.companies`

This is the **company-level** table, keyed on `SIREN` (the 9-digit French legal entity identifier).

| Column | Source | Purpose |
|--------|--------|---------|
| `siren` | Derived from SIRET (first 9 digits) | **Primary dedup anchor** — UNIQUE constraint |
| `legal_name` | SIRENE API or source file | Official legal name from INSEE |
| `trade_name` | Source file | Commercial/trade name as known by the data provider |
| `naf_code` | SIRENE API | Official INSEE activity code (e.g., `01.41Z`) |
| `naf_label` | Source file `Activite` column | Free-text activity description from the data provider (see Investigation section) |
| `legal_form` | SIRENE API (decoded) | e.g., `EARL`, `GAEC`, `SAS`, `Entrepreneur individuel` |
| `employee_bracket` | SIRENE API | INSEE employee count bracket code |
| `creation_date` | SIRENE API | Legal entity creation date |
| `sirene_etat` | SIRENE API | `A` = active, `F` = ferme (closed) |
| `sirene_last_checked_at` | Set by enrichment script | **Idempotency checkpoint** — NULL = not yet enriched |
| `qualification_status` | Set by qualification rules | `unqualified`, `qualified`, `disqualified`, `pending` |
| `disqualification_reason` | Set by qualification rules | e.g., `closed business`, `public administration` |

**Why SIREN, not SIRET?** A SIREN identifies a **legal entity** (the company). A SIRET identifies a **physical establishment** (a specific location of that company). One company can have multiple SIRETs. We deduplicate at the company level (SIREN) because the client wants unique legal entities, not duplicate entries for each branch.

**Why `sirene_last_checked_at`?** This column serves as an **idempotency anchor** for the enrichment pipeline. If the script crashes mid-run, we can restart it and it will skip already-enriched rows (where `sirene_last_checked_at IS NOT NULL`) and continue from where it left off.

---

#### `staging.sites`

This is the **establishment-level** table, keyed on `SIRET` (the 14-digit identifier).

| Column | Purpose |
|--------|---------|
| `siret` | **UNIQUE** — one row per physical location |
| `company_id` | FK to `staging.companies` — many sites can belong to one company |
| `is_headquarters` | Is this the company's main office? |
| `is_active` | NULL = unknown, FALSE = confirmed closed by SIRENE |
| `address_*` | Physical address fields |
| `website_domain` | Discovered later in the pipeline (email enrichment stage) |
| `email_format_pattern` | e.g., `{first}.{last}@{domain}` — used for email candidate generation |

**Why separate `companies` and `sites`?** A dairy farm (one SIREN) might have its main farm (SIRET 1) and a cheese-making facility (SIRET 2). The client may want to contact the energy manager at each location independently. The company-sites split supports this naturally.

---

#### `staging.contacts`

| Column | Purpose |
|--------|---------|
| `site_id` | FK to which physical establishment this person works at |
| `company_id` | Denormalized FK to company (avoids an extra JOIN in common queries) |
| `full_name` | Raw name from source (first/last split happens in enrichment) |
| `job_function` | Normalized role: `owner`, `energy_manager`, `maintenance`, etc. |
| `is_generic_contact` | TRUE when we only have a company-level phone/email, no named person |
| `enrichment_status` | Tracks progress: `not_started` -> `domain_found` -> `candidates_generated` -> `verified` |

---

#### `staging.emails`

| Column | Purpose |
|--------|---------|
| `contact_id` | FK to which person this email belongs to |
| `email_address` | The actual email |
| `is_primary` | Only one email per contact should be marked primary |
| `verification_status` | `candidate` -> `valid` / `invalid` / `risky` |
| `UNIQUE(contact_id, email_address)` | Prevents duplicate emails per contact |

**Why a separate `emails` table?** One contact can have multiple email candidates (e.g., `jean.dupont@farm.fr`, `j.dupont@farm.fr`, `jdupont@farm.fr`). Each candidate goes through verification independently. Storing them as rows (not columns) scales naturally.

---

#### `audit.audit_log`

| Column | Purpose |
|--------|---------|
| `table_name` | Which staging table was modified |
| `record_id` | The UUID of the modified row |
| `field_changed` | Which column changed (NULL for row-level events) |
| `old_value` / `new_value` | Before and after |
| `changed_by` | Script name or user identifier |
| `reason` | Human-readable explanation |

**This table is append-only.** No UPDATE, no DELETE. It's the full audit trail for compliance and traceability.

---

### 3.3 Entity Relationship Diagram

```
                        +------------------+
                        |  source_files    |
                        |  (provenance)    |
                        +--------+---------+
                                 |
              +------------------+------------------+
              |                  |                  |
              v                  v                  v
     +--------+------+   +------+------+    +------+------+
     | ingest_rows   |   | companies   |    |   sites     |
     | (raw JSON)    |   | (SIREN key) |    | (SIRET key) |
     +---------------+   +------+------+    +------+------+
                                |                  |
                                |    +-------------+
                                |    |
                                v    v
                          +-----+----+-----+
                          |   contacts     |
                          | (people at     |
                          |  sites)        |
                          +------+---------+
                                 |
                                 v
                          +------+---------+
                          |    emails      |
                          | (candidates   |
                          |  per contact) |
                          +---------------+

     +-------------------+       +---------------------+
     |   campaigns       +-------+ campaign_contacts   |
     | (delivery lists)  |       | (send tracking)     |
     +-------------------+       +---------------------+
```

**Relationships**:
- `companies` 1:N `sites` (one legal entity, many establishments)
- `sites` 1:N `contacts` (one location, many people)
- `contacts` 1:N `emails` (one person, many email candidates)
- `campaigns` M:N `contacts` (via `campaign_contacts` junction table)
- Every table has a FK to `source_files` for full provenance

### 3.4 Key Design Decisions

| Decision | Alternatives Considered | Why We Chose This |
|----------|------------------------|-------------------|
| UUID primary keys everywhere | Auto-increment integers | UUIDs are merge-safe if we ever consolidate multiple Supabase instances or import from external systems |
| COALESCE on UPDATE (never overwrite) | Last-write-wins | Data from SIRENE is authoritative for certain fields, but we don't want to overwrite a human-corrected value with an API value. COALESCE fills NULLs only |
| Partial index `idx_companies_not_enriched` | Full table scan | With 128k rows, a full scan on every enrichment run would be wasteful. The partial index (`WHERE sirene_last_checked_at IS NULL`) makes "find unenriched rows" instant |
| `pg_trgm` extension for fuzzy matching | Levenshtein / exact match only | French business names have accent variations, abbreviations (GAEC vs G.A.E.C.), and typos. Trigram similarity handles this robustly. **NOT YET IMPLEMENTED — see the correction below** |
| RLS enabled but permissive | No RLS | Future-proofing. When Supabase Auth users are configured, we tighten policies without schema changes. **The policies never actually applied — see the correction below** |

> [!WARNING]
> **Corrections (2026-07-19).** Three things this document previously described as
> working are not implemented. Recorded here so nobody builds on them by mistake:
>
> 1. **`pg_trgm` fuzzy dedup does not exist.** The extension is enabled
>    (`001:12`) and a GIN index is created (`001:118`), but **no query in the
>    repository uses either**. Companies without a valid SIRET are inserted with
>    no matching of any kind. That is ~83,344 rows — 65% of the database — which
>    are effectively undeduplicated. This is the largest gap between these docs
>    and the code.
> 2. **`audit.audit_log` was declared but never written to** until M1-S5 on
>    2026-07-19. The dedup-decision logging promised in
>    `implementation_plan_leads.md:257,372` was never implemented.
> 3. **`naf_label` is never populated from SIRENE.** The M1-S4 docstring and
>    section 4 below both claim it is extracted; `parse_api_result()` does not
>    return it. `naf_label` is *only ever* the free-text `Activite` column from
>    the source Excel. This matters a lot for qualification — see 5.1.
>
> Also: the RLS policies in `001:326` use `CREATE POLICY IF NOT EXISTS`, which is
> **not valid PostgreSQL in any version**. That block errored on apply, so RLS is
> enabled on all 7 tables with **zero policies**. Service role bypasses RLS so
> nothing is broken today, but anon/authenticated access is deny-all and 001
> cannot be replayed on a fresh database.

---

## 4. Pipeline Stages — What We Did

### M1-S1: File Inspection & Classification

**Script**: `scripts/m1_s1_inspect_files.py`
**Purpose**: Read-only analysis of the raw Excel files before any DB interaction.

**What it does**:
- Reads each file with `pandas` + `calamine` engine (fast for large XLSX)
- Reports: row count, column names, data types, null percentages, sample values
- Classifies each file's pipeline stage based on column presence

**Key findings from inspection**:

| File | Rows | Key Columns | Pipeline Stage |
|------|------|-------------|----------------|
| `Copie de agriculteurs total.xlsx` | ~50k | Societe, Siret, Activite, Email, CP | `raw` |
| `siret_results*.xlsx` | ~50k | Same + Siret_Verifie | `siret_matched` (pipeline artifact — **skipped**) |
| `Copie de Eleveurs_verified*.xlsx` | ~83k | Societe, Siret, Siret_Verifie, Activite | `verified` |

**Decision**: File B (`siret_results`) is an intermediate output from someone else's SIRET verification process. It was **skipped** to avoid double-counting — its data is already represented in File C (which has the verified SIRETs).

---

### M1-S2: Schema Deployment

**Migration**: `migrations/001_initial_schema.sql`

Deployed the full schema to Supabase:
- 3 schemas (`raw`, `staging`, `audit`)
- 8 tables with all constraints, indexes, comments
- 2 public views (`v_qualified_contacts`, `v_pipeline_summary`)
- RLS enabled with permissive policies
- `updated_at` trigger on mutable tables

**Additional Migrations**:
- `002_add_source_metadata.sql`: Added `collected_at` and `data_source` to `source_files`, backfilled pilot data
- `003_add_sirene_enrichment_columns.sql`: Added `sirene_last_checked_at`, `sirene_etat`, `creation_date` to `companies`

---

### M1-S3: Raw Ingest + Normalize + Deduplicate

**Script**: `scripts/m1_s3_ingest.py`

**What it does**:

1. **Registers** each source file in `staging.source_files` (SHA-256 hash prevents re-import)
2. **Stores raw rows** in `raw.ingest_rows` as JSONB (immutable archive)
3. **Normalizes** each row:
   - Cleans SIRET (removes spaces, dots, hyphens; validates 14-digit format)
   - Derives SIREN from SIRET (first 9 digits)
   - Normalizes business status (`actif` -> TRUE, `ferme`/`cesse` -> FALSE)
   - Cleans postal codes (handles float artifacts like `59000.0` -> `59000`)
4. **Deduplicates** companies on SIREN (UNIQUE constraint + `ON CONFLICT` with COALESCE)
5. **Bulk inserts** via `psycopg2.extras.execute_values` (critical for performance)

**Column mapping** (source Excel -> DB):

| Excel Column (agriculteurs) | Excel Column (eleveurs) | DB Column |
|-----------------------------|------------------------|-----------|
| `Societe` | `Societe` | `trade_name` |
| `Nom_Officiel` | `Nom_Officiel` | `legal_name` |
| `Siret` | `Siret_Verifie` | `siret` |
| `Activite` | `Activite` | `naf_label` |
| `CP` | `CP` | `postal_code` |

**Performance issues encountered and solved**:

| Problem | Symptom | Fix |
|---------|---------|-----|
| Single-row inserts | SSL crashes, DB locks, 45+ min runtime | Switched to `execute_values` (bulk) — down to seconds |
| Float postal codes | `psycopg2.errors.StringDataRightTruncation` | Added `clean_postal_code()` truncator |
| Missing unique constraint on contacts | `psycopg2.errors.InvalidColumnReference` on `ON CONFLICT` | Removed `ON CONFLICT` for contacts table (no natural key) |
| Slow Excel reading | Minutes to parse 80k rows | Switched to `calamine` engine |

---

### M1-S4: SIRENE Enrichment

**Script**: `scripts/m1_s4_sirene_enrich.py`
**API**: `https://recherche-entreprises.api.gouv.fr/search` (public, no API key required)

**What it does**:

For each company with a SIREN that hasn't been enriched yet (`sirene_last_checked_at IS NULL`):

1. Calls the public SIRENE API with the SIREN number
2. Extracts: `legal_name`, `naf_code`, `legal_form`, `employee_bracket`, `creation_date`, `sirene_etat`
3. Updates `staging.companies` — **only fills NULL fields** (never overwrites existing values)
4. Sets `sirene_last_checked_at = NOW()` as the idempotency checkpoint

**Design principles**:

| Principle | Implementation |
|-----------|---------------|
| **Idempotent** | `sirene_last_checked_at IS NULL` filter — already-enriched rows are skipped |
| **Non-destructive** | `COALESCE(existing, new)` — only fills blanks |
| **Resumable** | Commits every 500 rows. Kill and restart = continues from checkpoint |
| **Rate-limited** | `asyncio.Semaphore(2)` + `0.4s` delay per request -> ~3 req/sec (API allows ~4) |
| **Retry with backoff** | 3 retries with exponential backoff on 429/timeout/503 |

**Enrichment run results**:

| Metric | Value |
|--------|-------|
| Total companies with SIREN | 44,772 |
| Successfully enriched | ~42,910 (95.8%) |
| Not found in SIRENE | ~1,782 (very old / unregistered entities) |
| Errors (transient, will retry) | 15 |
| Total runtime | ~4 hours (2 runs due to a pagination bug fix mid-run) |

**Pagination bug found and fixed**: The initial query used `LIMIT %s OFFSET %s`, but since the `WHERE` clause filters on `sirene_last_checked_at IS NULL` (which shrinks as we process), the `OFFSET` caused rows to be skipped. Fixed by removing `OFFSET` entirely — the query itself naturally advances as rows are updated.

> [!NOTE]
> **Two defects in `m1_s4_sirene_enrich.py` found on 2026-07-19, not yet fixed.**
> - **Unvalidated API match.** The script queries
>   `recherche-entreprises.api.gouv.fr/search?q=<siren>`, a **full-text search**
>   endpoint, then accepts `results[0]` without checking the returned SIREN equals
>   the requested one. If ranking ever returns a different entity, that company is
>   enriched with another company's name, NAF code and legal form, then stamped
>   `sirene_last_checked_at` so it is never re-checked. Silent and permanent.
>   Worth an audit query before trusting enrichment further.
> - **`--dry-run` hangs forever** without `--limit`. The loop only advances because
>   rows get UPDATEd out of the `WHERE` predicate; a dry run writes nothing, so the
>   same 500 rows are returned indefinitely. The same hang occurs on sustained API
>   failure.

---

### M1-S5: Qualification (2026-07-19)

Classifies every company against per-sector rules. Config lives in
`config/sector_rules.py` — versioned in git rather than a DB table, so rule
changes are diffable and attributable. `RULE_VERSION` is the re-qualification
trigger: bump it and the next run reclassifies every affected row, no manual
reset.

**Two tiers.** The original spec sent every company without a SIREN to `pending`.
Measured against live data that would have shelved most of the usable dataset:
83,344 companies (65%) have no SIREN, but 83,326 have a contact, 83,324 have a
phone and 19,443 have an email — four times more reachable email than the entire
SIREN-verified agriculture segment. So they get their own tier instead:

| Tier | Status | Basis | Confidence |
|------|--------|-------|------------|
| 1 | `qualified` | Official INSEE NAF code in scope | High — SIREN-verified |
| 2 | `qualified_unverified` | Data provider's `naf_label` only, no SIREN | Lower — unverified, and undeduplicated |

**Three rule details that are easy to get wrong:**

- **NAF matching is by prefix (`01.`), not an exact-code allow-list.** 1,281
  companies carry pre-2008 **NAF rév.1** codes (`01.2A`, `01.4A`, `01.1A`,
  `01.2E`, `01.3Z`), 1,136 of them agricultural. An exact list of modern rév.2
  codes silently drops every one.
- **Label exclusions need word boundaries (`\y`).** Without them `chat` matches
  inside `achat` and `marchand`, disqualifying real prospects. The exclusion list
  removes 4,607 companies that a naive "elevage|eleveur" match would have swept
  in: `eleveur chien chat` (1,714), `eleveur d oiseaux` (1,541 — of which 1,525
  have emails, so it looks like a win until you read the label), `Fleuriste`
  (1,153). Horse breeding is deliberately **kept** — it is a listed target sector.
- **Two rules from the original spec were unimplementable and were dropped.**
  `employee_bracket >= "1-2"` would have disqualified 89% of the database
  (114,092 of 128,116 rows are NULL, and the values are INSEE tranche codes as
  TEXT so lexical `>=` is wrong anyway: `'11' < '2'`). `sirene_etat = 'F'`
  matches **zero** rows — the API in use only returns active entities, so closed
  businesses surface as "not found" instead. The rule is kept for future runs but
  is currently a no-op.

**Results** (128,116 companies, rule version `agri-v1`):

| Status | Reason | Rows | Share |
|--------|--------|------|-------|
| `qualified_unverified` | — | 61,944 | 48.3% |
| `qualified` | — | 39,701 | 31.0% |
| `pending` | `no_siren_no_label` | 16,793 | 13.1% |
| `disqualified` | `label_out_of_scope` | 4,607 | 3.6% |
| `disqualified` | `naf_out_of_scope` | 3,161 | 2.5% |
| `pending` | `sirene_not_found` | 1,782 | 1.4% |
| `disqualified` | `public_administration` | 128 | 0.1% |

The GFA rescue recovered **2,225 companies** (1,977 at `68.20B` + 248 at
`35.11Z`) that a strict NAF-code rule would have thrown away. 68 were correctly
left out because their labels do not claim agriculture. Communes (`84.11Z`) are
dropped regardless of label.

**Verification performed**: dry-run distribution compared against an independent
hand-written query (identical); re-run confirmed a true no-op (0 stale rows);
five disqualified companies spot-checked by hand. This run also wrote the
**first row ever** to `audit.audit_log`.

**Known limitation — deferred deliberately.** 2,430 of the 3,161
`naf_out_of_scope` companies (78%) carry an agricultural source label, spread
across ~100 NAF codes rather than the two that were rescued. Some are clearly
farms (`68.20A` land holding, `43.12A` earthworks), others are agritourism or
leisure (`55.20Z` farm gîtes, `93.19Z` sport, `85.51Z` riding schools). Recovering
them is a business decision, not a technical one. Because each is tagged
`naf_out_of_scope`, revisiting costs a `RULE_VERSION` bump and one re-run — this
is exactly what the version column is for.

---

### M1-S7: Natural-Language Query via Supabase MCP (2026-07-19)

MCP was connected with full read/write access (a deliberate change from the
read-only role the original plan proposed — see `CLAUDE.md`). Confirmed Sam's
prediction: **no custom code was needed**. The work was entirely in making the
views answer real questions.

**Acceptance test.** The Milestone-1 question was answered twice — once through
`public.v_qualified_contacts`, once hand-written against `staging.*` with no
views involved. Results were byte-identical, satisfying the
`implementation_plan_leads.md:394` criterion ("an answer you can verify by
running the SQL manually").

**What the first run exposed.** The top result was `department = NULL` with 735
farms, dwarfing every real department: **93.4% of sites (119,786 of 128,267) have
no department**, making the "broken down by department" question meaningless.
93,388 of those do have a postal code, so migration 006 derives it in the view —
`staging.sites.department` is left untouched so the raw value stays honest.

A naive `left(postal_code, 2)` would be wrong three ways, so four cases are
handled, each spot-checked against the city name:

| Case | Example | Result | Verified against |
|------|---------|--------|------------------|
| Leading zero lost in Excel | `1000` | `01` | city "Bourg en Bresse" (01000, Ain) |
| DOM-TOM needs 3 digits | `97480` | `974` | city "st joseph" (Réunion) |
| Corsica has no number | `20000` | `2A` | Ajaccio, Sartène, Figari (Corse-du-Sud) |
| Metropolitan | `71400` | `71` | — |

Two digits would have collapsed Guadeloupe, Martinique and Réunion into one
`'97'`. **Department coverage went from 7.5% to 87.3%.** A `department_source`
column records which branch produced each value, so an inferred department is
never mistaken for a recorded one.

**Two caveats.** ~99 sites (0.10%) have address fragments in `city` with a postal
code contradicting `postal_code` (e.g. postal `1027`, city
`"Rue De Lille - 59560Comines"`), so their derived department is wrong —
pre-existing source corruption. And **`with_verified_email` is 0 and will stay 0**
until M1-S6: every email is `verification_status = 'candidate'` by design, since
verification happens at campaign send time. The literal acceptance question asks
for a *verified* email, so it correctly returns zero; the test was run against
`with_email` instead.

---

## 5. Investigations & Findings

### 5.1 NAF Code vs `naf_label` Mismatch Investigation

**Problem discovered**: After enrichment, grouping `staging.companies` by `naf_code + naf_label` revealed that many rows have a `naf_label` that doesn't match the official meaning of their `naf_code`.

**Examples**:

| `naf_code` | Official Meaning | `naf_label` in DB | Rows Affected |
|------------|-----------------|-------------------|---------------|
| `68.20B` | Location de terrains et biens immobiliers (real estate) | `AGRICULTEURS` | ~2,000 |
| `35.11Z` | Production d'electricite | `AGRICULTEURS` | ~193 |
| `84.11Z` | Administration publique generale | `AGRICULTEURS` | ~85 |
| `43.12A` | Travaux de terrassement | `AGRICULTEURS` | ~23 |
| `10.71C` | Cuisson de produits de boulangerie | `eleveurs de petits gibiers` | ~22 |

**Root cause analysis**:

Checked the ingest script (`m1_s3_ingest.py`, line 58):
```python
"Activite": "naf_label",  # maps free-text Excel column to naf_label
```

The `naf_label` column was mapped from the source file's `Activite` column, which contains **free-text, user-entered descriptions** (e.g., "AGRICULTEURS"), not official INSEE NAF labels.

The `naf_code`, by contrast, was populated from the **official SIRENE API** during M1-S4 enrichment — this is the authoritative value.

**Cross-verification with live API**:

We queried a sample of mismatched SIRENs against the live SIRENE API:

| SIREN | `naf_label` (Excel) | API `naf_code` | What It Actually Is |
|-------|--------------------|----|-----|
| `921977179` | AGRICULTEURS | `68.20B` | **GFA DI GIROLAMO** — a Groupement Foncier Agricole (legal entity that owns farmland but is registered as real estate) |
| `890023708` | AGRICULTEURS | `35.11Z` | **SARL L'ENERGIE COURTAUX** — a farm that generates electricity (solar/biogas), registered as energy producer |
| `200084606` | AGRICULTEURS | `84.11Z` | **COMMUNE DE LES BELLEVILLE** — a town hall that owns communal farmland, registered as public administration |

**Conclusion**:

> **This is NOT a bug in the ingestion script.** The SIREN-to-NAF-code mapping from the SIRENE API is correct. The mismatch exists because:
>
> 1. **GFAs (68.20B)**: Real estate holding companies created to own farmland. They *are* in the agricultural ecosystem, but legally classified as real estate.
> 2. **Energy producers (35.11Z)**: Farms that installed solar panels or biogas digesters. Their primary registered activity is electricity production.
> 3. **Communes (84.11Z)**: Town halls that own communal agricultural land.
>
> The data provider's `Activite` column reflects the **business context** (these are all in the agricultural supply chain), while the `naf_code` reflects the **legal classification**.

**Impact on qualification**: This affects ~2,300+ rows. A strict NAF-code-only qualification would **incorrectly disqualify** real farms that are legally structured as GFAs or energy producers. The qualification rules must account for this by cross-referencing both `naf_code` and `naf_label`.

**Status**: Waiting for business-rule decision before implementing qualification (M1-S5).

---

### 5.2 Duplicate Measurement in the Qualified Set (2026-07-19)

**Why this was measured**: M1-S5 reported 101,645 qualified prospects, but tier 2
is undeduplicated (fuzzy dedup was never built — see 3.4). If duplication were
material, that headline is inflated and a campaign could contact the same farm
twice.

**Result: 4,365 confirmed duplicates among the 88,930 qualified rows that have a
usable key — 4.9%. So ~101,645 raw rows represent roughly 97,280 real
businesses.** 2,990 of the duplicate groups span **both tiers**.

**The mechanism.** The same farm appears in both source files — once with a SIRET
(→ tier 1) and once without (→ tier 2). M1-S3 deduplicated on SIRET only, so the
two records never merged. Confirmed examples, identical on name, postal code
*and* city, differing only in whether a SIREN is present:

| Postal | City | Tier 1 | Tier 2 |
|--------|------|--------|--------|
| 06200 | NICE | `CHEVAL LIBRE 06` (SIREN 818627168) | `CHEVAL LIBRE 06` (no SIREN) |
| 59269 | ARTRES | `FERME DES 3 MUIDS` (SIREN 899892749) | `FERME DES 3 MUIDS` (no SIREN) |
| 76590 | CRIQUETOT SUR LONGUEVILLE | `GAEC DES 2 SAPINS` (SIREN 495178907) | `GAEC DES 2 SAPINS` (no SIREN) |

**Method, and one false start worth recording.** The first attempt used
`phone_main` as the identity key and produced an alarming 12.5% duplication rate.
**That figure was wrong.** Spot-checking showed the worst offenders were numbers
like `+33890212903` and `+33899105777` shared across five different departments —
these are French **premium-rate service numbers** (`0890`, `0899`), a shared
agricultural helpline or provider placeholder, not one business. *Phone is not an
identity key in this dataset.*

The working key is **normalized name + postal code**: unaccent, lowercase, strip
punctuation, then **sort the word tokens** — which is what collapses
`"Lemoine Jean-Claude"` and `"JEAN-CLAUDE LEMOINE"` onto the same key. Person-name
records from this provider frequently reverse first/last name order between files.

**Two reasons the real rate is higher than 4.9%:**

1. **12,715 qualified rows have no usable key** (no postal code) and are excluded
   from the measurement entirely.
2. This is an **exact**-key match. A separate trigram pass at `similarity > 0.65`
   within the same postal code found **924 additional near-miss pairs** in tier 2
   alone — spelling variants the exact key misses.

**Also found**: the `pg_trgm` GIN index created in `001:118` is on
`legal_name` — which is **NULL for 92% of tier-2 companies** (only 5,016 of 61,944
have one). Tier 2 carries `trade_name` instead, on all 61,944 rows. So the one
index built for fuzzy dedup does not cover the rows that actually need it.
Migration 007 adds `idx_companies_trade_name_trgm` to close this.

### 5.3 Duplicate Marking Applied (M1-S5b, 2026-07-19)

`scripts/m1_s5b_dedup.py` + migrations 007/008. **5,810 companies marked as
duplicates** across two passes. In the query view this collapses 98,979 company
rows to **93,284 distinct businesses**.

| Pass | Method | Marked | What it catches |
|------|--------|--------|-----------------|
| 1 | `exact_name_postal` | 4,218 | Identical normalized name + postal |
| 2 | `stripped_legal_form` | 1,592 | Same after removing EARL/GAEC/SARL/SCEA/GFA and the provider's trailing activity tag: `EARL DU VAL VERT` == `DU VAL VERT` |

### The SIREN guard, and the bug it was written for

**A row is only marked as a duplicate if its SIREN is NULL, or equals the
survivor's.** Two rows with *different* non-null SIRENs are two separately
registered legal entities and must never be collapsed, however identical the
names look.

This matters specifically in French agriculture, where one family routinely
operates through several entities at one address:

| Entity A | Entity B | Why they must stay separate |
|----------|----------|------------------------------|
| `444925549 ANTOINE CARDON` | `830687851 EARL ANTOINE CARDON` | Sole trader + his EARL |
| `342450327 EARL DE LA CROIX BLANCHE` | `789799129 SCI DE LA CROIX BLANCHE` | Operating farm + land-holding company |
| `979953213 GFA LE CHAMP BERNARD` | `344280573 LE CHAMP BERNARD` | Land holding + operating entity |

**The first version of this script had no such guard and wrongly merged 56 rows**
— including `SCEA DE LA BRUNE` under two different SIRENs (`316741412`,
`504017724`) and `GAEC DES HORIZONS` under `423995182` / `423995000`. Caught by
checking the applied result rather than trusting the pass, unmarked, and the guard
added. The guard now blocks 56 rows in pass 1 and 146 in pass 2.

> **Open nuance, not auto-decided**: those separate legal entities usually share a
> decision-maker and an address. Legally distinct, commercially probably one
> conversation. Whether a campaign should contact both is a business call.

### Why a similarity threshold was rejected

A trigram pass was measured first, at thresholds from 0.50 to 0.90. Inspecting
actual pairs showed the true positives were almost never spelling variants — they
were legal-form noise. And the low bands mixed real matches with clear errors:

| Similarity | Example pair | Verdict |
|-----------|--------------|---------|
| 0.86 | `EURL PERIGORD VITELLUS DISTRIBUTION` / `PERIGORD VITELLUS DISTRIBUTION` | same |
| 0.63 | `Earl Albert` / `Earl Albert - eleveur` | same |
| 0.55 | `GAEC DE BEAULIEU` / `EARL DE BEAULIEU` | **different entities** |
| 0.53 | `Vente a la ferme` / `Au lait de chevre Vente a la ferme` | **generic bucket, not a name** |

No threshold cleanly separates those. Stripping legal forms and matching exactly
is more precise than any cutoff, so pass 2 does that instead. Trigram matching
remains unused.

**Non-destructive by design.** Nothing is deleted or merged. A duplicate keeps all
its data and gains `duplicate_of_company_id` pointing at its survivor;
deduplication happens in the views via `coalesce(duplicate_of_company_id, id)`,
exposed as `business_id`. The whole pass is undone with:

```sql
UPDATE staging.companies SET duplicate_of_company_id = NULL, dedup_checked_at = NULL;
```

**Why duplicates are kept in the view rather than filtered out.** 44 duplicate
groups have an email on the duplicate that the survivor does **not** have.
Filtering would have silently discarded those contacts. Measured before and after:
reachable emails stayed at **14,578** — dedup collapsed identity without losing a
single contact. 560 email-bearing contact rows sit on rows flagged `is_duplicate`.

**Survivor selection**, in order: has a SIREN → has a `naf_code` → earliest
`created_at` → lowest `id`. Deterministic, so re-runs are stable.

**Word-order reversal is the interesting case.** The provider routinely flips
first/last name between files, which is why the key sorts word tokens:

| Survivor | Duplicate | Postal |
|----------|-----------|--------|
| `CHRISTIAN JUFFET` (SIREN 330045709) | `CHRISTIAN JUFFET` (none) | 01120 |
| `FREDERIC MAGINIER` | `MAGINIER FREDERIC` | 01140 |
| `GAEC DU CHARNAY` (SIREN 437660913) | `DU CHARNAY GAEC` | 01270 |
| `GIBIER DOMBES` | `DOMBES GIBIER` | 01400 |

**How to count correctly from now on**: `count(DISTINCT business_id)`.
`count(DISTINCT siren)` returns 0 for all of tier 2, and
`count(DISTINCT company_id)` double-counts duplicates.

**Still not covered**: rows without a postal code (excluded from matching), and
near-miss spelling variants — the trigram pass found 924 candidate pairs in tier 2
that this exact-key method does not catch. `dedup_method` records
`exact_name_postal` per row, so a fuzzy pass can be added later without redoing
the confident matches.

---

## 6. Current Data Profile

> Verified live against the database on **2026-07-19**. Re-run the stats query to refresh.

| Table | Row Count |
|-------|-----------|
| `raw.ingest_rows` | 202,983 |
| `staging.source_files` | 2 |
| `staging.companies` | 128,116 |
| `staging.sites` | 128,267 |
| `staging.contacts` | 130,690 |
| `staging.emails` | 25,506 |
| `audit.audit_log` | 1 (first-ever write, from the M1-S5 run) |

**Company breakdown**:

| Category | Count |
|----------|-------|
| Has SIREN | 44,772 |
| No SIREN (**undeduplicated** — see corrections in 3.4) | 83,344 |
| SIRENE-enriched | 42,910 |
| Not found in SIRENE | 1,782 |

**Qualification** (rule version `agri-v1`):

| Status | Count |
|--------|-------|
| `qualified` (tier 1, official NAF) | 39,701 |
| `qualified_unverified` (tier 2, label only) | 61,944 |
| `pending` | 18,575 |
| `disqualified` | 7,896 |

**Reachability** — what is actually usable for a campaign:

| Metric | Tier 1 | Tier 2 |
|--------|--------|--------|
| Contact rows in `v_qualified_contacts` | 39,323 | 61,942 |
| Distinct companies | 37,037 | 61,942 |
| With an email | 5,048 | 9,530 |
| With a phone | 39,321 | 61,941 |
| With a **verified** email | 0 | 0 |

> Verified email is 0 across the board **by design** — verification is deferred to
> campaign send time (M1-S6), so every address is still `candidate`.

**Data quality caveats worth knowing before quoting any of these numbers:**

| Issue | Scale | Impact |
|-------|-------|--------|
| No-SIREN companies are undeduplicated | 83,344 (65%) | The same farm may appear more than once. Tier 2 makes them *usable*, not *unique* — real risk of contacting a prospect twice in one campaign |
| `employee_bracket` is NULL | 114,092 (89%) | Any size-based targeting is impossible today |
| `department` is NULL on the site row | 119,786 (93.4%) | Mitigated: derived from postal code in the views, coverage now 87.3% |
| `city` contaminated with address fragments | 99 (0.10%) | Their derived department is wrong |
| Emails exist for only a fifth of contacts | 25,506 of 130,690 | Phone is the far more complete channel — 99.9% coverage |

---

## 7. What's Next — Roadmap

> [!NOTE]
> **Milestone numbering.** This document previously listed M1-S7 as "Campaign
> Export". `implementation_plan_leads.md:391` and `CLAUDE.md` both define M1-S7 as
> the natural-language query step. The plan wins; campaign export is now M1-S8.

| Stage | Status |
|-------|--------|
| M1-S1 File inspection | ✅ Done |
| M1-S2 Schema | ✅ Done |
| M1-S3 Ingest / normalize / dedup | ✅ Done (partial — fuzzy dedup never built) |
| M1-S4 SIRENE enrichment | ✅ Done (95.8%) |
| M1-S5 Qualification | ✅ Done 2026-07-19 |
| M1-S7 NL query via MCP | ✅ Done 2026-07-19 |
| M1-S6 Email verification | ⏸ Deferred by design |
| M1-S8 Campaign export | 🔜 Planned |

### Highest-value next step: SIREN recovery (M1-S4b) — feasibility MEASURED

83,344 companies (65%) have no SIREN, the single constraint behind three
problems: they cannot be deduplicated on a hard key, cannot be SIRENE-verified,
and are stuck in tier 2. Recovering it addresses all three.

`scripts/m1_s4b_siren_recovery_sample.py` measured this on a 500-company sample
against the live API, querying name + `code_postal`. **Read-only — no writes.**

| Verdict | Share | Meaning |
|---------|-------|---------|
| `confident` | **42.0%** | Exactly one candidate whose normalized name matches exactly — auto-appliable |
| `no_result` | 35.6% | Nothing in the registry for that name in that commune |
| `weak` | 14.0% | Best similarity below 0.80 — correctly rejected |
| `probable` | 6.0% | One candidate, high but inexact similarity — reviewable |
| `ambiguous` | 2.4% | Several equally-good candidates |

**Correction to the earlier estimate**: the addressable population is **41,777,
not 83,344**. Roughly half the no-SIREN companies have no valid 5-digit postal
code, and without it name matching is not tractable. That also cuts the full-run
cost to **~2.3 hours**, not 4.6.

**Expected yield: ~17,500 companies promoted from tier 2 to tier 1**, each
gaining a verifiable identity, SIRENE enrichment eligibility, and a hard dedup
key. A further ~2,500 would land in a reviewable middle.

Sample verdicts behaved sensibly: `DE LA BOISSIERE EARL` → `530823178 EARL DE LA
BOISSIERE` (confident); `LA HOULERIE` → `GROUPEMENT AGRICOLE D EXPLOITATION`
(sim 0.28, correctly rejected as weak). The `ambiguous` cases are mostly the
multi-entity family pattern from 5.3 — `SERGE LAFON` returning three registered
entities in one commune — where picking one automatically would be a guess.

### M1-S4b RUN AND COMPLETE (2026-07-19)

`scripts/m1_s4b_siren_recovery.py`, confident matches only. **222.9 minutes,
41,777 companies attempted, 18,568 SIRENs recovered (44.8%)** plus **150
duplicates found by SIREN collision**.

| Verdict | Count | Share |
|---------|-------|-------|
| `confident` (applied) | 18,718 | 44.8% |
| `no_result` | 13,629 | 32.6% |
| `weak` | 6,318 | 15.1% |
| `probable` (left alone) | 2,209 | 5.3% |
| `ambiguous` (left alone) | 903 | 2.2% |

The 500-row sample predicted 42%; the full run delivered 44.8%. Sampling before
committing to a 3.7-hour job was worth it, and the estimate held.

**Companies with a SIREN: 44,772 → 63,340.**

**Verified**, not assumed: 8 randomly sampled recovered SIRENs were re-queried
against the live registry and all 8 resolved to an entity whose name matches ours
after normalization. Several were word-order reversals (`BILAN GERARD` →
`GERARD BILAN`, `BRETIJAN GAEC` → `GAEC BRETIJAN`), which is precisely what the
token-sorting in the key exists to handle.

**Collisions as deduplication.** 150 recovered SIRENs already belonged to another
company. Because `staging.companies.siren` is UNIQUE, a naive write would have
thrown; instead each collision was treated as evidence the two rows are the same
business and marked `dedup_method = 'siren_recovery_collision'`. These are the
most reliable duplicate links in the database — matched on a hard registry
identifier rather than a name heuristic.

**Reversible.** Every recovered SIREN carries `siren_recovered_at`:

```sql
UPDATE staging.companies
   SET siren = NULL, siren_recovered_at = NULL, siren_recovery_method = NULL
 WHERE siren_recovered_at IS NOT NULL;
```

**Follow-up chain not yet run.** Recovery wrote SIREN and nothing else. Three
steps remain, in order — until they run, the 18,568 companies still sit in tier 2
with no `naf_code`:

1. `m1_s4_sirene_enrich.py` — newly-SIRENed rows have `sirene_last_checked_at`
   NULL so they are picked up automatically. Multi-hour. **Now protected by the
   exact-SIREN guard** added the same day.
2. `m1_s5_qualify.py --force` — with `naf_code` present these rows move from
   tier 2 to tier 1.
3. `m1_s5b_dedup.py` — re-run to catch duplicates the new SIRENs expose.

**Still without a SIREN: 64,776 companies.** Roughly half of the original
no-SIREN population was never addressable (no valid 5-digit postal code), and
32.6% of those attempted genuinely are not in the registry under that name and
commune.

### Also outstanding (none block anything today)

| Item | Why it matters |
|------|----------------|
| **`pg_trgm` fuzzy dedup** | Never implemented. 83k rows may contain duplicates; a campaign could contact the same farm twice |
| **RLS policies** | `CREATE POLICY IF NOT EXISTS` is invalid SQL, so zero policies exist. Needed before any non-service-role access |
| **M1-S4 unvalidated `results[0]`** | Possible silent cross-contamination of enriched data. Audit before trusting further |
| **Missing FK** on `raw.ingest_rows.source_file_id` | Violates the project's own "no orphan rows" rule |
| **`requirements.txt` is wrong** | Missing `calamine` and `aiohttp` (both required), lists 3 unused packages |
| **The 2,430 label-agri companies** | Currently `naf_out_of_scope`. Recovering them is a business decision + a `RULE_VERSION` bump |

### M1-S6: Email Discovery & Verification (Deferred by design)

1. Discover company website domains
2. Generate email candidates from name patterns
3. Verify via MillionVerifier / Hunter.io free tier — **at campaign send time, not
   at ingestion**, so free-tier quota is not spent on results that go stale first

### M1-S8: Campaign Export (Planned)

1. Generate delivery CSVs filtered by qualification tier + verification status
2. Track campaign sends and responses in `staging.campaign_contacts`

---

## 8. Tech Stack

| Component | Tool | Why |
|-----------|------|-----|
| Database | Supabase (PostgreSQL, free tier) | Zero budget. Managed. MCP-ready |
| ETL | Python 3.11 + pandas + psycopg2 | Simple, no orchestration overhead |
| Excel reading | calamine engine | 10x faster than openpyxl for large files |
| Bulk inserts | `psycopg2.extras.execute_values` | Avoids row-by-row overhead and SSL crashes |
| SIRENE API | `aiohttp` (async) | Rate-limited API requires parallel + pacing |
| NL queries | Supabase MCP + Claude | Natural-language querying over structured data |
| Version control | GitHub | Standard |

---

## 9. How to Run

### Prerequisites

```bash
pip install -r requirements.txt
# Contains: pandas, calamine, psycopg2-binary, python-dotenv, aiohttp
```

### Environment

```bash
cp .env.example .env
```

> [!IMPORTANT]
> **Use the session-mode pooler, not the direct connection.** The direct host
> `db.<ref>.supabase.co` now resolves to **IPv6 only**. On a machine without an
> IPv6 route, psycopg2 fails with
> `could not translate host name ... Name or service not known` — which looks like
> a credentials problem but is not. This broke every script in `scripts/` on
> 2026-07-19 and is why the URL format changed.
>
> ```
> SUPABASE_DB_URL=postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres
> ```
>
> Note the username is `postgres.<project-ref>`, not plain `postgres`.

The Supabase MCP server additionally reads `SUPABASE_ACCESS_TOKEN` from the
**shell environment** — Claude Code does not load `.env`. On Windows:

```powershell
[Environment]::SetEnvironmentVariable("SUPABASE_ACCESS_TOKEN","sbp_...","User")
# then fully restart Claude Code — a window reload is not enough
```

### Pipeline Execution Order

```bash
# 1. Inspect files (read-only, no DB needed)
python scripts/m1_s1_inspect_files.py

# 2. Apply schema (run in Supabase SQL Editor or via psql)
psql $DATABASE_URL < migrations/001_initial_schema.sql
# Or run the migration runners:
python scripts/apply_migration_002.py
python scripts/apply_migration_003.py
# 004-006 were applied via the Supabase MCP server:
#   004_qualification.sql      - qualification columns + constraints
#   005_query_views.sql        - both tiers, best-email LATERAL, department rollup
#   006_derive_department.sql  - derive department from postal code

# 3. Ingest raw data
python scripts/m1_s3_ingest.py

# 4. Enrich from SIRENE (~4 hours for 44k companies)
python scripts/m1_s4_sirene_enrich.py
# NOTE: --dry-run hangs forever without --limit. Always pair them:
python scripts/m1_s4_sirene_enrich.py --limit 50 --dry-run

# 5. Qualify (seconds - one set-based UPDATE, not row-by-row)
python scripts/m1_s5_qualify.py --dry-run   # always review the distribution first
python scripts/m1_s5_qualify.py
```

**Re-qualifying after a rule change**: edit `config/sector_rules.py`, bump
`RULE_VERSION`, re-run. Rows whose stored version differs are reclassified
automatically. Running without bumping the version is a deliberate no-op — use
`--force` to override.

---

## Project Structure

```
leads_provider_codes/
|-- .env                           # Supabase connection string (gitignored)
|-- .env.example                   # Template for .env
|-- .mcp.json                      # Supabase MCP server config (token via env var)
|-- .gitignore
|-- CLAUDE.md                      # Working conventions + settled decisions
|-- README.md                      # Quick-start README
|-- implementation_plan_leads.md   # Milestone breakdown and done-criteria
|-- requirements.txt               # Python dependencies (OUT OF SYNC - see roadmap)
|-- config/
|   +-- sector_rules.py            # Qualification rules + RULE_VERSION
|-- docs/
|   +-- project_documentation.md   # <-- This file
|-- Data Globale 05 juillet 2026/
|   |-- Copie de agriculteurs total.xlsx
|   |-- siret_results*.xlsx          # Skipped (pipeline artifact)
|   +-- Copie de Eleveurs_verified*.xlsx
|-- migrations/
|   |-- 001_initial_schema.sql       # Full DDL
|   |-- 002_add_source_metadata.sql
|   |-- 003_add_sirene_enrichment_columns.sql
|   |-- 004_qualification.sql        # Qualification columns + constraints
|   |-- 005_query_views.sql          # Both tiers, best-email LATERAL
|   +-- 006_derive_department.sql    # Department derived from postal code
+-- scripts/
    |-- m1_s1_inspect_files.py       # Read-only file inspection
    |-- m1_s3_ingest.py              # Raw ingest + normalize + dedup
    |-- m1_s4_sirene_enrich.py       # SIRENE API enrichment
    |-- m1_s5_qualify.py             # Qualification pass (set-based)
    |-- apply_migration_002.py       # Migration runner
    +-- apply_migration_003.py       # Migration runner
```

---

## 11. Where the Project Stands

**In one sentence**: three scattered Excel files are now a queryable, enriched,
qualified prospect database that answers natural-language questions — with known,
documented gaps.

### The journey

| Stage | What changed |
|-------|--------------|
| Start | 3 Excel files, ~203k rows, overlapping, no schema, no way to query |
| M1-S1 | Established file B ⊂ file C (100% SIRET overlap) → skipped B, avoiding mass duplication |
| M1-S2 | 4 schemas, 8 tables, raw → staging → audit separation |
| M1-S3 | 202,983 raw rows → 128,116 companies / 128,267 sites / 130,690 contacts |
| M1-S4 | 42,910 companies enriched from INSEE (95.8% of those with a SIREN) |
| M1-S5 | 101,645 companies qualified across two tiers; 7,896 explicitly rejected with reasons |
| M1-S7 | Natural-language questions answerable and verifiable, no custom code |

### The headline result

**93,284 distinct qualified businesses**, deduplicated (see 5.3). Split 39,701
SIREN-verified (tier 1) and 61,944 label-qualified (tier 2) before dedup collapses
5,810 duplicate rows. **14,578 reachable emails** across 14,005 businesses, and
phone coverage on essentially all of them.

> Quote **93,284**, not the 101,645 raw row count. Count with
> `count(DISTINCT business_id)`. The real figure is slightly lower still: rows
> without a postal code cannot be duplicate-checked at all.

The most consequential decision was tier 2. Following the original spec literally
would have produced 39,701 qualified prospects and shelved 83,344 as `pending`.
Measuring first showed those "unusable" records held four times more reachable
email than the entire verified segment. That single choice is the difference
between a 39k and a 101k deliverable.

### What this is not, yet

Honest limits, so nobody over-promises to the client:

- **Dedup is now partial, not absent.** 5,810 duplicates are marked across two
  passes (5.3), but rows without a postal code cannot be checked at all, and
  entities sharing a decision-maker under different SIRENs are deliberately left
  separate. Count with `business_id`, and expect a small residue of
  double-contacts.
- **No email is verified.** All 25,506 are `candidate`. Verification is deliberately
  deferred to send time.
- **Phone is the real channel.** 99.9% coverage versus 20% for email.
- **Size targeting is impossible.** `employee_bracket` is NULL for 89% of rows.
- **~13% of leads have no department**, even after derivation from postal codes.

### The most useful thing to do next

**SIREN recovery (M1-S4b).** One missing field — SIREN on 65% of companies — is
the root cause of tier 2 existing, of dedup being impossible, and of enrichment
being unavailable for most of the database. Recovering it collapses three problems
into one job.
