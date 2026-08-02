r"""
M1-S9-K — Match scraped directory listings to our companies
============================================================
S9-J scrapes listings into staging.directory_listings and deliberately stops
there. This step decides which listing is which company, and it is the risky
half: a listing carries NO SIRET and NO postcode, only a name, a commune and a
department. Matching on a name alone attaches a stranger's email to a business.

⚠ THAT IS NOT HYPOTHETICAL — MEASURED ON THE 388-LISTING PILOT
Exact normalised-name matches, split by whether the location agreed:

    commune agrees        25 listings   ->  11 would gain an email   SAFE
    our city is NULL      13 listings   ->  12 would gain an email   needs dept
    commune DISAGREES      9 listings   ->   4 would gain an email   WRONG

Farm names are generic. 'GAEC DU LAC' in Luppy (Moselle) matched our 'GAEC DU
LAC' in Saint-Maurice-de-Lignon (Haute-Loire). 'SCEA DE LA PLAINE' matched two
different companies in two different departments. Name-only matching would have
injected those four wrong addresses straight into the client's file.

THE RULES, strongest first. Every one requires location agreement.

    exact_name_commune     name identical AND commune == our city
    exact_name_department  name identical AND our city is NULL
                           AND the directory's department == our department_code
    fuzzy_commune          similarity >= 0.55 AND commune == our city

AMBIGUITY IS REJECTION
    If a listing matches more than one distinct business, it is skipped. Two
    companies of the same name in the same commune cannot be told apart from a
    directory page, and guessing is exactly the failure above.

WHY DEPARTMENT AND NOT JUST COMMUNE
    12,828 deliverable rows have no department and many have no city, so
    commune-only matching throws away the 13 NULL-city listings that were the
    single richest group (12 of 13 would gain an email). The department comes
    from the listing URL, which is structural, and our side derives from the
    postal code.

TWO PHASES, SEPARATELY REVERSIBLE
    default   record the match on staging.directory_listings only
    --apply   additionally INSERT the email into staging.emails

    Emails are inserted as 'candidate' with verifier_tool='s9k:<method>' so
    S9-G can verify them like any other, and so they are trivially reversible.
    Never INSERT a contact row — see the standing rule in CLAUDE.md.

Usage:
    python scripts/m1_s9k_match_listings.py --dry-run
    python scripts/m1_s9k_match_listings.py                 # record matches
    python scripts/m1_s9k_match_listings.py --apply         # + insert emails

Reversal:
    UPDATE staging.directory_listings
       SET matched_company_id=NULL, match_method=NULL, match_score=NULL,
           matched_at=NULL;
    UPDATE staging.emails SET verification_status='invalid'
     WHERE verifier_tool LIKE 's9k:%';
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m1_s9k")

SCRIPT_NAME = "m1_s9k_match_listings.py"
FUZZY_MIN = 0.55

# Department name (unaccented, upper, no punctuation) -> INSEE code. The
# directory encodes the department in its URL, which is structural and always
# present; our side derives department_code from the postal code.
DEPARTMENTS = {
    "AIN": "01", "AISNE": "02", "ALLIER": "03", "ALPES DE HAUTE PROVENCE": "04",
    "HAUTES ALPES": "05", "ALPES MARITIMES": "06", "ARDECHE": "07",
    "ARDENNES": "08", "ARIEGE": "09", "AUBE": "10", "AUDE": "11",
    "AVEYRON": "12", "BOUCHES DU RHONE": "13", "CALVADOS": "14", "CANTAL": "15",
    "CHARENTE": "16", "CHARENTE MARITIME": "17", "CHER": "18", "CORREZE": "19",
    "CORSE DU SUD": "2A", "HAUTE CORSE": "2B", "COTE D OR": "21",
    "COTES D ARMOR": "22", "CREUSE": "23", "DORDOGNE": "24", "DOUBS": "25",
    "DROME": "26", "EURE": "27", "EURE ET LOIR": "28", "FINISTERE": "29",
    "GARD": "30", "HAUTE GARONNE": "31", "GERS": "32", "GIRONDE": "33",
    "HERAULT": "34", "ILLE ET VILAINE": "35", "INDRE": "36",
    "INDRE ET LOIRE": "37", "ISERE": "38", "JURA": "39", "LANDES": "40",
    "LOIR ET CHER": "41", "LOIRE": "42", "HAUTE LOIRE": "43",
    "LOIRE ATLANTIQUE": "44", "LOIRET": "45", "LOT": "46",
    "LOT ET GARONNE": "47", "LOZERE": "48", "MAINE ET LOIRE": "49",
    "MANCHE": "50", "MARNE": "51", "HAUTE MARNE": "52", "MAYENNE": "53",
    "MEURTHE ET MOSELLE": "54", "MEUSE": "55", "MORBIHAN": "56",
    "MOSELLE": "57", "NIEVRE": "58", "NORD": "59", "OISE": "60", "ORNE": "61",
    "PAS DE CALAIS": "62", "PUY DE DOME": "63", "PYRENEES ATLANTIQUES": "64",
    "HAUTES PYRENEES": "65", "PYRENEES ORIENTALES": "66", "BAS RHIN": "67",
    "HAUT RHIN": "68", "RHONE": "69", "HAUTE SAONE": "70",
    "SAONE ET LOIRE": "71", "SARTHE": "72", "SAVOIE": "73", "HAUTE SAVOIE": "74",
    "PARIS": "75", "SEINE MARITIME": "76", "SEINE ET MARNE": "77",
    "YVELINES": "78", "DEUX SEVRES": "79", "SOMME": "80", "TARN": "81",
    "TARN ET GARONNE": "82", "VAR": "83", "VAUCLUSE": "84", "VENDEE": "85",
    "VIENNE": "86", "HAUTE VIENNE": "87", "VOSGES": "88", "YONNE": "89",
    "TERRITOIRE DE BELFORT": "90", "ESSONNE": "91", "HAUTS DE SEINE": "92",
    "SEINE SAINT DENIS": "93", "VAL DE MARNE": "94", "VAL D OISE": "95",
    "GUADELOUPE": "971", "MARTINIQUE": "972", "GUYANE": "973",
    "LA REUNION": "974", "REUNION": "974", "MAYOTTE": "976",
}


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.upper().replace("'", " ").replace("-", " ").split())


def dept_code(name: str | None) -> str | None:
    return DEPARTMENTS.get(norm(name))


# One scan of the view, joined to the listings. The view is a two-pass
# DISTINCT ON over 128k rows, so it must never be evaluated per listing —
# doing that timed out at 388 rows during measurement.
MATCH_SQL = """
WITH v AS MATERIALIZED (
  SELECT business_id, display_name, city, department_code, email_address,
         upper(unaccent(display_name))            AS dn,
         upper(unaccent(coalesce(city, '')))      AS dc
  FROM public.v_deliverable_businesses
), l AS MATERIALIZED (
  SELECT id, business_name, commune, department_name, email, contact_name,
         upper(unaccent(business_name))           AS ln,
         upper(unaccent(commune))                 AS lc
  FROM staging.directory_listings
  WHERE email IS NOT NULL AND matched_at IS NULL
)
SELECT l.id, l.business_name, l.commune, l.department_name, l.email,
       v.business_id, v.display_name, v.city, v.department_code,
       v.email_address IS NOT NULL AS already_has_email,
       similarity(l.ln, v.dn)      AS score,
       (l.ln = v.dn)               AS exact,
       (v.dc = l.lc)               AS commune_agrees,
       (coalesce(v.dc, '') = '')   AS our_city_null
FROM l
JOIN v ON (l.ln = v.dn) OR (v.dc = l.lc AND similarity(l.ln, v.dn) >= %s)
"""

RECORD_SQL = """
UPDATE staging.directory_listings AS d
SET matched_company_id = v.cid::uuid,
    match_method       = v.method,
    match_score        = v.score::numeric,
    matched_at         = NOW()
FROM (VALUES %s) AS v(lid, cid, method, score)
WHERE d.id = v.lid::uuid AND d.matched_at IS NULL
"""

# Attach to the company's contact that has no address yet; never create one.
INSERT_EMAIL_SQL = """
INSERT INTO staging.emails
    (contact_id, email_address, is_primary, verification_status,
     verifier_tool, source_file_id)
SELECT c.id, v.email, TRUE, 'candidate', v.tool, c.source_file_id
FROM (VALUES %s) AS v(cid, email, tool)
JOIN LATERAL (
    SELECT ct.id, ct.source_file_id
    FROM staging.contacts ct
    WHERE ct.company_id = v.cid::uuid
    ORDER BY (EXISTS (SELECT 1 FROM staging.emails e
                      WHERE e.contact_id = ct.id
                        AND e.verification_status <> 'invalid')) ASC,
             ct.created_at, ct.id
    LIMIT 1
) c ON TRUE
ON CONFLICT (contact_id, email_address) DO NOTHING
"""

AUDIT_SQL = """
INSERT INTO audit.audit_log
    (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
VALUES ('staging.directory_listings', NULL, 'matched_company_id', NULL, %s, %s, %s)
"""


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


def classify(row: dict) -> tuple[str, float] | None:
    """Return (method, score) when the location agrees, else None."""
    if row["exact"] and row["commune_agrees"]:
        return "exact_name_commune", 1.0
    if row["exact"] and row["our_city_null"]:
        dc = dept_code(row["department_name"])
        if dc and dc == (row["department_code"] or ""):
            return "exact_name_department", 0.9
        return None
    if row["commune_agrees"] and (row["score"] or 0) >= FUZZY_MIN:
        return "fuzzy_commune", float(row["score"])
    return None                      # commune disagrees, or no location evidence


def run(args) -> None:
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    log.info("Matching listings (fuzzy floor %.2f)...", FUZZY_MIN)
    cur.execute(MATCH_SQL, (FUZZY_MIN,))
    rows = cur.fetchall()
    log.info("  candidate pairs returned: %d", len(rows))

    best: dict[str, dict] = {}
    rejected_location = set()
    for r in rows:
        verdict = classify(r)
        if verdict is None:
            rejected_location.add(r["id"])
            continue
        method, score = verdict
        prev = best.get(r["id"])
        cand = {**r, "method": method, "score_final": score}
        if prev is None:
            best[r["id"]] = cand
        elif prev["business_id"] != r["business_id"]:
            prev["ambiguous"] = True          # two DIFFERENT businesses -> reject
        elif score > prev["score_final"]:
            best[r["id"]] = cand

    ambiguous = {k for k, v in best.items() if v.get("ambiguous")}
    accepted = {k: v for k, v in best.items() if k not in ambiguous}
    gains = [v for v in accepted.values() if not v["already_has_email"]]

    log.info("")
    log.info("  accepted matches        %5d", len(accepted))
    log.info("    ...NEW email for us   %5d  <- the number that matters", len(gains))
    log.info("    ...already had one    %5d", len(accepted) - len(gains))
    log.info("  rejected: ambiguous     %5d  (same name, 2+ businesses)", len(ambiguous))
    log.info("  rejected: location      %5d  (commune/department disagrees)",
             len(rejected_location - set(accepted)))
    by_method: dict[str, int] = {}
    for v in accepted.values():
        by_method[v["method"]] = by_method.get(v["method"], 0) + 1
    for m, n in sorted(by_method.items(), key=lambda t: -t[1]):
        log.info("    %-24s %5d", m, n)

    log.info("")
    log.info("  sample of NEW emails (hand-check these):")
    for v in gains[:25]:
        log.info("    %-34s | %-22s | %-30s | %s",
                 (v["business_name"] or "")[:34], (v["commune"] or "")[:22],
                 (v["display_name"] or "")[:30], v["email"])

    if args.dry_run:
        log.info("DRY-RUN — nothing written.")
        conn.rollback(); conn.close(); return

    w = conn.cursor()
    recs = [(k, str(v["business_id"]), v["method"], v["score_final"])
            for k, v in accepted.items()]
    written = 0
    for i in range(0, len(recs), 500):
        psycopg2.extras.execute_values(w, RECORD_SQL, recs[i:i + 500], page_size=500)
        written += w.rowcount
    log.info("  listings marked matched %d", written)

    inserted = 0
    if args.apply and gains:
        payload = [(str(v["business_id"]), v["email"], "s9k:" + v["method"])
                   for v in gains]
        for i in range(0, len(payload), 500):
            psycopg2.extras.execute_values(
                w, INSERT_EMAIL_SQL, payload[i:i + 500], page_size=500)
            inserted += w.rowcount
        log.info("  emails inserted        %d", inserted)

    w.execute(AUDIT_SQL, (
        json.dumps({"step": "S9-K", "script": SCRIPT_NAME,
                    "accepted": len(accepted), "new_emails": len(gains),
                    "ambiguous_rejected": len(ambiguous),
                    "location_rejected": len(rejected_location - set(accepted)),
                    "emails_inserted": inserted, "by_method": by_method},
                   ensure_ascii=False),
        SCRIPT_NAME,
        "Match directory listings to companies; every rule requires location "
        "agreement, ambiguous names are rejected",
    ))
    conn.commit()
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Match directory listings")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="also INSERT the matched emails into staging.emails")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
