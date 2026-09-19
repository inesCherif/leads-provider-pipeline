"""
M7-S19 — What Supabase already knows about the PRODUCTEURS of depts 03 / 63, as listing CSVs
============================================================================================
Copy of `m6_s19_supabase.py` for the M7 population (agriculture minus
livestock, `m7_lib.NAF_SCOPE`). Two read-only SELECTs, one connection each,
opened and closed inside this script; the rest of M7 never touches the
database (the pooler-timeout lesson, three times).

(1) provider_agri.csv — the client's own files (M1 `agriculture` + M4
    `livestock` sectors, national). A row is taken when its NAF is one of
    the 29 M7 codes, or when the provider gave no NAF and its Activité
    label names neither an élevage nor an excluded activity (wine, cider,
    beer, pork — the principle). Counted 2026-09-11 before writing this:
    03 = 131 rows / 129 phones / 23 e-mails (7 SMTP-verified), 63 = 234 /
    231 / 50 (18); SIRET on 77 + 126. Provider phones agreed 73 % with
    measured sources (dept-13 bakeries), so this is a WITNESS: it
    corroborates, fills `telephone_piste`, and reaches Maha's Probable tab
    only (m7_s14). Provider e-mails carry the S9-G SMTP verdict
    (`email_verified`) — a verified one ships as `vérifié`, the others go
    through m7_s11. Rows whose primary sector is agriculteurs_bio are left
    out: M3AG's Agence Bio listings already bring them.

(2) db_claims.csv — every phone / e-mail / website claim in
    `staging.contact_points` for a 03/63 company that is a bio operator or
    carries NAF 01.x / 10.x / 03.x, WITH its source, verdict and is_dialable.
    Each claim keeps its own rank in m7_s8/m7_s9 (a `pagesjaunes` claim is a
    Pages Jaunes witness, never "provider"); e-mails proven `invalid` there
    are an exclusion list (m7_s11 never probes them again).

Usage:
    python scripts/m7_s19_supabase.py
"""

import argparse
import csv
import logging
import os
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone, is_surtaxe   # noqa: E402
from m7_lib import CHECK_DIR, DEPARTEMENTS, NAF_SCOPE, EXCLUDED_NAF, dept_of_cp   # noqa: E402
from france_lib import parse_departements                  # noqa: E402

PROVIDER_PATH = CHECK_DIR / "provider_agri.csv"
CLAIMS_PATH = CHECK_DIR / "db_claims.csv"
PROVIDER_FIELDS = ["dept", "listing_id", "name", "siret", "siren", "dirigeant", "phone", "mobile", "email",
                   "email_verified", "email_status", "website", "address", "postcode", "city", "naf", "label",
                   "sirene_etat", "in_liquidation", "primary_sector", "source_file"]
CLAIMS_FIELDS = ["dept", "listing_id", "company_id", "name", "siret", "siren", "numero_bio", "kind",
                 "phone", "mobile", "email", "website", "source", "verdict", "is_dialable",
                 "address", "postcode", "city", "sector"]

# a NAF-less provider row is NOT a producteur when its label says élevage
# (M6's population) or an excluded activity (the principle)
NOT_PRODUCTEUR_LABEL_RE = (r"(elev|bovin|ovin|caprin|porc|volaille|vache|brebis|chevr|poule|"
                           r"equin|cheval|haras|apicult|abeille|production animale|"
                           r"vin\b|vins\b|vign|viticult|cidr|biere|brass|charcut)")

SQL_PROVIDER = """
SELECT v.company_id, v.siren, v.siret, v.legal_name, v.trade_name, v.display_name,
       v.naf_code, v.naf_label, v.address_line1, v.postal_code, v.city, v.department_code,
       v.website_domain, v.full_name, v.phone_main, v.email_address, v.email_status,
       v.email_verified, v.sirene_etat, v.in_liquidation, v.primary_sector, v.data_source
  FROM public.v_deliverable_businesses v
 WHERE v.department_code = ANY(%s)
   AND NOT v.source_closed
   AND v.primary_sector <> 'agriculteurs_bio'
   AND ('agriculture' = ANY(v.sectors) OR 'livestock' = ANY(v.sectors)
        OR v.naf_code LIKE '01.%%' OR v.naf_code LIKE '10.%%' OR v.naf_code LIKE '03.%%')
   AND (v.naf_code = ANY(%s)
        OR (v.naf_code IS NULL AND unaccent(lower(coalesce(v.naf_label, ''))) !~ %s))
"""

SQL_CLAIMS = """
SELECT cp.company_id, cp.kind, cp.value_norm, cp.source, cp.verdict, cp.is_dialable, cp.is_surtaxe,
       c.siren, c.legal_name, c.trade_name, c.naf_code, sf.sector,
       s.siret, s.address_line1, s.postal_code, s.city,
       (SELECT ci.id_value FROM staging.company_identifiers ci
         WHERE ci.company_id = c.id AND ci.id_type = 'numero_bio' ORDER BY ci.created_at LIMIT 1) AS numero_bio
  FROM staging.contact_points cp
  JOIN staging.companies c ON c.id = cp.company_id
  JOIN staging.source_files sf ON sf.id = cp.source_file_id
  LEFT JOIN LATERAL (
        SELECT s1.siret, s1.address_line1, s1.postal_code, s1.city
          FROM staging.sites s1
         WHERE s1.company_id = c.id
         ORDER BY (s1.id = cp.site_id) DESC, s1.id
         LIMIT 1) s ON TRUE
 WHERE left(lpad(btrim(s.postal_code::text), 5, '0'), 2) = ANY(%s)
   AND cp.kind IN ('phone', 'email', 'website')
   AND (sf.sector = 'agriculteurs_bio' OR c.naf_code LIKE '01.%%' OR c.naf_code LIKE '10.%%' OR c.naf_code LIKE '03.%%')
"""

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m7_s19")


def db_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("SUPABASE_DB_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        sys.exit("SUPABASE_DB_URL missing from .env")
    return url


def fetch(sql: str, params: tuple) -> list[dict]:
    import psycopg2
    conn = psycopg2.connect(db_url(), keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def split_phones(values: list[str]) -> tuple[str, str]:
    phones = [normalize_fr_phone(p) for p in values if p]
    phones = [p for p in dict.fromkeys(phones) if p and not is_surtaxe(p)]
    land = [p for p in phones if not p.startswith(("06", "07"))]
    mob = [p for p in phones if p.startswith(("06", "07"))]
    phone = land[0] if land else (mob[0] if mob else "")
    mobile = mob[0] if land and mob else (mob[1] if len(mob) > 1 else "")
    return phone, mobile


def sql_depts(depts: list[str]) -> list[str]:
    """What to send to the SQL. The base stores Corsica three ways —
    measured 2026-09-19: department_code is '2A' (458 rows), '2B' (408) and a
    bare '20' (430) — and the claims query keys on left(postal_code,2), which
    is always '20'. So ask for all of them and let row_dept() decide."""
    out = list(depts)
    if {"2A", "2B"} & set(depts):
        out.append("20")
    return out


def row_dept(postcode: str, department_code: str) -> str:
    """The postcode decides, because department_code is '20' for a third of
    Corsica. Falls back to the stored code when there is no usable postcode."""
    return dept_of_cp(postcode) or (department_code or "")


def write_provider(raw: list[dict], wanted: set[str]) -> list[dict]:
    rows = []
    for r in raw:
        if (r["naf_code"] or "") in EXCLUDED_NAF:
            continue                                   # belt and braces: never a wine / pork code
        phone, mobile = split_phones([r["phone_main"]])
        email = (r["email_address"] or "").strip().lower()
        pc = str(r["postal_code"] or "").strip()
        if len(pc) == 4:
            pc = "0" + pc
        dept = row_dept(pc, r["department_code"])
        if dept not in wanted:
            continue                                   # the '20' rows land on 2A / 2B here
        rows.append({
            "dept": dept, "listing_id": f"PV{r['company_id']}",
            "name": (r["display_name"] or r["trade_name"] or r["legal_name"] or "").strip(),
            "siret": r["siret"] or "", "siren": r["siren"] or "",
            "dirigeant": (r["full_name"] or "").strip(),
            "phone": phone, "mobile": mobile,
            "email": email if "@" in email else "",
            "email_verified": "1" if r["email_verified"] else "",
            "email_status": r["email_status"] or "",
            "website": r["website_domain"] or "",
            "address": (r["address_line1"] or "").strip(), "postcode": pc,
            "city": (r["city"] or "").strip(), "naf": (r["naf_code"] or "").strip(),
            "label": (r["naf_label"] or "").strip(),
            "sirene_etat": r["sirene_etat"] or "", "in_liquidation": "1" if r["in_liquidation"] else "",
            "primary_sector": r["primary_sector"] or "", "source_file": r["data_source"] or "",
        })
    rows.sort(key=lambda x: (x["dept"], x["postcode"], x["name"]))
    with PROVIDER_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PROVIDER_FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    return rows


def write_claims(raw: list[dict], wanted: set[str]) -> list[dict]:
    rows = []
    for r in raw:
        if (r["naf_code"] or "") in EXCLUDED_NAF:
            continue
        kind, val = r["kind"], (r["value_norm"] or "").strip()
        if not val:
            continue
        phone = mobile = email = website = ""
        if kind == "phone":
            if r["is_surtaxe"]:
                continue
            p = normalize_fr_phone(val) or val
            if p.startswith(("06", "07")):
                mobile = p
            else:
                phone = p
        elif kind == "email":
            email = val.lower()
        else:
            website = val
        pc = str(r["postal_code"] or "").strip()
        if len(pc) == 4:
            pc = "0" + pc
        dept = row_dept(pc, pc[:2])
        if dept not in wanted:
            continue
        rows.append({
            "dept": dept, "listing_id": f"CP{r['company_id']}", "company_id": r["company_id"],
            "name": (r["trade_name"] or r["legal_name"] or "").strip(),
            "siret": r["siret"] or "", "siren": r["siren"] or "", "numero_bio": r["numero_bio"] or "",
            "kind": kind, "phone": phone, "mobile": mobile, "email": email, "website": website,
            "source": r["source"] or "", "verdict": r["verdict"] or "",
            "is_dialable": "" if r["is_dialable"] is None else ("1" if r["is_dialable"] else "0"),
            "address": (r["address_line1"] or "").strip(), "postcode": pc,
            "city": (r["city"] or "").strip(), "sector": r["sector"] or "",
        })
    rows.sort(key=lambda x: (x["dept"], x["postcode"], x["name"], x["kind"], x["source"]))
    with CLAIMS_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CLAIMS_FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default=",".join(DEPARTEMENTS),
                    help="comma list, a région name, or 'all' for the 96 of métropole")
    args = ap.parse_args()
    depts = parse_departements(args.departements)
    wanted = set(depts)
    verbose = len(depts) <= 6          # a per-dept block is unreadable at 96

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"{len(depts)} département(s): {', '.join(depts[:12])}{' …' if len(depts) > 12 else ''}")

    prov = write_provider(fetch(SQL_PROVIDER, (sql_depts(depts), sorted(NAF_SCOPE),
                                               NOT_PRODUCTEUR_LABEL_RE)), wanted)
    log.info("-" * 62)
    log.info(f"written={len(prov)} provider producteurs -> {PROVIDER_PATH.name} "
             f"({len({x['dept'] for x in prov})} départements, "
             f"phone {sum(1 for x in prov if x['phone'] or x['mobile'])}, "
             f"e-mail {sum(1 for x in prov if x['email'])})")
    for d in depts if verbose else []:
        sub = [x for x in prov if x["dept"] == d]
        log.info(f"[{d}] {len(sub)}: phone {sum(1 for x in sub if x['phone'] or x['mobile'])}, "
                 f"e-mail {sum(1 for x in sub if x['email'])} (verified {sum(1 for x in sub if x['email_verified'])}), "
                 f"SIRET {sum(1 for x in sub if len(x['siret']) == 14)}, SIREN {sum(1 for x in sub if len(x['siren']) == 9)}, "
                 f"dirigeant {sum(1 for x in sub if x['dirigeant'])}, no NAF {sum(1 for x in sub if not x['naf'])}; "
                 f"sectors {dict(Counter(x['primary_sector'] for x in sub))}")
    if not verbose:
        empty = [d for d in depts if not any(x["dept"] == d for x in prov)]
        log.info(f"départements with no provider row: {len(empty)}"
                 + (f" ({', '.join(empty[:20])})" if empty else ""))

    claims = write_claims(fetch(SQL_CLAIMS, (sql_depts(depts),)), wanted)
    log.info("-" * 62)
    log.info(f"written={len(claims)} contact_points claims -> {CLAIMS_PATH.name} "
             f"on {len({x['company_id'] for x in claims})} companies")
    for d in depts if verbose else []:
        sub = [x for x in claims if x["dept"] == d]
        by = Counter((x["kind"], x["source"].split("(")[0], x["verdict"] or ("dialable" if x["is_dialable"] == "1" else "")) for x in sub)
        log.info(f"[{d}] {len(sub)} claims on {len({x['company_id'] for x in sub})} companies; "
                 f"top {by.most_common(8)}")
    log.info(f"invalid e-mails (exclusion list): {sum(1 for x in claims if x['kind'] == 'email' and x['verdict'] in ('invalid', 'invalide'))}")


if __name__ == "__main__":
    main()
