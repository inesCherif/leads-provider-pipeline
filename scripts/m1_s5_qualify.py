"""
M1-S5 — Qualification
======================
Classifies every company in staging.companies against the sector rules in
config/sector_rules.py, writing:
    qualification_status       qualified | qualified_unverified | disqualified | pending
    disqualification_reason    controlled vocabulary (NULL when qualified)
    qualified_at               checkpoint
    qualification_rule_version which rule version produced the verdict

Two qualification tiers, per the 2026-07-19 decision:
  tier 1  'qualified'             — official INSEE NAF code is in scope
  tier 2  'qualified_unverified'  — no SIREN, so no official code; qualified on the
                                    data provider's source label alone. 83,344
                                    companies (65%) have no SIREN but 83,326 have a
                                    contact and 19,443 have an email, so shelving
                                    them as 'pending' would discard most of the
                                    reachable dataset. The separate tier keeps them
                                    usable while flagging them lower-confidence.

Design principles:
  - Idempotent: re-running with an unchanged RULE_VERSION is a no-op (zero rows match).
  - Set-based: one UPDATE over 128k rows. This is pure SQL classification — there is
    no reason to pull rows into Python and write them back.
  - Re-runnable on rule change: bumping RULE_VERSION in config/sector_rules.py makes
    every row stale and re-qualifies it. No manual reset needed.
  - Audited: each run writes one aggregate row to audit.audit_log.

NOTE — deliberate deviation from the repo's COALESCE-never-overwrite convention
(CLAUDE.md). That rule is correct for *enrichment* (external facts you don't want
to clobber) but wrong for *qualification*, which is derived: when the rule changes
the verdict must change. So these four columns are recomputed and overwritten.
Nothing else on the row is touched.

Usage:
    pip install psycopg2-binary python-dotenv

    # Show the projected distribution without writing anything (do this first):
    python scripts/m1_s5_qualify.py --dry-run

    # Apply:
    python scripts/m1_s5_qualify.py

    # Re-qualify every row regardless of stored version:
    python scripts/m1_s5_qualify.py --force
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install psycopg2-binary python-dotenv")

from config.sector_rules import (
    AGRI_EXCLUDE,
    AGRI_INCLUDE,
    NAF_HARD_EXCLUDE,
    NAF_PREFIXES_IN_SCOPE,
    NAF_RESCUE_CODES,
    NAF_RESCUE_PREFIXES,
    RULE_VERSION,
    SECTOR,
    get_sector,
)

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("m1_s5")

# ─── Classification ───────────────────────────────────────────────────────────

# The verdict is encoded as 'status|reason' in a single CASE so that status and
# reason can never diverge. An empty reason means NULL (qualified rows).
#
# Branch order is the rule. Each branch assumes every branch above it missed:
#   1. closed businesses            — currently 0 rows (the SIRENE endpoint in use
#                                     only returns active entities, so closed ones
#                                     surface as 'not found' instead). Kept so the
#                                     rule is correct once that is fixed.
#   2. hard-excluded NAF            — communes, regardless of source label
#   3. NAF prefix in scope          — tier 1, covers NAF rév.1 and rév.2 alike
#   4. rescue code + agri label     — GFAs and on-farm electricity producers
#   4b. rescue prefix + agri label  — on-farm transformation (food, drink,
#                                     electricity), added agri-v2
#   5. any other official NAF       — out of scope
#   6. has SIREN but no NAF         — SIRENE lookup failed, undecidable
#   -- everything below has no SIREN --
#   7. no label                     — nothing to judge on
#   8. excluded label               — pet breeders, aviaries, florists
#   9. agri label                   — tier 2
#  10. anything else                — unrecognised label, undecidable

# The sector filter is NOT optional and NOT a convenience.
#
# The stale-row selector is `qualification_rule_version IS DISTINCT FROM <v>`.
# Without a sector scope, running sector B's pass would match every sector A row
# (their version differs by definition) and silently re-classify 128k
# agricultural companies against bakery rules. The join to source_files is what
# makes a pass incapable of touching another sector's data.
#
# --force widens the row selector to "every row"; it must still be confined to
# the sector, which is why the sector predicate sits in the base FROM and not in
# {where_clause}.
CLASSIFY_CTE = """
WITH target AS (
    -- M4: sectors whose companies arrive with the provider's / registry-API's
    -- NAF but no SIRENE enrichment yet may fall back to naf_code_source
    -- (sector_rules "naf_fallback_source"). OFF for agriculture on purpose:
    -- 570 farms merged with the tourism file now carry a 55.xx provider NAF,
    -- and a farm stay must stay qualified as a farm.
    SELECT c.id, c.siren,
           CASE WHEN %(naf_fallback)s THEN coalesce(c.naf_code, c.naf_code_source)
                ELSE c.naf_code END AS naf_code,
           c.naf_label, c.sirene_etat
    FROM staging.companies c
    JOIN staging.source_files sf ON sf.id = c.source_file_id
    WHERE sf.sector = ANY(%(source_sectors)s)
    {where_clause}
),
flagged AS (
    SELECT
        id, siren, naf_code, naf_label, sirene_etat,
        btrim(naf_code) AS naf,
        unaccent(lower(coalesce(naf_label, ''))) ~ %(include)s AS is_agri_label,
        unaccent(lower(coalesce(naf_label, ''))) ~ %(exclude)s AS is_excluded_label
    FROM target
),
classified AS (
    SELECT id, naf, naf_label,
        CASE
            WHEN sirene_etat = 'F'                        THEN 'disqualified|business_closed'
            WHEN naf = ANY(%(hard_exclude)s)              THEN 'disqualified|public_administration'
            WHEN naf IS NOT NULL
                 AND naf LIKE ANY(%(prefixes)s)           THEN 'qualified|'
            WHEN naf = ANY(%(rescue)s)
                 AND is_agri_label                        THEN 'qualified|'
            WHEN naf LIKE ANY(%(rescue_prefixes)s)
                 AND is_agri_label                        THEN 'qualified|'
            WHEN naf IS NOT NULL                          THEN 'disqualified|naf_out_of_scope'
            WHEN siren IS NOT NULL                        THEN 'pending|sirene_not_found'
            WHEN naf_label IS NULL                        THEN 'pending|no_siren_no_label'
            WHEN is_excluded_label                        THEN 'disqualified|label_out_of_scope'
            WHEN is_agri_label                            THEN 'qualified_unverified|'
            ELSE                                               'pending|no_siren_no_label'
        END AS verdict
    FROM flagged
)
"""

# AND, not WHERE: the sector predicate above already opened the WHERE clause.
# `IS DISTINCT FROM` rather than `<>` because NULL <> 'agri-v2' evaluates to
# NULL, not TRUE, so a never-qualified row would not be selected.
WHERE_STALE = """
      AND (c.qualified_at IS NULL
           OR c.qualification_rule_version IS DISTINCT FROM %(rule_version)s)
"""


def rule_params(sector: dict) -> dict:
    return {
        "include":         sector["include"],
        "exclude":         sector["exclude"],
        "hard_exclude":    list(sector["naf_hard_exclude"]),
        "rescue":          list(sector["naf_rescue_codes"]),
        "rescue_prefixes": [p + "%" for p in sector["naf_rescue_prefixes"]],
        "prefixes":        [p + "%" for p in sector["naf_prefixes_in_scope"]],
        "rule_version":    sector["rule_version"],
        "source_sectors":  list(sector["source_sectors"]),
        "naf_fallback":    bool(sector.get("naf_fallback_source", False)),
    }


def build_sql(body: str, force: bool) -> str:
    return CLASSIFY_CTE.format(where_clause="" if force else WHERE_STALE) + body


# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url)


def count_stale(cur, force: bool, sector: dict) -> int:
    # Sector-scoped in both branches: --force must mean "every row OF THIS
    # SECTOR", never every row in the table.
    base = """
        SELECT count(*) FROM staging.companies c
        JOIN staging.source_files sf ON sf.id = c.source_file_id
        WHERE sf.sector = ANY(%(source_sectors)s)
    """
    cur.execute(base if force else base + WHERE_STALE, rule_params(sector))
    return cur.fetchone()[0]


def projected_distribution(cur, force: bool, sector: dict) -> list[tuple]:
    """Classify without writing. A single GROUP BY — cannot loop, cannot hang."""
    cur.execute(
        build_sql(
            """
            SELECT split_part(verdict, '|', 1)              AS status,
                   nullif(split_part(verdict, '|', 2), '')  AS reason,
                   count(*)                                 AS n
            FROM classified
            GROUP BY 1, 2
            ORDER BY 3 DESC
            """,
            force,
        ),
        rule_params(sector),
    )
    return cur.fetchall()


def verdict_samples(cur, force: bool, sector: dict, per_bucket: int = 3) -> list[tuple]:
    """A few example companies per verdict, so the reasons can be eyeballed."""
    cur.execute(
        build_sql(
            """
            SELECT verdict, naf, naf_label, legal_name
            FROM (
                SELECT c.verdict, c.naf, c.naf_label, co.legal_name,
                       row_number() OVER (PARTITION BY c.verdict ORDER BY c.id) AS rn
                FROM classified c
                JOIN staging.companies co ON co.id = c.id
            ) s
            WHERE rn <= %(per_bucket)s
            ORDER BY verdict, rn
            """,
            force,
        ),
        {**rule_params(sector), "per_bucket": per_bucket},
    )
    return cur.fetchall()


def apply_qualification(cur, force: bool, sector: dict) -> int:
    cur.execute(
        build_sql(
            """
            UPDATE staging.companies c
            SET qualification_status       = split_part(cl.verdict, '|', 1),
                disqualification_reason    = nullif(split_part(cl.verdict, '|', 2), ''),
                qualified_at               = NOW(),
                qualification_rule_version = %(rule_version)s
            FROM classified cl
            WHERE c.id = cl.id
            """,
            force,
        ),
        rule_params(sector),
    )
    return cur.rowcount


def write_audit(cur, counts: list[tuple], rows_updated: int, force: bool, sector: dict) -> None:
    """One aggregate row per run. Row-level entries for 128k rows would be noise."""
    summary = {
        "sector": sector["key"],
        "rule_version": sector["rule_version"],
        "rows_updated": rows_updated,
        "mode": "force" if force else "stale-only",
        "distribution": {
            f"{status}:{reason or '-'}": n for status, reason, n in counts
        },
    }
    cur.execute(
        """
        INSERT INTO audit.audit_log
            (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
        VALUES
            ('staging.companies', NULL, 'qualification_status', NULL, %s, %s, %s)
        """,
        (
            json.dumps(summary, ensure_ascii=False),
            "m1_s5_qualify.py",
            f"Qualification pass, sector={sector['key']}, "
            f"rule_version={sector['rule_version']}",
        ),
    )


# ─── Reporting ────────────────────────────────────────────────────────────────

def report(counts: list[tuple], total: int) -> None:
    log.info("%-22s %-24s %10s  %s", "STATUS", "REASON", "ROWS", "SHARE")
    log.info("%s", "-" * 70)
    for status, reason, n in counts:
        pct = (n / total * 100) if total else 0
        log.info("%-22s %-24s %10s  %5.1f%%", status, reason or "-", f"{n:,}", pct)
    log.info("%s", "-" * 70)
    log.info("%-22s %-24s %10s", "TOTAL", "", f"{sum(c[2] for c in counts):,}")


def report_samples(samples: list[tuple]) -> None:
    log.info("")
    log.info("Sample rows per verdict (spot-check the reasons):")
    current = None
    for verdict, naf, naf_label, legal_name in samples:
        if verdict != current:
            current = verdict
            log.info("")
            log.info("  %s", verdict)
        log.info(
            "    naf=%-8s label=%-38s name=%s",
            naf or "-",
            (naf_label or "-")[:38],
            (legal_name or "-")[:40],
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="M1-S5 qualification pass")
    ap.add_argument("--sector", default=None,
                    help="which rule set to apply (default: the pilot sector). "
                         "A pass can only ever touch its own sector's rows.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the projected distribution, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-qualify every row, ignoring the stored rule version")
    args = ap.parse_args()

    sector = get_sector(args.sector)
    log.info("Sector       : %s  (source_files.sector in %s)",
             sector["key"], ", ".join(sector["source_sectors"]))
    log.info("Rule version : %s", sector["rule_version"])
    log.info("Mode         : %s%s",
             "DRY-RUN (no writes)" if args.dry_run else "APPLY",
             " --force" if args.force else "")

    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            stale = count_stale(cur, args.force, sector)
            log.info("Rows to classify: %s", f"{stale:,}")
            if stale == 0:
                log.info("Nothing to do - every row already carries rule_version=%s.",
                         sector["rule_version"])
                log.info("Bump RULE_VERSION in config/sector_rules.py, or pass --force.")
                return

            counts = projected_distribution(cur, args.force, sector)
            report(counts, stale)

            if args.dry_run:
                report_samples(verdict_samples(cur, args.force, sector))
                log.info("")
                log.info("DRY-RUN - nothing written. Re-run without --dry-run to apply.")
                conn.rollback()
                return

            updated = apply_qualification(cur, args.force, sector)
            write_audit(cur, counts, updated, args.force, sector)
            conn.commit()
            log.info("")
            log.info("Applied: %s rows updated, 1 audit.audit_log row written.",
                     f"{updated:,}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
