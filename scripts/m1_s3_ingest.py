"""
M1-S3 — Raw Ingest + Normalize + Deduplicate (BULK EDITION)
=============================================================
Rewritten to use psycopg2 execute_values for massive speedup.
Processes 50k rows in seconds instead of hours.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path
import math

try:
    import pandas as pd
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install pandas calamine psycopg2-binary python-dotenv")

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR     = PROJECT_ROOT / "Data Globale 05 juillet 2026"
SCRIPT_NAME  = "m1_s3_ingest.py"

PIPELINE_TRUST = {"unknown": 0, "raw": 1, "siret_matched": 2, "verified": 3}

FILE_CONFIGS = [
    {
        "filename": "Copie de agriculteurs total.xlsx",
        "sector":   "agriculture",
        "pipeline_stage": "raw",
        "notes": "Independent raw source: French farming businesses.",
        "collection_date_approx": None,
    },
    {
        "filename": "Copie de Eleveurs_verified (liste de toutes les eleveurs avec Siret ).xlsx",
        "sector":   "livestock",
        "pipeline_stage": "verified",
        "notes": "Primary éleveurs source. Has Siret_Verifie.",
        "collection_date_approx": None,
    },
]

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
        "Siret":                "siret_raw",
        "Siret_Verifie":        "siret",
        "Email":                "email_address",
        "Activite":             "naf_label",
        "Statut_Activite":      "status_raw",
        "Nom_Officiel":         "legal_name",
    },
}

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def clean_siret(raw) -> str | None:
    if raw is None or str(raw).strip() in ("", "nan", "None", "NaN"):
        return None
    cleaned = re.sub(r"[\s.\-]", "", str(raw)).strip()
    if len(cleaned) == 14 and cleaned.isdigit():
        return cleaned
    return None

def siret_to_siren(siret: str | None) -> str | None:
    return siret[:9] if siret and len(siret) == 14 else None

def normalize_status(raw) -> bool | None:
    if raw is None or str(raw).strip() in ("", "nan", "None", "NaN"):
        return None
    s = str(raw).strip().lower()
    if "actif" in s:
        return True
    if any(k in s for k in ("ferm", "cess", "radié", "radie")):
        return False
    return None

def clean_str(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return None if s.lower() in ("", "nan", "none") else s

def clean_postal_code(val) -> str | None:
    s = clean_str(val)
    if s:
        s = s.replace(" ", "").replace(".", "")
        if s.endswith("0") and len(s) > 5 and "." in str(val):
            # float conversion issue e.g. "59000.0" -> "590000" (already replaced dots)
            s = s[:5]
        return s[:5]
    return None

def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    db_url = os.getenv("SUPABASE_DB_URL")
    if not db_url:
        sys.exit("ERROR: SUPABASE_DB_URL not found in .env")
    return psycopg2.connect(db_url)

def process_file_bulk(conn, path: Path, config: dict, col_map: dict):
    print(f"\n{'='*60}\nProcessing: {path.name}\n{'='*60}")
    file_hash = sha256_file(path)
    print(f"  SHA-256 : {file_hash[:16]}...")

    df = pd.read_excel(path, dtype=str, engine="calamine")
    df = df.where(df.notna(), other=None)
    
    with conn.cursor() as cur:
        # Register file
        cur.execute("SELECT id FROM staging.source_files WHERE file_hash = %s", (file_hash,))
        existing = cur.fetchone()
        if existing:
            print(f"  [SKIP] Already imported (id={existing[0]}).")
            return
            
        cur.execute(
            """INSERT INTO staging.source_files (file_name, file_hash, sector, pipeline_stage, notes, row_count_raw, imported_by) 
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (path.name, file_hash, config["sector"], config["pipeline_stage"], config["notes"], len(df), SCRIPT_NAME)
        )
        source_file_id = cur.fetchone()[0]
        print(f"  Registered file -> {source_file_id}")

        # Ingest Raw
        print("  Inserting raw rows...")
        raw_records = [(source_file_id, i, json.dumps(r.to_dict(), ensure_ascii=False, default=str)) for i, r in df.iterrows()]
        psycopg2.extras.execute_values(
            cur, "INSERT INTO raw.ingest_rows (source_file_id, row_index, raw_json) VALUES %s ON CONFLICT DO NOTHING", raw_records, page_size=2000
        )

        # Normalize rows
        normalized = []
        for _, row in df.iterrows():
            norm = {canon: clean_str(row.get(src)) for src, canon in col_map.items()}
            norm["siret"] = clean_siret(norm.get("siret"))
            norm["siren"] = siret_to_siren(norm.get("siret"))
            norm["is_active"] = normalize_status(norm.get("status_raw"))
            norm["postal_code"] = clean_postal_code(norm.get("postal_code"))
            normalized.append(norm)

        # 1. BULK COMPANIES
        print("  Merging Companies...")
        co_unique = {}
        for n in normalized:
            if n["siren"]:
                # Last seen overwrites if duplicates in same file
                co_unique[n["siren"]] = (n["siren"], n.get("legal_name"), n.get("trade_name"), n.get("naf_label"), 'unqualified', source_file_id)
        
        co_values = list(co_unique.values())
        siren_to_id = {}
        if co_values:
            query = """
                INSERT INTO staging.companies (siren, legal_name, trade_name, naf_label, qualification_status, source_file_id)
                VALUES %s
                ON CONFLICT (siren) DO UPDATE SET
                    legal_name = COALESCE(staging.companies.legal_name, EXCLUDED.legal_name),
                    trade_name = COALESCE(staging.companies.trade_name, EXCLUDED.trade_name),
                    naf_label = COALESCE(staging.companies.naf_label, EXCLUDED.naf_label)
                RETURNING siren, id
            """
            res = psycopg2.extras.execute_values(cur, query, co_values, fetch=True, page_size=2000)
            siren_to_id = {r[0]: r[1] for r in res}
            
        # Non-siren companies (fallback)
        fallback_co = []
        fallback_co_indices = []
        for i, n in enumerate(normalized):
            if not n["siren"] and (n.get("legal_name") or n.get("trade_name")):
                fallback_co.append((n.get("legal_name"), n.get("trade_name"), n.get("naf_label"), 'unqualified', source_file_id))
                fallback_co_indices.append(i)
            elif n["siren"]:
                n["_company_id"] = siren_to_id.get(n["siren"])
                
        if fallback_co:
            query = "INSERT INTO staging.companies (legal_name, trade_name, naf_label, qualification_status, source_file_id) VALUES %s RETURNING id"
            res = psycopg2.extras.execute_values(cur, query, fallback_co, fetch=True, page_size=2000)
            for idx, r in zip(fallback_co_indices, res):
                normalized[idx]["_company_id"] = r[0]

        # 2. BULK SITES
        print("  Merging Sites...")
        site_unique = {}
        for n in normalized:
            if n["siret"] and n.get("_company_id"):
                site_unique[n["siret"]] = (
                    n["siret"], n["_company_id"], True, n.get("is_active"), n.get("address_line1"), 
                    n.get("postal_code"), n.get("city"), n.get("department"), source_file_id
                )
        
        site_values = list(site_unique.values())
        siret_to_id = {}
        if site_values:
            query = """
                INSERT INTO staging.sites (siret, company_id, is_headquarters, is_active, address_line1, postal_code, city, department, source_file_id)
                VALUES %s
                ON CONFLICT (siret) DO UPDATE SET
                    is_active = COALESCE(staging.sites.is_active, EXCLUDED.is_active)
                RETURNING siret, id
            """
            res = psycopg2.extras.execute_values(cur, query, site_values, fetch=True, page_size=2000)
            siret_to_id = {r[0]: r[1] for r in res}
            
        fallback_site = []
        fallback_site_indices = []
        for i, n in enumerate(normalized):
            if n["siret"]:
                n["_site_id"] = siret_to_id.get(n["siret"])
            elif not n["siret"] and n.get("_company_id"):
                fallback_site.append((n["_company_id"], True, n.get("address_line1"), n.get("postal_code"), n.get("city"), n.get("department"), source_file_id))
                fallback_site_indices.append(i)
                
        if fallback_site:
            query = "INSERT INTO staging.sites (company_id, is_headquarters, address_line1, postal_code, city, department, source_file_id) VALUES %s RETURNING id"
            res = psycopg2.extras.execute_values(cur, query, fallback_site, fetch=True, page_size=2000)
            for idx, r in zip(fallback_site_indices, res):
                normalized[idx]["_site_id"] = r[0]

        # 3. BULK CONTACTS
        print("  Merging Contacts...")
        contact_unique = {}
        generic_contact = []
        generic_contact_indices = []
        for i, n in enumerate(normalized):
            if n.get("_site_id") and n.get("contact_full_name"):
                key = (n["_site_id"], n["contact_full_name"])
                contact_unique[key] = (n["_site_id"], n.get("_company_id"), n["contact_full_name"], n.get("phone_main"), n.get("phone_alt"), False, 'not_started', source_file_id)
            elif n.get("_site_id") and not n.get("contact_full_name") and n.get("phone_main"):
                generic_contact.append((n["_site_id"], n.get("_company_id"), n.get("phone_main"), n.get("phone_alt"), True, 'not_started', source_file_id))
                generic_contact_indices.append(i)
                
        if generic_contact:
            query = "INSERT INTO staging.contacts (site_id, company_id, phone_main, phone_alt, is_generic_contact, enrichment_status, source_file_id) VALUES %s RETURNING id"
            res = psycopg2.extras.execute_values(cur, query, generic_contact, fetch=True, page_size=2000)
            for idx, r in zip(generic_contact_indices, res):
                normalized[idx]["_contact_id"] = r[0]
                
        contact_values = list(contact_unique.values())
        contact_keys_to_id = {}
        if contact_values:
            query = """
                INSERT INTO staging.contacts (site_id, company_id, full_name, phone_main, phone_alt, is_generic_contact, enrichment_status, source_file_id)
                VALUES %s
                RETURNING site_id, full_name, id
            """
            res = psycopg2.extras.execute_values(cur, query, contact_values, fetch=True, page_size=2000)
            contact_keys_to_id = {(r[0], r[1]): r[2] for r in res}
            
        for n in normalized:
            if n.get("_site_id") and n.get("contact_full_name"):
                n["_contact_id"] = contact_keys_to_id.get((n["_site_id"], n["contact_full_name"]))

        # 4. BULK EMAILS
        print("  Merging Emails...")
        email_unique = {}
        for n in normalized:
            if n.get("_contact_id") and n.get("email_address") and "@" in n["email_address"]:
                key = (n["_contact_id"], n["email_address"].lower().strip())
                email_unique[key] = (n["_contact_id"], n["email_address"].lower().strip(), True, 'candidate', source_file_id)
                
        email_values = list(email_unique.values())
        if email_values:
            query = """
                INSERT INTO staging.emails (contact_id, email_address, is_primary, verification_status, source_file_id)
                VALUES %s
                ON CONFLICT (contact_id, email_address) DO NOTHING
            """
            psycopg2.extras.execute_values(cur, query, email_values, page_size=2000)

        cur.execute("UPDATE staging.source_files SET row_count_imported = %s WHERE id = %s", (len(normalized), source_file_id))
        conn.commit()
        print("  Done!")

def main():
    conn = get_conn()
    try:
        for config in FILE_CONFIGS:
            process_file_bulk(conn, DATA_DIR / config["filename"], config, COL_MAPS[config["filename"]])
    finally:
        conn.close()

if __name__ == "__main__":
    main()
