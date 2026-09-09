"""
M5-S19 — Provider witness: the tourism rows already in Supabase, as a CSV listing
================================================================================
The M4 ingestion loaded the provider's "tourisme" file (48,749 rows France-
wide). For depts 03/63 it holds ~970 lodgings with a phone and ~600 with an
e-mail. Provider phones agreed 73 % with our measured sources on dept-13
bakeries, so this is a WITNESS (corroboration, e-mail candidates, dirigeant
names) — never a dialled source on its own (m5_s9 ranks it last, m5_s8
matches it by SIRET first).

ONE read-only SELECT, connection opened and closed inside this script; the
rest of M5 never touches the database (the pooler-timeout lesson, three times).

Output: exports/hebergement/checkpoints/provider_tourisme.csv  (';')

Usage:
    python scripts/m5_s19_provider.py
"""

import csv
import logging
import os
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone, is_surtaxe   # noqa: E402
from m5_lib import CHECK_DIR, DEPARTEMENTS                 # noqa: E402

OUT_PATH = CHECK_DIR / "provider_tourisme.csv"
FIELDS = ["dept", "listing_id", "name", "siret", "siren", "dirigeant", "phone", "mobile", "email",
          "website", "address", "postcode", "city", "naf", "label", "source_file"]

SQL = """
SELECT c.id AS company_id, c.siren, s.siret, c.legal_name, c.trade_name, c.naf_code, c.naf_label,
       s.address_line1, s.postal_code, s.city, s.website_url, f.file_name,
       (SELECT string_agg(DISTINCT cp.value_norm, '|')
          FROM staging.contact_points cp
         WHERE cp.company_id = c.id AND cp.kind = 'phone' AND cp.is_dialable) AS phones,
       (SELECT string_agg(DISTINCT cp.value_norm, '|')
          FROM staging.contact_points cp
         WHERE cp.company_id = c.id AND cp.kind = 'email'
           AND coalesce(cp.verdict, '') NOT IN ('invalid', 'invalide')) AS emails,
       (SELECT ct.full_name FROM staging.contacts ct
         WHERE ct.company_id = c.id AND ct.full_name IS NOT NULL
         ORDER BY ct.id LIMIT 1) AS dirigeant
  FROM staging.companies c
  JOIN staging.sites s ON s.company_id = c.id
  JOIN staging.source_files f ON f.id = c.source_file_id
 WHERE f.sector = 'tourisme' AND left(s.postal_code, 2) = ANY(%s)
"""

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m5_s19")


def db_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("SUPABASE_DB_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        sys.exit("SUPABASE_DB_URL missing from .env")
    return url


def main() -> None:
    import psycopg2
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(db_url(), keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL, (list(DEPARTEMENTS),))
            cols = [d[0] for d in cur.description]
            raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    rows = []
    for r in raw:
        phones = [normalize_fr_phone(p) for p in (r["phones"] or "").split("|") if p]
        phones = [p for p in phones if p and not is_surtaxe(p)]
        land = [p for p in phones if not p.startswith(("06", "07"))]
        mob = [p for p in phones if p.startswith(("06", "07"))]
        emails = [e.lower() for e in (r["emails"] or "").split("|") if e and "@" in e]
        rows.append({
            "dept": (r["postal_code"] or "")[:2], "listing_id": f"PV{r['company_id']}",
            "name": (r["trade_name"] or r["legal_name"] or "").strip(),
            "siret": r["siret"] or "", "siren": r["siren"] or "",
            "dirigeant": (r["dirigeant"] or "").strip(),
            "phone": land[0] if land else (mob[0] if mob else ""),
            "mobile": (mob[0] if land and mob else (mob[1] if len(mob) > 1 else "")),
            "email": emails[0] if emails else "",
            "website": r["website_url"] or "",
            "address": (r["address_line1"] or "").strip(), "postcode": r["postal_code"] or "",
            "city": (r["city"] or "").strip(), "naf": r["naf_code"] or "",
            "label": (r["naf_label"] or "").strip(), "source_file": r["file_name"] or "",
        })
    rows.sort(key=lambda x: (x["dept"], x["postcode"], x["name"]))
    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    log.info("─" * 62)
    log.info(f"written={len(rows)} provider rows -> {OUT_PATH.name}")
    for d in DEPARTEMENTS:
        sub = [x for x in rows if x["dept"] == d]
        log.info(f"[{d}] {len(sub)}: phone {sum(1 for x in sub if x['phone'])}, e-mail {sum(1 for x in sub if x['email'])}, "
                 f"SIRET {sum(1 for x in sub if len(x['siret']) == 14)}, dirigeant {sum(1 for x in sub if x['dirigeant'])}; "
                 f"labels {dict(Counter(x['label'][:20] for x in sub).most_common(5))}")


if __name__ == "__main__":
    main()
