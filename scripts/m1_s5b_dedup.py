"""
M1-S5b — Duplicate marking
===========================
Finds companies that are the same real business appearing more than once, and
links each duplicate to a chosen survivor.

NON-DESTRUCTIVE. No row is deleted and no data is merged or overwritten. A
duplicate keeps everything it has and gains a pointer:
    duplicate_of_company_id -> the survivor
Deduplication then happens in the views via coalesce(duplicate_of_company_id, id).
Undo the whole pass with:
    UPDATE staging.companies SET duplicate_of_company_id = NULL, dedup_checked_at = NULL;

WHY THIS EXISTS
M1-S3 deduplicated on SIRET only. A farm present in both source files - once with
a SIRET, once without - became two rows that never merged. Measured: 4,365
duplicates across the qualified set, 2,990 groups spanning both qualification
tiers. See docs/project_documentation.md section 5.2.

THE MATCHING KEY
Normalized name + postal code, where "normalized" means: unaccent, lowercase,
strip punctuation to spaces, then SORT the word tokens. Sorting is what collapses
"Lemoine Jean-Claude" and "JEAN-CLAUDE LEMOINE" - this data provider routinely
reverses first/last name order between files.

Phone is deliberately NOT used. An earlier attempt keyed on phone and produced a
12.5% duplicate rate that was wrong: numbers like +33890212903 and +33899105777
are French premium-rate service lines (0890/0899) shared across five different
departments. Phone is not an identity key in this dataset.

SURVIVOR SELECTION, in order:
    1. has a SIREN            - verifiable against INSEE, carries enrichment
    2. has a naf_code         - more complete
    3. earliest created_at    - first seen wins, consistent with the ingest merge
    4. lowest id              - deterministic tiebreak, so re-runs are stable

SCOPE: qualified + qualified_unverified only. Duplicates among pending and
disqualified companies are not marked - they are not delivered to anyone, and
leaving them unmarked keeps this pass cheap and reviewable.

Usage:
    python scripts/m1_s5b_dedup.py --dry-run     # always look first
    python scripts/m1_s5b_dedup.py
    python scripts/m1_s5b_dedup.py --force       # re-evaluate already-checked rows
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import psycopg2
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m1_s5b")

DEDUP_METHOD = "exact_name_postal"

# One row per company (a company may have several sites - take a deterministic one),
# with the normalized matching key attached.
KEYED_CTE = """
WITH one_site AS (
    SELECT DISTINCT ON (co.id)
           co.id, co.siren, co.naf_code, co.created_at,
           co.qualification_status,
           btrim(s.postal_code) AS pc,
           coalesce(co.legal_name, co.trade_name) AS raw_name
    FROM staging.companies co
    JOIN staging.sites s ON s.company_id = co.id
    WHERE co.qualification_status IN ('qualified', 'qualified_unverified')
      {stale_filter}
    ORDER BY co.id, s.siret NULLS LAST, s.id
),
keyed AS (
    SELECT o.*,
           (SELECT string_agg(w, ' ' ORDER BY w)
            FROM unnest(string_to_array(
                   btrim(regexp_replace(unaccent(lower(o.raw_name)),
                                        '[^a-z0-9]+', ' ', 'g')), ' ')) AS w
            WHERE w <> '') AS name_key
    FROM one_site o
),
valid AS (
    SELECT * FROM keyed
    WHERE name_key IS NOT NULL AND name_key <> '' AND pc IS NOT NULL
),
ranked AS (
    SELECT v.*,
           row_number() OVER w  AS rn,
           first_value(v.id) OVER w AS survivor_id,
           count(*) OVER (PARTITION BY v.name_key, v.pc) AS grp_size
    FROM valid v
    WINDOW w AS (
        PARTITION BY v.name_key, v.pc
        ORDER BY (v.siren IS NOT NULL) DESC,
                 (v.naf_code IS NOT NULL) DESC,
                 v.created_at,
                 v.id
    )
)
"""

STALE_FILTER = "AND (co.dedup_checked_at IS NULL)"


def build(body: str, force: bool) -> str:
    return KEYED_CTE.format(stale_filter="" if force else STALE_FILTER) + body


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url)


def summarize(cur, force: bool) -> dict:
    cur.execute(build("""
        SELECT count(*)                                              AS considered,
               count(*) FILTER (WHERE grp_size > 1)                  AS in_a_dup_group,
               count(*) FILTER (WHERE grp_size > 1 AND rn = 1)       AS survivors,
               count(*) FILTER (WHERE grp_size > 1 AND rn > 1)       AS to_mark_duplicate,
               count(DISTINCT (name_key, pc)) FILTER (WHERE grp_size > 1) AS dup_groups
        FROM ranked
    """, force))
    considered, in_grp, survivors, to_mark, groups = cur.fetchone()
    return {"considered": considered, "in_a_dup_group": in_grp,
            "survivors": survivors, "to_mark_duplicate": to_mark,
            "dup_groups": groups}


def samples(cur, force: bool, n: int = 5) -> list[tuple]:
    cur.execute(build("""
        SELECT r.pc, r.name_key,
               string_agg(
                   CASE WHEN r.rn = 1 THEN 'KEEP  ' ELSE 'DUP   ' END ||
                   coalesce(r.siren, '---------') || '  ' ||
                   left(coalesce(co.legal_name, co.trade_name), 34),
                   E'\\n              ' ORDER BY r.rn
               ) AS members
        FROM ranked r
        JOIN staging.companies co ON co.id = r.id
        WHERE r.grp_size > 1
        GROUP BY r.pc, r.name_key
        ORDER BY r.pc
        LIMIT %(n)s
    """, force), {"n": n})
    return cur.fetchall()


def apply_marks(cur, force: bool) -> tuple[int, int]:
    """Mark duplicates, then stamp the checkpoint on everything considered."""
    cur.execute(build("""
        UPDATE staging.companies c
        SET duplicate_of_company_id = r.survivor_id,
            dedup_method            = %(method)s,
            dedup_checked_at        = NOW()
        FROM ranked r
        WHERE c.id = r.id AND r.grp_size > 1 AND r.rn > 1
    """, force), {"method": DEDUP_METHOD})
    marked = cur.rowcount

    # Survivors and singletons are checked too, so they are not re-examined.
    cur.execute(build("""
        UPDATE staging.companies c
        SET dedup_checked_at = NOW()
        FROM ranked r
        WHERE c.id = r.id AND (r.grp_size = 1 OR r.rn = 1)
    """, force))
    return marked, cur.rowcount


def write_audit(cur, stats: dict, marked: int, force: bool) -> None:
    payload = {**stats, "rows_marked": marked, "method": DEDUP_METHOD,
               "mode": "force" if force else "unchecked-only"}
    cur.execute("""
        INSERT INTO audit.audit_log
            (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
        VALUES ('staging.companies', NULL, 'duplicate_of_company_id', NULL, %s, %s, %s)
    """, (json.dumps(payload, ensure_ascii=False), "m1_s5b_dedup.py",
          f"Duplicate marking pass, method={DEDUP_METHOD}"))


def main() -> None:
    ap = argparse.ArgumentParser(description="M1-S5b duplicate marking")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--force", action="store_true", help="re-evaluate already-checked rows")
    args = ap.parse_args()

    log.info("Method : %s", DEDUP_METHOD)
    log.info("Mode   : %s%s", "DRY-RUN (no writes)" if args.dry_run else "APPLY",
             " --force" if args.force else "")

    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            stats = summarize(cur, args.force)
            if stats["considered"] == 0:
                log.info("Nothing to do - every qualified company already dedup-checked.")
                log.info("Pass --force to re-evaluate.")
                return

            log.info("")
            log.info("Companies considered      : %s", f"{stats['considered']:,}")
            log.info("Duplicate groups found    : %s", f"{stats['dup_groups']:,}")
            log.info("Rows inside those groups  : %s", f"{stats['in_a_dup_group']:,}")
            log.info("  -> survivors (kept)     : %s", f"{stats['survivors']:,}")
            log.info("  -> to mark as duplicate : %s", f"{stats['to_mark_duplicate']:,}")

            log.info("")
            log.info("Sample groups:")
            for pc, _key, members in samples(cur, args.force):
                log.info("  [%s]  %s", pc, members)

            if args.dry_run:
                conn.rollback()
                log.info("")
                log.info("DRY-RUN - nothing written.")
                return

            marked, checked = apply_marks(cur, args.force)
            write_audit(cur, stats, marked, args.force)
            conn.commit()
            log.info("")
            log.info("Applied: %s marked as duplicates, %s checkpointed, 1 audit row.",
                     f"{marked:,}", f"{checked:,}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
