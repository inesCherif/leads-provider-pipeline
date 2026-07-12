# leads-provider-data-pipeline

A modular, zero-budget B2B lead data pipeline for an energy-efficiency prospecting company. Centralizes scattered prospect spreadsheets, enriches them via the French SIRENE business registry, and makes the full dataset queryable in natural language via Supabase + MCP.

## Project Context

The pipeline targets French businesses that are heavy energy consumers (agrifood, livestock, bakeries, hotels, clinics, etc.) for a utility-sector client's energy-efficiency services. It ingests historical spreadsheet data, deduplicates across sources using SIRET as the canonical anchor, enriches via INSEE's public API, qualifies prospects by sector-specific rules, discovers contact details, verifies email addresses in batch, and exports clean delivery lists.

## Architecture

```
Raw XLSX/CSV files  ──►  Stage 0: Inspect & Register Sources
INSEE SIRENE API    ──►  Stage 3: Enrich & Validate Status
Web / LinkedIn      ──►  Stage 5: Contact & Domain Discovery
                              │
                     Supabase (PostgreSQL)
                     ├── raw.*      (untouched ingested data)
                     ├── staging.*  (normalized, deduped, enriched)
                     ├── public.*   (read-only views for NL queries)
                     └── audit.*    (full change log)
                              │
                     Supabase MCP + Claude/Cursor
                     └── Natural-language queries, no custom UI
```

## Pipeline Stages

| Stage | Script | Description |
|-------|--------|-------------|
| M1-S1 | `scripts/m1_s1_inspect_files.py` | Read-only file inspection & source classification |
| M1-S2 | `migrations/001_initial_schema.sql` | Supabase schema setup |
| M1-S3 | `scripts/m1_s3_ingest.py` | Raw ingest + normalize + deduplicate |
| M1-S4 | `scripts/m1_s4_sirene_enrich.py` | SIRENE API enrichment |
| M1-S5 | `scripts/m1_s5_qualify.py` | Sector-specific qualification rules |
| M1-S6 | `scripts/m1_s6_email_verify.py` | Email candidate generation + batch verification |

## Tech Stack

- **Database**: [Supabase](https://supabase.com) (PostgreSQL, free tier)
- **ETL**: Python 3.11+ with `pandas`, `psycopg2`, `httpx`
- **Registry data**: [INSEE SIRENE API](https://api.insee.fr) (free, official)
- **NL queries**: Supabase MCP + Claude Desktop / Cursor
- **Scheduling**: Windows Task Scheduler / GitHub Actions (free tier)
- **Email verification**: MillionVerifier + Hunter.io (free tiers)

## Getting Started

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Fill in your Supabase URL and service-role key
```

### 3. Run inspection (read-only, no DB needed)
```bash
python scripts/m1_s1_inspect_files.py
```

### 4. Apply schema to Supabase
```bash
# Run migrations/001_initial_schema.sql in your Supabase SQL editor
# or via psql:
psql $DATABASE_URL < migrations/001_initial_schema.sql
```

## Data Model (core tables)

| Table | Description |
|-------|-------------|
| `staging.source_files` | Traceability anchor — every row in every table points here |
| `staging.companies` | Legal entities (SIREN level) |
| `staging.sites` | Physical establishments (SIRET level) |
| `staging.contacts` | Named individuals at sites |
| `staging.emails` | Email candidates with verification status |
| `audit.audit_log` | Append-only change log |

## Conventions

- Every record carries a `source_file_id` FK — no orphan rows
- SIRET is the primary deduplication key; fuzzy match on `(name + postal_code)` as fallback
- Stages are **independently re-runnable** — no monolithic ETL
- All scripts are idempotent (safe to re-run on the same data)
- Closed/inactive companies are flagged, never deleted

## Sector Scope

| Sector | Status | NAF codes |
|--------|--------|-----------|
| Livestock / éleveurs | 🟡 Pilot in progress | 01.4x, 01.5x |
| Agrifood processors | 🔲 Planned | 10.xx, 11.xx |
| Bakeries | 🔲 Planned | 10.71 |
| Hotels / Spas | 🔲 Planned | 55.xx |
| Other | 🔲 Planned | — |

## License

Internal use only. Data sourced from INSEE SIRENE (open license) and proprietary commercial sources. Do not redistribute contact data.
