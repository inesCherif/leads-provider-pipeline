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
| `pg_trgm` extension for fuzzy matching | Levenshtein / exact match only | French business names have accent variations, abbreviations (GAEC vs G.A.E.C.), and typos. Trigram similarity handles this robustly |
| RLS enabled but permissive | No RLS | Future-proofing. When Supabase Auth users are configured, we tighten policies without schema changes |

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

## 6. Current Data Profile

> These numbers are from the last verified run (2026-07-12). Re-run the stats query to get current values.

| Table | Row Count |
|-------|-----------|
| `raw.ingest_rows` | ~133,000 |
| `staging.source_files` | 2 |
| `staging.companies` | ~128,116 |
| `staging.sites` | ~128,116 |
| `staging.contacts` | ~128,116 |
| `staging.emails` | ~40,000+ |

**Company breakdown**:

| Category | Count |
|----------|-------|
| Has SIREN | ~44,772 |
| No SIREN (dedup by name only) | ~83,344 |
| SIRENE-enriched | ~42,910 |
| Pending enrichment (errors/not found) | ~1,862 |

---

## 7. What's Next — Roadmap

### M1-S5: Qualification Rules (Next)

Apply business rules to classify each company as `qualified`, `disqualified`, or `pending`:

| Rule | Status | Action |
|------|--------|--------|
| `sirene_etat = 'F'` (closed) | Disqualified | Business is legally closed |
| `naf_code` in target sectors | Qualified | Agriculture, agrifood, livestock |
| `naf_code` = `84.11Z` (public admin) | Disqualified | Town halls, not a prospect |
| `naf_code` = `68.20B` but `naf_label` = 'AGRICULTEURS' | **Decision needed** | GFAs — real farms legally as real estate |
| No SIREN at all | Pending | Cannot enrich or verify |

### M1-S6: Email Discovery & Verification (Planned)

1. Discover company website domains
2. Generate email candidates from name patterns
3. Batch-verify via MillionVerifier / Hunter.io (free tier)

### M1-S7: Campaign Export (Planned)

1. Generate delivery CSVs filtered by qualification + verification status
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
# Set SUPABASE_DB_URL=postgresql://postgres:***@db.xxx.supabase.co:5432/postgres
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

# 3. Ingest raw data
python scripts/m1_s3_ingest.py

# 4. Enrich from SIRENE (takes ~4 hours for 44k companies)
python scripts/m1_s4_sirene_enrich.py
# Test with: python scripts/m1_s4_sirene_enrich.py --limit 50 --dry-run
```

---

## Project Structure

```
leads_provider_codes/
|-- .env                           # Supabase connection string (gitignored)
|-- .env.example                   # Template for .env
|-- .gitignore
|-- README.md                      # Quick-start README
|-- requirements.txt               # Python dependencies
|-- docs/
|   +-- project_documentation.md   # <-- This file
|-- Data Globale 05 juillet 2026/
|   |-- Copie de agriculteurs total.xlsx
|   |-- siret_results*.xlsx          # Skipped (pipeline artifact)
|   +-- Copie de Eleveurs_verified*.xlsx
|-- migrations/
|   |-- 001_initial_schema.sql       # Full DDL
|   |-- 002_add_source_metadata.sql
|   +-- 003_add_sirene_enrichment_columns.sql
+-- scripts/
    |-- m1_s1_inspect_files.py       # Read-only file inspection
    |-- m1_s3_ingest.py              # Raw ingest + normalize + dedup
    |-- m1_s4_sirene_enrich.py       # SIRENE API enrichment
    |-- apply_migration_002.py       # Migration runner
    +-- apply_migration_003.py       # Migration runner
```
