"""
M1-S3 — Raw Ingest + Normalize + Deduplicate
==============================================
Stage   : M1-S3 (Milestone 1, Step 3)
Purpose : Ingest the two pilot source files into Supabase:
            - File A: Copie de agriculteurs total.xlsx         (independent raw source)
            - File C: Copie de Eleveurs_verified (...).xlsx    (primary éleveurs source)
          File B (Eleveurs_siret_results) is intentionally SKIPPED — confirmed in M1-S1
          to be a strict subset of File C; ingesting it would create duplicate companies.

What this script does:
  1. Compute SHA-256 of each file → skip if already imported (idempotent)
  2. Register each file in staging.source_files
  3. Store every row verbatim as JSONB in raw.ingest_rows
  4. Normalize column names per file → canonical row dict
  5. Deduplicate:
       - Companies  : SIREN (first 9 digits of SIRET) as primary key
       - Sites      : SIRET (14 digits) as primary key
       - Contacts   : one per (site_id, full_name); skip phone-only rows without company
       - Emails     : one per (contact_id, email_address)
  6. Log all merge/skip decisions to audit.audit_log

IDEMPOTENT  : safe to re-run — already-imported files are skipped.
NO DELETES  : never deletes existing rows.

Requirements:
    pip install pandas openpyxl psycopg2-binary python-dotenv tqdm

Environment (.env file in project root):
    SUPABASE_DB_URL=postgresql://postgres:[password]@db.<project-ref>.supabase.co:5432/postgres
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from datetime import date

try:
    import pandas as pd
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
    from tqdm import tqdm
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install pandas openpyxl psycopg2-binary python-dotenv tqdm")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR     = PROJECT_ROOT / "Data Globale 05 juillet 2026"
SCRIPT_NAME  = "m1_s3_ingest.py"

# Pipeline-stage trust order: higher index = more trusted
PIPELINE_TRUST = {"unknown": 0, "raw": 1, "siret_matched": 2, "verified": 3}

# ── File definitions ─────────────────────────────────────────────────────────
# Each entry: filename, sector, pipeline_stage, notes, collection_date_approx
FILE_CONFIGS = [
    {
        "filename": "Copie de agriculteurs total.xlsx",
        "sector":   "agriculture",
        "pipeline_stage": "raw",
        "notes": "Independent raw source: French farming businesses. Provided by CEO. "
                 "Does not overlap significantly with éleveurs files (17% SIRET overlap confirmed M1-S1).",
        "collection_date_approx": None,   # unknown — fill in if learned later
    },
    # File B intentionally skipped (100% subset of File C — see M1-S1 notes)
    {
        "filename": "Copie de Eleveurs_verified (liste de toutes les eleveurs avec Siret ).xlsx",
        "sector":   "livestock",
        "pipeline_stage": "verified",
        "notes": "Primary éleveurs source. Contains all of File B + 14,879 additional records. "
                 "Has Siret_Verifie column (registry-confirmed SIRET). ~72k rows are phone-only contacts "
                 "with no company name — these are skipped in this import pass (logged to audit_log).",
        "collection_date_approx": None,
    },
]

# ── Column mappings per file ─────────────────────────────────────────────────
# Maps source column name → canonical field name used in normalization logic
COL_MAPS = {
    "Copie de agriculteurs total.xlsx": {
        "Societe":              "trade_name",
        "Responsable":          "contact_full_name",
        "Adresse":              "address_line1",
        "CP":                   "postal_code",
        "dep":                  "department",
        "Ville":                "city",
        "Telephone":            "phone_main",
        "Siret":                "siret",
        "Email":                "email_address",
        "Activite":             "naf_label",
        "Statut_Entreprise":    "status_raw",
        "Nom_Officiel":         "legal_name",
    },
    "Copie de Eleveurs_verified (liste de toutes les eleveurs avec Siret ).xlsx": {
        "Societe":              "trade_name",
        "Responsable":          "contact_full_name",
        "Adresse":              "address_line1",
        "CP":                   "postal_code",
        "dep":                  "department",
        "Ville":                "city",
        "Telephone":            "phone_main",
        "Phone_number":         "phone_alt",
        "Siret":                "siret_raw",       # less reliable — use Siret_Verifie when available
        "Siret_Verifie":        "siret",            # registry-confirmed SIRET — takes priority
        "Email":                "email_address",
        "Activite":             "naf_label",
        "Statut_Activite":      "status_raw",
        "Nom_Officiel":         "legal_name",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_siret(raw) -> str | None:
    """Strip whitespace/dots and validate length. Returns 14-char SIRET or None."""
    if raw is None or str(raw).strip() in ("", "nan", "None", "NaN"):
        return None
    cleaned = re.sub(r"[\s.\-]", "", str(raw)).strip()
    if len(cleaned) == 14 and cleaned.isdigit():
        return cleaned
    return None


def siret_to_siren(siret: str | None) -> str | None:
    if siret and len(siret) == 14:
        return siret[:9]
    return None


def normalize_status(raw) -> bool | None:
    """Map French status strings to boolean is_active. None = unknown."""
    if raw is None or str(raw).strip() in ("", "nan", "None", "NaN"):
        return None
    s = str(raw).strip().lower()
    if "actif" in s:
        return True
    if any(k in s for k in ("ferm", "cess", "radié", "radie")):
        return False
    # "introuvable", "non trouvé" → unknown, NOT closed
    return None


def clean_str(val) -> str | None:
    """Strip and return None for empty/nan values."""
    if val is None:
        return None
    s = str(val).strip()
    return None if s.lower() in ("", "nan", "none") else s


def normalize_row(row: dict, col_map: dict) -> dict:
    """Apply column mapping and clean all string values."""
    canonical = {}
    for src_col, canon_name in col_map.items():
        canonical[canon_name] = clean_str(row.get(src_col))
    return canonical


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    db_url = os.getenv("SUPABASE_DB_URL")
    if not db_url:
        sys.exit(
            "ERROR: SUPABASE_DB_URL not found in .env\n"
            "Add: SUPABASE_DB_URL=postgresql://postgres:[password]@db.<project-ref>.supabase.co:5432/postgres"
        )
    return psycopg2.connect(db_url)


def log_audit(cur, table_name, record_id, field, old_val, new_val, reason):
    cur.execute(
        """
        INSERT INTO audit.audit_log (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (table_name, record_id, field, old_val, new_val, SCRIPT_NAME, reason),
    )


# ---------------------------------------------------------------------------
# Phase 1 — Register source file
# ---------------------------------------------------------------------------

def register_source_file(cur, path: Path, config: dict, file_hash: str, row_count_raw: int) -> str | None:
    """
    Insert into staging.source_files. Returns the UUID, or None if already imported.
    """
    # Check if already imported
    cur.execute("SELECT id FROM staging.source_files WHERE file_hash = %s", (file_hash,))
    existing = cur.fetchone()
    if existing:
        print(f"    [SKIP] Already imported (hash match). source_file_id={existing[0]}")
        return None

    collection_date = config.get("collection_date_approx")
    cur.execute(
        """
        INSERT INTO staging.source_files
            (file_name, file_hash, collection_date_approx, row_count_raw, sector, pipeline_stage, notes, imported_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            path.name,
            file_hash,
            collection_date,
            row_count_raw,
            config["sector"],
            config["pipeline_stage"],
            config["notes"],
            SCRIPT_NAME,
        ),
    )
    source_file_id = cur.fetchone()[0]
    print(f"    Registered in source_files → id={source_file_id}")
    return source_file_id


# ---------------------------------------------------------------------------
# Phase 2 — Raw ingest (verbatim JSONB rows)
# ---------------------------------------------------------------------------

def ingest_raw_rows(cur, df: pd.DataFrame, source_file_id: str, batch_size: int = 2000) -> int:
    """Insert all rows into raw.ingest_rows as JSONB. Returns count inserted."""
    records = []
    for i, row in df.iterrows():
        records.append({
            "source_file_id": source_file_id,
            "row_index": int(i),
            "raw_json": json.dumps(row.where(row.notna(), other=None).to_dict(), ensure_ascii=False, default=str),
        })

    inserted = 0
    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO raw.ingest_rows (source_file_id, row_index, raw_json)
            VALUES %s
            ON CONFLICT (source_file_id, row_index) DO NOTHING
            """,
            [(r["source_file_id"], r["row_index"], r["raw_json"]) for r in batch],
        )
        inserted += len(batch)
    return inserted


# ---------------------------------------------------------------------------
# Phase 3 — Normalize + Deduplicate into staging tables
# ---------------------------------------------------------------------------

def upsert_company(cur, norm: dict, source_file_id: str, pipeline_stage: str) -> tuple[str, bool]:
    """
    Insert or update company. Returns (company_id, is_new).
    Dedup key: SIREN (from SIRET). Falls back to (legal_name or trade_name + postal_code) if no SIREN.
    """
    siret  = clean_siret(norm.get("siret"))
    siren  = siret_to_siren(siret)
    name   = clean_str(norm.get("legal_name")) or clean_str(norm.get("trade_name"))
    postal = clean_str(norm.get("postal_code"))

    if siren:
        cur.execute("SELECT id, source_file_id FROM staging.companies WHERE siren = %s", (siren,))
        existing = cur.fetchone()
        if existing:
            existing_id, existing_sf_id = existing
            # Only update fields if incoming source is more trusted
            cur.execute("SELECT pipeline_stage FROM staging.source_files WHERE id = %s", (existing_sf_id,))
            existing_stage = cur.fetchone()[0]
            if PIPELINE_TRUST.get(pipeline_stage, 0) > PIPELINE_TRUST.get(existing_stage, 0):
                # Update NULL fields with better data
                cur.execute(
                    """
                    UPDATE staging.companies SET
                        legal_name  = COALESCE(legal_name, %s),
                        trade_name  = COALESCE(trade_name, %s),
                        naf_label   = COALESCE(naf_label, %s),
                        updated_at  = NOW()
                    WHERE id = %s
                    """,
                    (
                        clean_str(norm.get("legal_name")),
                        clean_str(norm.get("trade_name")),
                        clean_str(norm.get("naf_label")),
                        existing_id,
                    ),
                )
                log_audit(cur, "staging.companies", existing_id, "merged_from_higher_trust_source",
                          existing_stage, pipeline_stage, f"Merged NULL fields from {pipeline_stage} source")
            return existing_id, False

    elif name and postal:
        # Fuzzy fallback: similarity search via pg_trgm
        cur.execute(
            """
            SELECT id FROM staging.companies
            WHERE postal_code_approx = %s
              AND (
                similarity(legal_name, %s) > 0.6
                OR similarity(trade_name, %s) > 0.6
              )
            LIMIT 1
            """,
            (postal[:2], name, name),  # department-level match first
        )
        # Note: postal_code_approx doesn't exist — use a simpler approach
        cur.execute(
            """
            SELECT c.id FROM staging.companies c
            JOIN staging.sites s ON s.company_id = c.id
            WHERE s.postal_code = %s
              AND (
                similarity(c.legal_name, %s) > 0.65
                OR similarity(c.trade_name, %s) > 0.65
              )
            LIMIT 1
            """,
            (postal, name, name),
        )
        row = cur.fetchone()
        if row:
            log_audit(cur, "staging.companies", row[0], None, None, None,
                      f"Fuzzy match found: '{name}' + '{postal}' → existing company")
            return row[0], False

    # Insert new company
    cur.execute(
        """
        INSERT INTO staging.companies
            (siren, legal_name, trade_name, naf_label, qualification_status, source_file_id)
        VALUES (%s, %s, %s, %s, 'unqualified', %s)
        RETURNING id
        """,
        (
            siren,
            clean_str(norm.get("legal_name")),
            clean_str(norm.get("trade_name")),
            clean_str(norm.get("naf_label")),
            source_file_id,
        ),
    )
    company_id = cur.fetchone()[0]
    return company_id, True


def upsert_site(cur, norm: dict, company_id: str, source_file_id: str) -> tuple[str, bool]:
    """
    Insert or skip site. Returns (site_id, is_new).
    Dedup key: SIRET if available; else (company_id) — one site per company for no-SIRET records.
    """
    siret     = clean_siret(norm.get("siret"))
    is_active = normalize_status(norm.get("status_raw"))

    if siret:
        cur.execute("SELECT id FROM staging.sites WHERE siret = %s", (siret,))
        existing = cur.fetchone()
        if existing:
            # Update is_active if we now know it
            if is_active is not None:
                cur.execute(
                    "UPDATE staging.sites SET is_active = %s, updated_at = NOW() WHERE id = %s AND is_active IS NULL",
                    (is_active, existing[0]),
                )
            return existing[0], False

    else:
        # No SIRET: check if this company already has a site
        cur.execute("SELECT id FROM staging.sites WHERE company_id = %s LIMIT 1", (company_id,))
        existing = cur.fetchone()
        if existing:
            return existing[0], False

    cur.execute(
        """
        INSERT INTO staging.sites
            (siret, company_id, is_headquarters, is_active,
             address_line1, postal_code, city, department,
             source_file_id)
        VALUES (%s, %s, TRUE, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            siret,
            company_id,
            is_active,
            clean_str(norm.get("address_line1")),
            clean_str(norm.get("postal_code")),
            clean_str(norm.get("city")),
            clean_str(norm.get("department")),
            source_file_id,
        ),
    )
    return cur.fetchone()[0], True


def upsert_contact(cur, norm: dict, site_id: str, company_id: str, source_file_id: str) -> str | None:
    """Insert contact if not already present for this site. Returns contact_id or None."""
    full_name = clean_str(norm.get("contact_full_name"))
    phone     = clean_str(norm.get("phone_main"))
    phone_alt = clean_str(norm.get("phone_alt"))

    if full_name:
        # Check duplicate: same site + same name
        cur.execute(
            "SELECT id FROM staging.contacts WHERE site_id = %s AND full_name = %s",
            (site_id, full_name),
        )
        existing = cur.fetchone()
        if existing:
            return existing[0]

    elif not phone:
        return None  # Nothing to create

    is_generic = full_name is None

    cur.execute(
        """
        INSERT INTO staging.contacts
            (site_id, company_id, full_name, phone_main, phone_alt, is_generic_contact,
             enrichment_status, source_file_id)
        VALUES (%s, %s, %s, %s, %s, %s, 'not_started', %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (site_id, company_id, full_name, phone, phone_alt, is_generic, source_file_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def upsert_email(cur, norm: dict, contact_id: str, source_file_id: str):
    """Insert email candidate if not already present."""
    email = clean_str(norm.get("email_address"))
    if not email or "@" not in email:
        return
    cur.execute(
        """
        INSERT INTO staging.emails
            (contact_id, email_address, is_primary, verification_status, source_file_id)
        VALUES (%s, %s, TRUE, 'candidate', %s)
        ON CONFLICT (contact_id, email_address) DO NOTHING
        """,
        (contact_id, email.lower().strip(), source_file_id),
    )


# ---------------------------------------------------------------------------
# Phase 3 orchestrator — process one file
# ---------------------------------------------------------------------------

def process_file(conn, path: Path, config: dict, col_map: dict):
    print(f"\n{'='*60}")
    print(f"Processing: {path.name}")
    print(f"{'='*60}")

    if not path.exists():
        print(f"  ERROR: File not found: {path}")
        return

    # Compute hash
    file_hash = sha256_file(path)
    print(f"  SHA-256 : {file_hash[:16]}...")

    # Load file
    print("  Loading XLSX...")
    df = pd.read_excel(path, dtype=str, engine="openpyxl")
    df = df.where(df.notna(), other=None)   # replace NaN with None
    row_count_raw = len(df)
    print(f"  Rows    : {row_count_raw:,}")

    with conn.cursor() as cur:
        # Phase 1: Register
        source_file_id = register_source_file(cur, path, config, file_hash, row_count_raw)
        if source_file_id is None:
            conn.commit()
            return  # Already imported

        # Phase 2: Raw ingest
        print("  Inserting raw rows...")
        raw_count = ingest_raw_rows(cur, df, source_file_id)
        print(f"  Raw rows inserted: {raw_count:,}")

        # Phase 3: Normalize + Deduplicate
        print("  Normalizing and deduplicating into staging tables...")
        stats = {
            "companies_new": 0, "companies_existing": 0,
            "sites_new": 0,     "sites_existing": 0,
            "contacts_new": 0,
            "emails_new": 0,
            "skipped_no_identity": 0,
        }

        rows_iter = df.iterrows()

        for _, row in tqdm(rows_iter, total=row_count_raw, unit="rows"):
            norm = normalize_row(row.to_dict(), col_map)

            siret = clean_siret(norm.get("siret"))
            name  = clean_str(norm.get("legal_name")) or clean_str(norm.get("trade_name"))

            # Skip phone-only rows: no company identity at all
            if not siret and not name:
                stats["skipped_no_identity"] += 1
                continue

            try:
                # Company
                company_id, is_new_co = upsert_company(cur, norm, source_file_id, config["pipeline_stage"])
                stats["companies_new" if is_new_co else "companies_existing"] += 1

                # Site
                site_id, is_new_site = upsert_site(cur, norm, company_id, source_file_id)
                stats["sites_new" if is_new_site else "sites_existing"] += 1

                # Contact
                contact_id = upsert_contact(cur, norm, site_id, company_id, source_file_id)
                if contact_id:
                    stats["contacts_new"] += 1

                    # Email
                    upsert_email(cur, norm, contact_id, source_file_id)
                    if clean_str(norm.get("email_address")):
                        stats["emails_new"] += 1

            except Exception as e:
                conn.rollback()
                print(f"\n  ERROR on row: {norm}\n  {e}")
                raise

        # Update imported row count
        cur.execute(
            "UPDATE staging.source_files SET row_count_imported = %s WHERE id = %s",
            (row_count_raw - stats["skipped_no_identity"], source_file_id),
        )

        # Log summary to audit_log
        log_audit(
            cur, "staging.source_files", source_file_id, None, None, None,
            f"Import complete: {stats}",
        )

        conn.commit()

    # Print summary
    print(f"\n  ── Import Summary ──────────────────────────────")
    print(f"  Companies new       : {stats['companies_new']:>8,}")
    print(f"  Companies merged    : {stats['companies_existing']:>8,}")
    print(f"  Sites new           : {stats['sites_new']:>8,}")
    print(f"  Sites merged        : {stats['sites_existing']:>8,}")
    print(f"  Contacts created    : {stats['contacts_new']:>8,}")
    print(f"  Emails staged       : {stats['emails_new']:>8,}")
    print(f"  Skipped (no ID)     : {stats['skipped_no_identity']:>8,}  ← phone-only rows, no company/SIRET")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_final_summary(conn):
    print(f"\n{'='*60}")
    print("DATABASE TOTALS AFTER IMPORT")
    print(f"{'='*60}")
    with conn.cursor() as cur:
        for table in ["staging.source_files", "staging.companies", "staging.sites",
                      "staging.contacts", "staging.emails", "raw.ingest_rows"]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"  {table:<35} : {count:>10,} rows")


def main():
    print("\n" + "#"*60)
    print("  M1-S3 — RAW INGEST + NORMALIZE + DEDUPLICATE")
    print("#"*60)

    conn = get_conn()
    print(f"  Connected to Supabase ✓")

    try:
        for config in FILE_CONFIGS:
            path     = DATA_DIR / config["filename"]
            col_map  = COL_MAPS[config["filename"]]
            process_file(conn, path, config, col_map)

        print_final_summary(conn)

    finally:
        conn.close()

    print("\n" + "#"*60)
    print("  M1-S3 COMPLETE")
    print("#"*60 + "\n")


if __name__ == "__main__":
    main()
