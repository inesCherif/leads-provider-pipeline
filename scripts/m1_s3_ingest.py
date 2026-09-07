r"""
M1-S3 / M4 — ingest one source file into Supabase (spec-driven, resumable)
===========================================================================
    python scripts/m1_s3_ingest.py --list
    python scripts/m1_s3_ingest.py --file "Copie de Camping.xlsx" --dry-run
    python scripts/m1_s3_ingest.py --file "<name>" [--limit N]

One file per run. The file's shape, sector and cleaning hooks come from
config/ingest_specs.py — never from the command line, because a typo'd sector
is qualified by nobody (check_data_quality.py asserts every sector has a rule
set, but the spec is where it is prevented).

Stages, each in its OWN committed transaction, each idempotent, so a dropped
pooler socket costs a re-run and not the file (the three data-loss incidents
in this project were all one long transaction held across slow work):

    register  staging.source_files (hash-keyed; a finished file is SKIPPED,
              an unfinished one is RESUMED)
    raw       raw.ingest_rows, every row, every column, verbatim JSON
    companies staging.companies (SIREN path: ON CONFLICT; no-SIREN path:
              look up staging.company_identifiers first) + identifiers
    members   staging.company_sources — sector is membership (migration 018)
    sites     staging.sites (SIRET path: ON CONFLICT; else one no-SIRET site
              per company, reused on resume)
    contacts  staging.contacts (named: (site, full_name) looked up first;
              generic: one per site, reused)
    emails    staging.emails — ONLY addresses classify_email() calls
              'candidate'; malformed ones never reach the table that ships
    points    staging.contact_points — every phone (all phone columns) and
              every e-mail (candidate AND malformed) with source + evidence
    attrs     staging.company_attributes — the sector-specific long tail
    finish    source_files.row_count_imported (not with --limit, so a pilot
              is resumed by the full run rather than skipped)

--dry-run reads and normalises the file and prints the distribution it WOULD
write; it opens no connection. Run it first, every time.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

try:
    import pandas as pd
    import psycopg2
    import psycopg2.extras
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install pandas python-calamine psycopg2-binary python-dotenv")

from config.ingest_specs import FILE_SPECS, spec_for            # noqa: E402
from config.sector_rules import source_to_sector_key             # noqa: E402
from ingest_lib import (                                          # noqa: E402
    check_column_contract, classify_email, clean_department, clean_phone,
    clean_postal_code, clean_siren, clean_siret, clean_str, dialable_first, get_conn, is_surtaxe,
    json_dumps, luhn_ok, normalize_status, phone_digits, sha256_file,
    siret_to_siren, truncated_identifier,
)

DATA_DIR    = PROJECT_ROOT / "Data Globale 05 juillet 2026"
SCRIPT_NAME = "m1_s3_ingest.py"
PAGE        = 2000

# ─── file-specific row hooks (canonical dict, raw row dict) -> canonical dict ─

def _pre_provider_address(n, raw):
    """Provider dialect: ADRESSE2 holds the street (ADRESSE1 is 0-10% filled)."""
    a1, a2 = clean_str(n.get("address_line1")), clean_str(n.get("address_line2"))
    n["address_line1"] = a2 or a1
    n["address_line2"] = a1 if (a1 and a2) else None
    return n


def _pre_imprimerie_city(n, raw):
    """5,020 rows have the CITY in ADRESSE1 and VILLE empty; the two columns
    are mutually exclusive (measured), so the coalesce is lossless. There is
    no street anywhere in this file."""
    city, a1 = clean_str(n.get("city")), clean_str(n.get("address_line1"))
    n["city"] = city or a1
    n["address_line1"] = None
    return n


_PJ_ID_RE = re.compile(r"/pros/(\d+)")


def _pre_viticulteur_pj(n, raw):
    for k in ("phone_main", "phone_alt"):
        if n.get(k) and n[k].strip().upper() == "N/A":
            n[k] = None
    if n.get("naf_label"):
        n["naf_label"] = re.sub(r"\s*\+\d+$", "", n["naf_label"]).strip()   # '+1' = UI residue
    m = _PJ_ID_RE.search(n.get("pj_detail_url") or "")
    n["pj_detail_url"] = m.group(1) if m else None            # 'pagesjaunes.fr#' -> None
    return n


def _pre_boulang_keys(n, raw):
    """siren_enrichi is the key (100%). The provider's SIRET is kept only when
    it agrees with that SIREN; siret_enrichi is the fallback under the same
    rule. A SIRET that contradicts the SIREN is another company's identifier."""
    _pre_provider_address(n, raw)
    siren = clean_siren(n.get("siren"))
    cands = [clean_siret(n.get("siret")), clean_siret(n.get("siret_alt"))]
    keep = next((c for c in cands if c and siren and c[:9] == siren), None)
    if keep is None and siren is None:
        keep = cands[0]                      # 8-digit siren_enrichi: fall back to the file's SIRET
    n["siret"] = keep
    n["siren"] = siren or siret_to_siren(keep)
    n["siret_alt"] = None
    return n


PRE_CLEAN = {
    "provider_address": _pre_provider_address,
    "imprimerie_city":  _pre_imprimerie_city,
    "viticulteur_pj":   _pre_viticulteur_pj,
    "boulang_keys":     _pre_boulang_keys,
}

# Frame-level hooks (after the raw landing, before normalisation).
PRE_FRAME = {
    # 896 exact duplicate rows; (name, zipcode, phone) is the listing identity.
    "viticulteur_pj": lambda df: df.drop_duplicates(subset=["name", "zipcode", "phone"]),
}


def _parse_date(raw) -> date | None:
    s = clean_str(raw)
    if not s:
        return None
    # '2019 06 01 00 00 00' is the boulang file's registry date (separators stripped)
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y %m %d %H %M %S", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


# ─── normalisation ───────────────────────────────────────────────────────────

def normalize_rows(df, spec: dict, file_hash: str) -> list[dict]:
    hook = PRE_CLEAN.get(spec.get("pre_clean")) if spec.get("pre_clean") else None
    rows = []
    for idx, row in df.iterrows():
        raw = row.to_dict()
        n = {canon: clean_str(raw.get(src)) for src, canon in spec["col_map"].items()}
        n["_row_index"] = int(idx)
        if hook:
            n = hook(n, raw)

        # identifiers — validate, never pad
        if spec.get("pre_clean") != "boulang_keys":
            n["siret"] = clean_siret(n.get("siret"))
            n["siren"] = clean_siren(n.get("siren")) or siret_to_siren(n["siret"])
        n["_siret_truncated"] = None if n["siret"] else truncated_identifier(
            raw.get(next((s for s, c in spec["col_map"].items() if c == "siret"), ""), None))

        n["is_active"]   = normalize_status(n.get("status_raw"))
        n["postal_code"] = clean_postal_code(n.get("postal_code"))
        n["department"]  = clean_department(n.get("department"))
        n["creation_date"] = _parse_date(n.get("creation_date_raw"))
        code = clean_str(n.get("naf_code_source"))
        if code:
            code = code.upper().replace(" ", "")
            if re.fullmatch(r"[0-9]{2}\.?[0-9]{2}[A-Z]", code):
                code = code if "." in code else code[:2] + "." + code[2:]
            else:
                code = None
        n["naf_code_source"] = code

        # phones: every phone column, first two become main/alt
        phones, seen = [], set()
        for col in spec["phone_columns"]:
            p = clean_phone(raw.get(col)) if col in raw else clean_phone(n.get(col))
            if p and p not in seen:
                seen.add(p)
                phones.append((col, p, is_surtaxe(p)))
        phones = dialable_first(phones)
        n["_phones"] = phones
        dialable = [p for _, p, s in phones if not s]      # surtaxé never reaches contacts
        n["phone_main"] = dialable[0] if dialable else None
        n["phone_alt"]  = dialable[1] if len(dialable) > 1 else None

        # e-mails
        emails = []
        for col in spec["email_columns"]:
            norm, verdict, ev = classify_email(raw.get(col))
            if norm and verdict != "empty":
                emails.append((col, norm, verdict, ev))
        n["_emails"] = emails
        n["email_address"] = next((e[1] for e in emails if e[2] == "candidate"), None)

        # sector-specific long tail
        n["_attrs"] = {c: clean_str(raw.get(c)) for c in spec["attribute_columns"] if clean_str(raw.get(c))}

        # external identifiers
        ids = {t: n.get(f) for t, f in spec["identifier_columns"].items() if n.get(f)}
        if n["_siret_truncated"]:
            ids["siret_truncated"] = n["_siret_truncated"]
        if not n["siret"] and not n["siren"] and not ids:
            ids["file_row"] = f"{file_hash[:16]}:{idx}"
        n["_ids"] = ids

        n["_has_name"] = bool(n.get("legal_name") or n.get("trade_name"))
        rows.append(n)
    return rows


def describe(rows: list[dict], n_raw: int) -> None:
    c = Counter()
    defects = Counter()
    id_types = Counter()
    for n in rows:
        c["rows"] += 1
        c["named_business"] += n["_has_name"]
        c["siret"] += bool(n["siret"])
        c["siren_only"] += bool(n["siren"] and not n["siret"])
        c["no_siren"] += not n["siren"]
        c["siret_luhn_fail"] += bool(n["siret"] and not luhn_ok(n["siret"]))
        c["siret_truncated"] += bool(n["_siret_truncated"])
        c["postal_5"] += bool(n["postal_code"] and re.fullmatch(r"[0-9]{5}", n["postal_code"]))
        c["department"] += bool(n["department"])
        c["contact_name"] += bool(n.get("contact_full_name"))
        c["phone_main"] += bool(n["phone_main"])
        c["phone_points"] += len(n["_phones"])
        c["surtaxe"] += sum(1 for _, _, s in n["_phones"] if s)
        c["email_candidate"] += bool(n["email_address"])
        for _, _, v, ev in n["_emails"]:
            if v == "malformed":
                c["email_malformed"] += 1
                defects[ev.get("defect", "?")] += 1
        c["active_true"] += n["is_active"] is True
        c["active_false"] += n["is_active"] is False
        c["creation_date"] += bool(n["creation_date"])
        c["naf_code_source"] += bool(n["naf_code_source"])
        for t in n["_ids"]:
            id_types[t] += 1
    print(f"\n  rows in file {n_raw:,} -> rows to load {c['rows']:,} "
          f"(named business {c['named_business']:,})")
    for k in ("siret", "siren_only", "no_siren", "siret_truncated", "siret_luhn_fail",
              "postal_5", "department", "contact_name", "phone_main", "phone_points",
              "surtaxe", "email_candidate", "email_malformed", "active_true",
              "active_false", "creation_date", "naf_code_source"):
        print(f"  {k:<18} {c[k]:>8,}")
    if defects:
        print(f"  malformed e-mails by defect: {dict(defects)}")
    print(f"  identifiers by type: {dict(id_types)}")


# ─── writers — one committed transaction each ────────────────────────────────

def stage_register(conn, path: Path, spec: dict, file_hash: str, n_raw: int):
    with conn.cursor() as cur:
        cur.execute("SELECT id, row_count_imported FROM staging.source_files WHERE file_hash = %s",
                    (file_hash,))
        row = cur.fetchone()
        if row and row[1] is not None:
            return row[0], "done"
        if row:
            return row[0], "resume"
        cur.execute(
            """INSERT INTO staging.source_files
                   (file_name, file_hash, sector, pipeline_stage, notes, row_count_raw,
                    imported_by, data_source, collected_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (path.name, file_hash, spec["sector"], spec["pipeline_stage"], spec["notes"],
             n_raw, SCRIPT_NAME, spec.get("data_source"), spec.get("collected_at")))
        sfid = cur.fetchone()[0]
    conn.commit()
    return sfid, "new"


def stage_raw(conn, sfid, df) -> int:
    records = [(sfid, int(i), json_dumps(r.to_dict())) for i, r in df.iterrows()]
    for start in range(0, len(records), 5000):          # short transactions
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur, "INSERT INTO raw.ingest_rows (source_file_id, row_index, raw_json) "
                     "VALUES %s ON CONFLICT DO NOTHING",
                records[start:start + 5000], page_size=PAGE)
        conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.ingest_rows WHERE source_file_id = %s", (sfid,))
        return cur.fetchone()[0]


def stage_companies(conn, sfid, rows: list[dict]) -> dict:
    stats = Counter()
    with conn.cursor() as cur:
        # A. SIREN path — bulk upsert, COALESCE never overwrites
        by_siren = {}
        for n in rows:
            if n["siren"] and n["_has_name"]:
                by_siren[n["siren"]] = (n["siren"], n.get("legal_name"), n.get("trade_name"),
                                        n.get("naf_label"), n["naf_code_source"], n["creation_date"],
                                        "unqualified", sfid)
        siren_to_id = {}
        if by_siren:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.companies
                    (siren, legal_name, trade_name, naf_label, naf_code_source, creation_date,
                     qualification_status, source_file_id)
                VALUES %s
                ON CONFLICT (siren) DO UPDATE SET
                    legal_name      = COALESCE(staging.companies.legal_name,      EXCLUDED.legal_name),
                    trade_name      = COALESCE(staging.companies.trade_name,      EXCLUDED.trade_name),
                    naf_label       = COALESCE(staging.companies.naf_label,       EXCLUDED.naf_label),
                    naf_code_source = COALESCE(staging.companies.naf_code_source, EXCLUDED.naf_code_source),
                    creation_date   = COALESCE(staging.companies.creation_date,   EXCLUDED.creation_date)
                RETURNING siren, id, (xmax = 0) AS inserted""",
                list(by_siren.values()), fetch=True, page_size=PAGE)
            for siren, cid, inserted in res:
                siren_to_id[siren] = cid
                stats["siren_inserted" if inserted else "siren_merged"] += 1
        for n in rows:
            if n["siren"]:
                n["_company_id"] = siren_to_id.get(n["siren"])

        # B. no-SIREN path — identifiers first, INSERT only what is unknown
        pending = [n for n in rows if not n["siren"] and n["_has_name"]]
        wanted = {(t, v) for n in pending for t, v in n["_ids"].items()}
        found = {}
        if wanted:
            res = psycopg2.extras.execute_values(cur, """
                SELECT ci.id_type, ci.id_value, ci.company_id
                FROM staging.company_identifiers ci
                JOIN (VALUES %s) AS w(id_type, id_value)
                  ON w.id_type = ci.id_type AND w.id_value = ci.id_value""",
                sorted(wanted), fetch=True, page_size=PAGE)
            found = {(t, v): cid for t, v, cid in res}
        to_insert, key_of = [], {}
        for n in pending:
            hit = next((found[(t, v)] for t, v in n["_ids"].items() if (t, v) in found), None)
            if hit:
                n["_company_id"] = hit
                stats["fallback_reused"] += 1
                continue
            key = tuple(sorted(n["_ids"].items())) or ("row", n["_row_index"])
            if key in key_of:
                n["_company_id"] = key_of[key]      # same identifiers within this file
                continue
            key_of[key] = None
            to_insert.append((key, n))
        if to_insert:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.companies
                    (legal_name, trade_name, naf_label, naf_code_source, creation_date,
                     qualification_status, source_file_id)
                VALUES %s RETURNING id""",
                [(n.get("legal_name"), n.get("trade_name"), n.get("naf_label"),
                  n["naf_code_source"], n["creation_date"], "unqualified", sfid)
                 for _, n in to_insert], fetch=True, page_size=PAGE)
            for (key, n), (cid,) in zip(to_insert, res):
                key_of[key] = cid
                n["_company_id"] = cid
                stats["fallback_inserted"] += 1
            for n in pending:
                if n.get("_company_id") is None:
                    n["_company_id"] = key_of.get(tuple(sorted(n["_ids"].items())))

        # C. identifiers for every row that has any
        id_rows = [(n["_company_id"], t, v, sfid) for n in rows
                   if n.get("_company_id") for t, v in n["_ids"].items()]
        if id_rows:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.company_identifiers (company_id, id_type, id_value, source_file_id)
                VALUES %s ON CONFLICT DO NOTHING""", id_rows, page_size=PAGE)
            stats["identifiers"] = len(id_rows)
    conn.commit()
    stats["rows_without_name_skipped"] = sum(1 for n in rows if not n["_has_name"])
    return stats


def stage_membership(conn, sfid, rows) -> int:
    recs = [(n["_company_id"], sfid, n["_row_index"]) for n in rows if n.get("_company_id")]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO staging.company_sources (company_id, source_file_id, row_index)
            VALUES %s ON CONFLICT DO NOTHING""", recs, page_size=PAGE)
    conn.commit()
    return len(recs)


def stage_sites(conn, sfid, rows) -> dict:
    stats = Counter()
    with conn.cursor() as cur:
        by_siret = {}
        for n in rows:
            if n["siret"] and n.get("_company_id"):
                by_siret[n["siret"]] = (
                    n["siret"], n["_company_id"], True, n["is_active"], n.get("address_line1"),
                    n.get("address_line2"), n["postal_code"], n.get("city"), n["department"],
                    n.get("status_raw"), sfid)
        siret_to_id = {}
        if by_siret:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.sites
                    (siret, company_id, is_headquarters, is_active, address_line1, address_line2,
                     postal_code, city, department, source_status, source_file_id)
                VALUES %s
                ON CONFLICT (siret) DO UPDATE SET
                    is_active     = COALESCE(staging.sites.is_active,     EXCLUDED.is_active),
                    address_line2 = COALESCE(staging.sites.address_line2, EXCLUDED.address_line2),
                    department    = COALESCE(staging.sites.department,    EXCLUDED.department),
                    source_status = COALESCE(staging.sites.source_status, EXCLUDED.source_status)
                RETURNING siret, id, (xmax = 0) AS inserted""",
                list(by_siret.values()), fetch=True, page_size=PAGE)
            for siret, sid, inserted in res:
                siret_to_id[siret] = sid
                stats["siret_inserted" if inserted else "siret_merged"] += 1
        for n in rows:
            if n["siret"]:
                n["_site_id"] = siret_to_id.get(n["siret"])

        # no-SIRET rows: one site per company, reused if it already exists
        need = {n["_company_id"] for n in rows if not n["siret"] and n.get("_company_id")}
        existing = {}
        if need:
            cur.execute("""
                SELECT DISTINCT ON (company_id) company_id, id FROM staging.sites
                WHERE company_id = ANY(%s::uuid[]) AND siret IS NULL
                ORDER BY company_id, created_at, id""", (list(need),))
            existing = dict(cur.fetchall())
            stats["fallback_reused"] = len(existing)
        first_row = {}
        for n in rows:
            if not n["siret"] and n.get("_company_id") and n["_company_id"] not in existing:
                first_row.setdefault(n["_company_id"], n)
        if first_row:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.sites
                    (company_id, is_headquarters, is_active, address_line1, address_line2,
                     postal_code, city, department, source_status, source_file_id)
                VALUES %s RETURNING company_id, id""",
                [(cid, True, n["is_active"], n.get("address_line1"), n.get("address_line2"),
                  n["postal_code"], n.get("city"), n["department"], n.get("status_raw"), sfid)
                 for cid, n in first_row.items()], fetch=True, page_size=PAGE)
            existing.update(dict(res))
            stats["fallback_inserted"] = len(res)
        for n in rows:
            if not n["siret"] and n.get("_company_id"):
                n["_site_id"] = existing.get(n["_company_id"])
    conn.commit()
    return stats


def stage_contacts(conn, sfid, rows) -> dict:
    stats = Counter()
    site_ids = list({n["_site_id"] for n in rows if n.get("_site_id")})
    with conn.cursor() as cur:
        cur.execute("""
            SELECT site_id, full_name, is_generic_contact, id, phone_main, phone_alt
            FROM staging.contacts WHERE site_id = ANY(%s::uuid[])
            ORDER BY site_id, created_at, id""", (site_ids,))
        named, generic = {}, {}
        for site_id, full_name, is_generic, cid, pm, pa in cur.fetchall():
            if full_name:
                named.setdefault((site_id, full_name), (cid, pm, pa))
            elif is_generic:
                generic.setdefault(site_id, (cid, pm, pa))

        new_named, new_generic, fill = {}, {}, []
        for n in rows:
            sid = n.get("_site_id")
            if not sid:
                continue
            name = n.get("contact_full_name")
            if name:
                key = (sid, name)
                if key in named:
                    n["_contact_id"] = named[key][0]
                    stats["named_reused"] += 1
                    if (named[key][1] is None and n["phone_main"]) or (named[key][2] is None and n["phone_alt"]):
                        fill.append((named[key][0], n["phone_main"], n["phone_alt"]))
                elif key not in new_named:
                    new_named[key] = (sid, n["_company_id"], name, n["phone_main"], n["phone_alt"],
                                      False, "not_started", sfid)
            elif n["phone_main"] or n["email_address"]:
                if sid in generic:
                    n["_contact_id"] = generic[sid][0]
                    stats["generic_reused"] += 1
                    if generic[sid][1] is None and n["phone_main"]:
                        fill.append((generic[sid][0], n["phone_main"], n["phone_alt"]))
                elif sid not in new_generic:
                    new_generic[sid] = (sid, n["_company_id"], n["phone_main"], n["phone_alt"],
                                        True, "not_started", sfid)
        if new_named:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.contacts
                    (site_id, company_id, full_name, phone_main, phone_alt, is_generic_contact,
                     enrichment_status, source_file_id)
                VALUES %s RETURNING site_id, full_name, id""",
                list(new_named.values()), fetch=True, page_size=PAGE)
            for sid, name, cid in res:
                named[(sid, name)] = (cid, None, None)
            stats["named_inserted"] = len(res)
        if new_generic:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.contacts
                    (site_id, company_id, phone_main, phone_alt, is_generic_contact,
                     enrichment_status, source_file_id)
                VALUES %s RETURNING site_id, id""",
                list(new_generic.values()), fetch=True, page_size=PAGE)
            for sid, cid in res:
                generic[sid] = (cid, None, None)
            stats["generic_inserted"] = len(res)
        if fill:
            psycopg2.extras.execute_values(cur, """
                UPDATE staging.contacts c
                   SET phone_main = COALESCE(c.phone_main, v.pm),
                       phone_alt  = COALESCE(c.phone_alt,  v.pa)
                  FROM (VALUES %s) AS v(id, pm, pa)
                 WHERE c.id = v.id::uuid""", fill, page_size=PAGE)
            stats["phones_filled_on_existing"] = len(fill)
        for n in rows:
            sid = n.get("_site_id")
            if not sid or n.get("_contact_id"):
                continue
            name = n.get("contact_full_name")
            if name and (sid, name) in named:
                n["_contact_id"] = named[(sid, name)][0]
            elif not name and sid in generic:
                n["_contact_id"] = generic[sid][0]
    conn.commit()
    return stats


def stage_emails(conn, sfid, rows) -> dict:
    stats = Counter()
    recs, contact_ids = {}, set()
    for n in rows:
        cid = n.get("_contact_id")
        if not cid:
            continue
        for col, norm, verdict, _ in n["_emails"]:
            if verdict == "candidate":
                recs[(cid, norm)] = (cid, norm, False, "candidate", sfid, f"client_file:{col}")
                contact_ids.add(cid)
    with conn.cursor() as cur:
        if recs:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.emails
                    (contact_id, email_address, is_primary, verification_status, source_file_id, source)
                VALUES %s ON CONFLICT (contact_id, email_address) DO NOTHING""",
                list(recs.values()), page_size=PAGE)
            # exactly one primary per contact: the oldest address, if none is primary yet
            cur.execute("""
                UPDATE staging.emails e SET is_primary = TRUE
                WHERE e.id IN (
                    SELECT DISTINCT ON (contact_id) id FROM staging.emails x
                    WHERE x.contact_id = ANY(%s::uuid[])
                      AND NOT EXISTS (SELECT 1 FROM staging.emails p
                                      WHERE p.contact_id = x.contact_id AND p.is_primary)
                    ORDER BY contact_id, created_at, id)""", (list(contact_ids),))
            stats["primaries_set"] = cur.rowcount
        stats["candidates"] = len(recs)
    conn.commit()
    return stats


def stage_contact_points(conn, sfid, rows) -> dict:
    stats = Counter()
    recs = []
    for n in rows:
        cid = n.get("_company_id")
        if not cid:
            continue
        for rank, (col, canonical, surtaxe) in enumerate(n["_phones"]):
            recs.append((cid, n.get("_site_id"), n.get("_contact_id"), "phone",
                         phone_digits(canonical), canonical, f"client_file:{col}", None, None,
                         not surtaxe, surtaxe, rank, json_dumps({"column": col}), sfid))
            stats["phone"] += 1
        for col, norm, verdict, ev in n["_emails"]:
            recs.append((cid, n.get("_site_id"), n.get("_contact_id"), "email", norm, norm,
                         f"client_file:{col}", None, verdict, None, None, None,
                         json_dumps(ev) if ev else None, sfid))
            stats["email_" + verdict] += 1
    for start in range(0, len(recs), 5000):
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.contact_points
                    (company_id, site_id, contact_id, kind, value_norm, value_raw, source,
                     confidence, verdict, is_dialable, is_surtaxe, rank_hint, evidence, source_file_id)
                VALUES %s ON CONFLICT (company_id, kind, value_norm, source) DO NOTHING""",
                recs[start:start + 5000], page_size=PAGE)
        conn.commit()
    return stats


def stage_attributes(conn, sfid, rows, sector: str) -> int:
    merged = defaultdict(dict)
    for n in rows:
        if n.get("_company_id") and n["_attrs"]:
            merged[n["_company_id"]].update(n["_attrs"])
    recs = [(cid, sector, json_dumps(attrs), sfid) for cid, attrs in merged.items()]
    with conn.cursor() as cur:
        if recs:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.company_attributes (company_id, sector, attrs, source_file_id)
                VALUES %s
                ON CONFLICT (company_id, sector) DO UPDATE SET
                    attrs = staging.company_attributes.attrs || EXCLUDED.attrs,
                    updated_at = now()""", recs, page_size=PAGE)
    conn.commit()
    return len(recs)


def stage_finish(conn, sfid, n_raw: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE staging.source_files SET row_count_imported = %s WHERE id = %s",
                    (n_raw, sfid))
    conn.commit()


# ─── driver ──────────────────────────────────────────────────────────────────

def load_frame(path: Path, spec: dict):
    df = pd.read_excel(path, sheet_name=spec["sheet"], dtype=str, engine="calamine")
    df = df.where(df.notna(), other=None)
    return df


def run(spec: dict, dry_run: bool, limit: int | None) -> int:
    path = DATA_DIR / spec["filename"]
    if spec.get("skip"):
        print(f"[SKIP by spec] {spec['filename']}: {spec['notes']}")
        return 0
    if spec["sector"] not in source_to_sector_key():
        sys.exit(f"spec sector '{spec['sector']}' has no rule set in config/sector_rules.py")
    print(f"\n{'=' * 70}\n{spec['filename']}  (sector={spec['sector']}, stage={spec['pipeline_stage']})\n{'=' * 70}")
    file_hash = sha256_file(path)
    print(f"  SHA-256 : {file_hash[:16]}…")

    df = load_frame(path, spec)
    check_column_contract(path, df, spec["col_map"], spec["required"])
    n_raw = len(df)
    frame_hook = PRE_FRAME.get(spec.get("pre_clean"))
    work = frame_hook(df) if frame_hook else df
    if limit:
        work = work.head(limit)
    rows = normalize_rows(work, spec, file_hash)
    describe(rows, n_raw)
    if dry_run:
        print("\n  DRY RUN — nothing written.")
        return 0

    conn = get_conn()
    try:
        sfid, state = stage_register(conn, path, spec, file_hash, n_raw)
        if state == "done":
            print(f"  [SKIP] Already imported (source_file_id={sfid}).")
            return 0
        print(f"  source_file_id={sfid} ({state})")
        print(f"  raw       written={stage_raw(conn, sfid, df):,}")
        print(f"  companies {dict(stage_companies(conn, sfid, rows))}")
        print(f"  members   written={stage_membership(conn, sfid, rows):,}")
        print(f"  sites     {dict(stage_sites(conn, sfid, rows))}")
        print(f"  contacts  {dict(stage_contacts(conn, sfid, rows))}")
        print(f"  emails    {dict(stage_emails(conn, sfid, rows))}")
        print(f"  points    {dict(stage_contact_points(conn, sfid, rows))}")
        print(f"  attrs     companies={stage_attributes(conn, sfid, rows, spec['sector']):,}")
        if limit:
            print(f"  --limit {limit}: row_count_imported left NULL so the full run resumes this file.")
        else:
            stage_finish(conn, sfid, n_raw)
            print(f"  finish    row_count_imported={n_raw:,}")
        print("  Done. Now run: python scripts/check_data_quality.py --strict")
    finally:
        conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="exact file name under the data folder (see --list)")
    ap.add_argument("--list", action="store_true", help="print the known specs and exit")
    ap.add_argument("--dry-run", action="store_true", help="normalise and describe; write nothing")
    ap.add_argument("--limit", type=int, help="pilot: load only the first N rows (resumable)")
    args = ap.parse_args()
    if args.list:
        for s in FILE_SPECS:
            flag = "SKIP " if s.get("skip") else "     "
            print(f"{flag}{s['sector']:<18} {s['filename']}")
        return 0
    if not args.file:
        ap.error("--file is required (or --list)")
    return run(spec_for(args.file), args.dry_run, args.limit)


if __name__ == "__main__":
    sys.exit(main())
