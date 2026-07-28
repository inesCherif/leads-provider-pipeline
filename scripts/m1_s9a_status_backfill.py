r"""
M1-S9a — Recover the source activity status lost at ingestion
=============================================================
Backfills staging.sites.source_status from raw.ingest_rows.raw_json, so that
businesses the source file reported as closed ('Fermé') can be identified
before a campaign is mailed. Answers Sam's third ask (2026-07-26).

THE BUG THIS REPAIRS
    m1_s3_ingest.py COL_MAPS names the status column "Statut_Entreprise" for
    "Copie de agriculteurs total.xlsx", but that file's actual header is
    "Statut_Activite". row.get() on a missing key returns None, so the status
    silently became NULL for all 54,198 of its rows — 3,498 'Fermé' among them.
    File C mapped the same column correctly, which is why 2,947 sites already
    carry is_active=FALSE while file A's contributed none.

    Nothing was lost: raw.ingest_rows.raw_json keeps every source column
    verbatim. That is the whole point of the raw layer.

WHY THIS DOES NOT WRITE is_active
    'Fermé' is the data provider's own enrichment verdict, not a registry check
    we performed. v_qualified_contacts filters `is_active IS DISTINCT FROM
    false`, so writing it there would drop those businesses out of every view
    and export at once — shrinking the deliverable on someone else's say-so,
    silently. source_status records the claim; excluding on it is a business
    decision, taken deliberately via m1_s8_export.py --exclude-closed.

MATCHING
    Join key is SIRET. The SIRET is cleaned exactly the way m1_s3_ingest.py
    cleaned it (strip whitespace/dot/hyphen, require 14 digits) so this matches
    the same values that landed in staging.sites. File C's authoritative column
    is Siret_Verifie, file A's is Siret — COALESCE covers both.

    Sites with no SIRET cannot be matched and are reported, not guessed at.

CONFLICTS
    6 SIRETs carry both 'Actif' and 'Fermé' across rows (measured 2026-07-28).
    Resolution is deterministic: a definitive status beats 'Introuvable'/'Non
    trouvé', and among definitive ones 'Fermé' wins — for a mailing list,
    wrongly flagging a live business is cheaper than mailing a dead one.

Idempotent: only fills rows where source_status IS NULL. Safe to re-run.

Usage:
    python scripts/m1_s9a_status_backfill.py --dry-run   # report, write nothing
    python scripts/m1_s9a_status_backfill.py             # apply
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install psycopg2-binary python-dotenv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("m1_s9a")

SCRIPT_NAME = "m1_s9a_status_backfill.py"

# The source's own vocabulary. 'Introuvable' / 'Non trouvé' describe a failed
# lookup by the provider, NOT a closed business — they are stored verbatim for
# transparency but never count as closed.
CLOSED_PATTERNS = ("Ferm%", "Cess%", "Radi%")
ACTIVE_PATTERNS = ("Actif%",)


def _ilike_any(col: str, patterns: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f"{col} ILIKE '{p}'" for p in patterns) + ")"


IS_CLOSED = _ilike_any("st", CLOSED_PATTERNS)
IS_ACTIVE = _ilike_any("st", ACTIVE_PATTERNS)

# Extract (siret, status) from the raw layer, one winner per SIRET.
#   - siret cleaned exactly as m1_s3_ingest.clean_siret did: strip whitespace,
#     dot and hyphen, then require exactly 14 digits.
#   - Siret_Verifie is file C's authoritative column; file A only has Siret.
SOURCE_CTE = rf"""
WITH raw_status AS (
    SELECT
        regexp_replace(
            COALESCE(r.raw_json->>'Siret_Verifie', r.raw_json->>'Siret'),
            '[\s.-]', '', 'g'
        ) AS siret,
        btrim(r.raw_json->>'Statut_Activite') AS st
    FROM raw.ingest_rows r
    WHERE r.raw_json->>'Statut_Activite' IS NOT NULL
),
clean AS (
    SELECT
        siret,
        st,
        {IS_CLOSED}                 AS is_closed,
        ({IS_CLOSED} OR {IS_ACTIVE}) AS is_definitive
    FROM raw_status
    WHERE siret ~ '^[0-9]{{14}}$'
      AND st <> ''
),
best AS (
    -- One status per SIRET. Definitive beats 'Introuvable'; closed beats active.
    -- Final tiebreak on the literal keeps the choice reproducible across runs.
    SELECT DISTINCT ON (siret) siret, st, is_closed
    FROM clean
    ORDER BY siret, is_definitive DESC, is_closed DESC, st
)
"""

UPDATE_SQL = SOURCE_CTE + """
UPDATE staging.sites s
SET source_status               = b.st,
    source_status_backfilled_at = NOW()
FROM best b
WHERE s.siret = b.siret
  AND s.source_status IS NULL
"""

# What the backfill would touch, without touching it.
PREVIEW_SQL = SOURCE_CTE + """
SELECT b.st, b.is_closed, count(*) AS sites
FROM best b
JOIN staging.sites s ON s.siret = b.siret
WHERE s.source_status IS NULL
GROUP BY b.st, b.is_closed
ORDER BY sites DESC
"""

# Deliverable impact: how many businesses in the CURRENT export selection are
# affected. Mirrors m1_s8_export.py's two collapse passes so the number quoted
# here is the number that will appear in the file.
IMPACT_SQL = """
WITH per_business AS (
    SELECT DISTINCT ON (business_id)
        business_id, shared_address_group, tier, source_status, source_closed,
        email_address, phone_main
    FROM public.v_qualified_contacts
    WHERE NOT is_duplicate
    ORDER BY business_id,
             (email_address IS NOT NULL) DESC,
             (tier = 'tier1_official_naf') DESC,
             email_verified DESC NULLS LAST,
             full_name NULLS LAST, email_address NULLS LAST, phone_main NULLS LAST
),
per_family AS (
    SELECT DISTINCT ON (COALESCE(shared_address_group, business_id::text)) *
    FROM per_business
    ORDER BY COALESCE(shared_address_group, business_id::text),
             (email_address IS NOT NULL) DESC,
             (tier = 'tier1_official_naf') DESC,
             business_id
)
SELECT
    count(*)                                            AS deliverable,
    count(*) FILTER (WHERE source_closed)                AS closed,
    count(*) FILTER (WHERE source_closed
                       AND email_address IS NOT NULL)    AS closed_with_email,
    count(*) FILTER (WHERE source_status IS NULL)         AS status_unknown
FROM per_family
"""

UNMATCHABLE_SQL = """
SELECT
    count(*) FILTER (WHERE siret IS NULL) AS sites_without_siret,
    count(*)                              AS sites_total
FROM staging.sites
"""


def connect(attempts: int = 4):
    """Connect with keepalives, retrying transient pooler failures.

    The pooler intermittently rejects a fresh connection (EAUTHTIMEOUT) and
    drops long-running ones mid-query (SSL SYSCALL EOF). Keepalives hold the
    socket open; the retry covers the rest. Same pattern as m1_s8_export.py.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    for attempt in range(1, attempts + 1):
        try:
            return psycopg2.connect(
                url, keepalives=1, keepalives_idle=30,
                keepalives_interval=10, keepalives_count=5,
            )
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if attempt == attempts:
                raise
            wait = 3 * attempt
            log.warning("Connection attempt %d/%d failed (%s) — retrying in %ds",
                        attempt, attempts, str(e).strip().splitlines()[0], wait)
            time.sleep(wait)


def report_impact(cur) -> dict:
    cur.execute(IMPACT_SQL)
    d, closed, closed_mail, unknown = cur.fetchone()
    log.info("─" * 68)
    log.info("Deliverable businesses          %7d", d)
    log.info("  reported CLOSED by source     %7d", closed)
    log.info("    of which have an email      %7d", closed_mail)
    log.info("  status still unknown          %7d", unknown)
    log.info("─" * 68)
    return {"deliverable": d, "closed": closed,
            "closed_with_email": closed_mail, "status_unknown": unknown}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="M1-S9a — backfill source activity status from the raw layer")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    args = ap.parse_args()

    conn = connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(UNMATCHABLE_SQL)
            no_siret, total_sites = cur.fetchone()
            log.info("Sites: %d total, %d without a SIRET (unmatchable here)",
                     total_sites, no_siret)

            log.info("Statuses recoverable from raw.ingest_rows:")
            cur.execute(PREVIEW_SQL)
            preview = cur.fetchall()
            if not preview:
                log.info("  none — every matchable site already has a status")
            projected = 0
            for st, is_closed, n in preview:
                projected += n
                log.info("   %-14s %-8s %7d", st, "CLOSED" if is_closed else "", n)
            log.info("Projected sites to update: %d", projected)

            if args.dry_run:
                log.info("Impact on the deliverable, as it stands BEFORE the backfill:")
                report_impact(cur)
                conn.rollback()
                log.info("--dry-run: nothing written")
                return

            cur.execute(UPDATE_SQL)
            updated = cur.rowcount
            log.info("Updated %d sites", updated)

            summary = dict(report_impact(cur), sites_updated=updated,
                           sites_without_siret=no_siret)
            cur.execute(
                """
                INSERT INTO audit.audit_log
                    (table_name, record_id, field_changed, old_value, new_value,
                     changed_by, reason)
                VALUES
                    ('staging.sites', NULL, 'source_status', NULL, %s, %s, %s)
                """,
                (
                    json.dumps(summary, ensure_ascii=False),
                    SCRIPT_NAME,
                    "M1-S9a: recovered Statut_Activite from raw.ingest_rows after the "
                    "m1_s3 COL_MAP named it Statut_Entreprise for file A",
                ),
            )
        conn.commit()
        log.info("Committed.")
    except Exception:
        conn.rollback()
        log.error("Rolled back — nothing was written.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
