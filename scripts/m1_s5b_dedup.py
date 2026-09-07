"""
M1-S5b — Duplicate marking
===========================
Finds companies that are the same real business appearing more than once, and
links each duplicate to a chosen survivor.

NON-DESTRUCTIVE. No row is deleted and no data is merged or overwritten. A
duplicate keeps everything it has and gains a pointer:
    duplicate_of_company_id -> the survivor
Deduplication then happens in the views via coalesce(duplicate_of_company_id, id),
exposed as business_id. Undo the whole thing with:
    UPDATE staging.companies
       SET duplicate_of_company_id = NULL, dedup_method = NULL, dedup_checked_at = NULL;

WHY THIS EXISTS
M1-S3 deduplicated on SIRET only. A farm present in both source files - once with
a SIRET, once without - became two rows that never merged.
See docs/project_documentation.md sections 5.2 and 5.3.

TWO PASSES, most confident first. Each row records which method matched it in
dedup_method, so a later pass can be re-evaluated without redoing the earlier one.

  1. exact_name_postal   normalized name + postal code, exact.
  2. stripped_legal_form same, but with French legal forms (EARL, GAEC, SARL,
                         SCEA, GFA...) and the provider's trailing activity tag
                         stripped first. Catches "EARL DU VAL VERT" == "DU VAL VERT".

NORMALIZATION: unaccent, lowercase, punctuation to spaces, then SORT the word
tokens. Sorting is what collapses "Lemoine Jean-Claude" and "JEAN-CLAUDE LEMOINE"
- this provider routinely reverses first/last name order between files.

THE SIREN GUARD - important
A row is only ever marked as a duplicate if its SIREN is NULL, or equals the
survivor's SIREN. Two rows with DIFFERENT non-null SIRENs are two separately
registered legal entities and must not be collapsed, however identical their
names look. This matters in French agriculture, where one family commonly
operates through several entities at one address:
    444925549 ANTOINE CARDON   /  830687851 EARL ANTOINE CARDON
    342450327 EARL DE LA CROIX BLANCHE  /  789799129 SCI DE LA CROIX BLANCHE
The first version of this script lacked the guard and wrongly merged 56 such
rows; they were unmarked and the guard added.

PHONE IS DELIBERATELY NOT USED as an identity signal. An earlier attempt keyed on
phone and produced a 12.5% duplicate rate that was wrong: +33890xxxxxx and
+33899xxxxxx are French premium-rate service lines shared across many departments.

SURVIVOR SELECTION, in order:
    1. has a SIREN          - verifiable against INSEE, carries enrichment
    2. has a naf_code       - more complete
    3. earliest created_at  - first seen wins, consistent with the ingest merge
    4. lowest id            - deterministic tiebreak, so re-runs are stable

IDEMPOTENT BY CONSTRUCTION: every pass only considers rows that are not already
marked, so a group whose duplicates were marked last run no longer has two
unmarked members and produces nothing. Re-running is a safe no-op.

SCOPE: qualified + qualified_unverified only. Duplicates among pending and
disqualified companies are not marked - they are not delivered to anyone.

Usage:
    python scripts/m1_s5b_dedup.py --dry-run
    python scripts/m1_s5b_dedup.py
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

# French legal forms and the provider's trailing activity tag, stripped in pass 2.
LEGAL_FORM_PATTERN = (
    r"\y(earl|gaec|sarl|eurl|sasu|sas|scea|sci|sca|snc|gfa|cuma|sc|sa|ea"
    r"|eleveur|eleveurs|elevage)\y"
)

# Pass 1 key: normalize only. Pass 2 key: normalize with legal forms removed.
KEY_RAW = "regexp_replace(unaccent(lower(o.raw_name)), '[^a-z0-9]+', ' ', 'g')"
KEY_STRIPPED = (
    "regexp_replace(regexp_replace(unaccent(lower(o.raw_name)), '[^a-z0-9]+', ' ', 'g'),"
    f" '{LEGAL_FORM_PATTERN}', ' ', 'g')"
)

PASSES = [
    ("exact_name_postal",   KEY_RAW),
    ("stripped_legal_form", KEY_STRIPPED),
]

# ─── Pass 3: fuzzy (pg_trgm), added 2026-07-20 ────────────────────────────────
#
# Closes the long-documented "pg_trgm fuzzy dedup does not exist" gap. Measured
# before building: only 213 candidate pairs exist across BOTH tiers, not the
# ~83k the docs implied. The exact passes had already done the heavy lifting.
#
# Catches near-misses the exact key cannot: a leading article or a typo.
#   volailles de fontenai      <- les volailles de fontenai
#   la ferme du xviieme siecle <- la ferme du xviie siecle
#   les cochons du berger      <- les cochon du berger
#
# TWO GUARDS, both required:
#   1. SAME PHONE. Name similarity alone is not evidence of duplication. Checked
#      empirically: 'gamm vert' appears 15 times with 15 different phones — it is
#      a national garden-centre chain, and merging those would delete 14 real
#      prospects. Of the 213 candidate pairs only 160 share a phone; the other 53
#      are left alone precisely because they may be distinct businesses.
#   2. The SIREN guard, same as passes 1-2: different non-null SIRENs are
#      different legal entities and are never merged.
#
# Threshold 0.85 is deliberately strict. Everything below it in the sample was
# either a chain outlet or a genuinely different farm.
FUZZY_SIMILARITY_MIN = 0.85

FUZZY_CTE = """
WITH one_site AS (
    SELECT DISTINCT ON (co.id)
           co.id, co.siren, co.naf_code, co.created_at,
           sf.sector,                                   -- M4: primary sector
           btrim(s.postal_code) AS pc,
           regexp_replace(unaccent(lower(coalesce(co.legal_name, co.trade_name))),
                          '[^a-z0-9]+', ' ', 'g') AS nm,
           (SELECT ct.phone_main FROM staging.contacts ct
             WHERE ct.site_id = s.id AND ct.phone_main IS NOT NULL
             LIMIT 1) AS phone
    FROM staging.companies co
    JOIN staging.sites s ON s.company_id = co.id
    JOIN staging.source_files sf ON sf.id = co.source_file_id
    WHERE co.qualification_status IN ('qualified', 'qualified_unverified')
      AND co.duplicate_of_company_id IS NULL
      AND coalesce(co.legal_name, co.trade_name) IS NOT NULL
      AND btrim(coalesce(s.postal_code, '')) <> ''
    ORDER BY co.id, s.siret NULLS LAST, s.id
),
pairs AS (
    SELECT a.id AS a_id, b.id AS b_id, a.siren AS a_siren, b.siren AS b_siren, a.pc,
           -- survivor precedence, identical to passes 1-2: prefer a row that has
           -- a SIREN, then one that has a NAF, then the oldest, then lowest id.
           (ROW(a.siren IS NULL, a.naf_code IS NULL, a.created_at, a.id)
          < ROW(b.siren IS NULL, b.naf_code IS NULL, b.created_at, b.id)) AS a_wins
    FROM one_site a
    JOIN one_site b
      ON a.pc = b.pc
     AND a.sector = b.sector                    -- M4 GUARD 0: never across sectors
     AND a.id < b.id
     AND a.nm <> b.nm
     AND a.phone IS NOT NULL
     AND a.phone = b.phone                      -- GUARD 1
     AND similarity(a.nm, b.nm) > {sim}
),
resolved AS (
    SELECT CASE WHEN a_wins THEN a_id    ELSE b_id    END AS survivor_id,
           CASE WHEN a_wins THEN b_id    ELSE a_id    END AS dup_id,
           CASE WHEN a_wins THEN a_siren ELSE b_siren END AS survivor_siren,
           CASE WHEN a_wins THEN b_siren ELSE a_siren END AS dup_siren,
           pc
    FROM pairs
),
marked AS (
    SELECT DISTINCT ON (dup_id) *
    FROM resolved
    WHERE (dup_siren IS NULL OR dup_siren IS NOT DISTINCT FROM survivor_siren)  -- GUARD 2
      -- never mark a row that is itself acting as a survivor: prevents a chain
      -- A<-B<-C from pointing at an id that is already redirected elsewhere.
      AND dup_id NOT IN (SELECT survivor_id FROM resolved)
    ORDER BY dup_id, survivor_id
)
"""

# Only ever considers rows not already marked, which is what makes this idempotent.
CTE = """
WITH one_site AS (
    SELECT DISTINCT ON (co.id)
           co.id, co.siren, co.naf_code, co.created_at,
           sf.sector,                                   -- M4: primary sector
           btrim(s.postal_code) AS pc,
           coalesce(co.legal_name, co.trade_name) AS raw_name
    FROM staging.companies co
    JOIN staging.sites s ON s.company_id = co.id
    JOIN staging.source_files sf ON sf.id = co.source_file_id
    WHERE co.qualification_status IN ('qualified', 'qualified_unverified')
      AND co.duplicate_of_company_id IS NULL
    ORDER BY co.id, s.siret NULLS LAST, s.id
),
keyed AS (
    SELECT o.*,
           (SELECT string_agg(w, ' ' ORDER BY w)
            FROM unnest(string_to_array(btrim({key_expr}), ' ')) AS w
            WHERE w <> '') AS name_key
    FROM one_site o
),
valid AS (
    SELECT * FROM keyed WHERE name_key IS NOT NULL AND name_key <> '' AND pc IS NOT NULL
),
ranked AS (
    SELECT v.*,
           row_number() OVER w      AS rn,
           first_value(v.id)    OVER w AS survivor_id,
           first_value(v.siren) OVER w AS survivor_siren,
           count(*) OVER (PARTITION BY v.name_key, v.pc, v.sector) AS grp_size
    FROM valid v
    WINDOW w AS (
        -- M4 GUARD 0: a group never spans two sectors. Two no-SIREN businesses
        -- with the same name at one postcode in different sectors (a 'SARL
        -- DUPONT' printing shop and a 'SARL DUPONT' gîte) are not one business.
        PARTITION BY v.name_key, v.pc, v.sector
        ORDER BY (v.siren IS NOT NULL) DESC, (v.naf_code IS NOT NULL) DESC,
                 v.created_at, v.id
    )
),
-- THE GUARD: a member is only a duplicate if it has no SIREN of its own, or the
-- same SIREN as the survivor. Different non-null SIRENs = different legal entities.
marked AS (
    SELECT * FROM ranked
    WHERE grp_size > 1 AND rn > 1
      AND (siren IS NULL OR siren IS NOT DISTINCT FROM survivor_siren)
)
"""


def build(key_expr: str, body: str) -> str:
    return CTE.format(key_expr=key_expr) + body


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url)


def preview(cur, key_expr: str) -> dict:
    cur.execute(build(key_expr, """
        SELECT (SELECT count(*) FROM ranked)                       AS considered,
               (SELECT count(*) FROM marked)                       AS would_mark,
               (SELECT count(DISTINCT (name_key, pc)) FROM marked)  AS groups,
               (SELECT count(*) FROM ranked
                 WHERE grp_size > 1 AND rn > 1
                   AND siren IS NOT NULL
                   AND siren IS DISTINCT FROM survivor_siren)      AS blocked_by_siren_guard
    """))
    considered, would_mark, groups, blocked = cur.fetchone()
    return {"considered": considered, "would_mark": would_mark,
            "groups": groups, "blocked_by_siren_guard": blocked}


def samples(cur, key_expr: str, n: int = 4) -> list[tuple]:
    cur.execute(build(key_expr, """
        SELECT m.pc,
               coalesce(sv.siren,'---------') || ' ' || left(coalesce(sv.legal_name,sv.trade_name),30)
                 || '   <-   ' ||
               coalesce(dp.siren,'---------') || ' ' || left(coalesce(dp.legal_name,dp.trade_name),30) AS pair
        FROM marked m
        JOIN staging.companies dp ON dp.id = m.id
        JOIN staging.companies sv ON sv.id = m.survivor_id
        ORDER BY m.pc LIMIT %(n)s
    """), {"n": n})
    return cur.fetchall()


def apply_pass(cur, key_expr: str, method: str) -> int:
    cur.execute(build(key_expr, """
        UPDATE staging.companies c
        SET duplicate_of_company_id = m.survivor_id,
            dedup_method            = %(method)s,
            dedup_checked_at        = NOW()
        FROM marked m
        WHERE c.id = m.id
    """), {"method": method})
    return cur.rowcount


def build_fuzzy(body: str) -> str:
    return FUZZY_CTE.format(sim=FUZZY_SIMILARITY_MIN) + body


def preview_fuzzy(cur) -> dict:
    cur.execute(build_fuzzy("""
        SELECT (SELECT count(*) FROM one_site)  AS considered,
               (SELECT count(*) FROM pairs)     AS candidate_pairs,
               (SELECT count(*) FROM marked)    AS would_mark,
               (SELECT count(*) FROM resolved
                 WHERE dup_siren IS NOT NULL
                   AND dup_siren IS DISTINCT FROM survivor_siren) AS blocked_by_siren_guard
    """))
    considered, cand, would_mark, blocked = cur.fetchone()
    return {"considered": considered, "candidate_pairs": cand,
            "would_mark": would_mark, "blocked_by_siren_guard": blocked}


def samples_fuzzy(cur, n: int = 4) -> list[tuple]:
    cur.execute(build_fuzzy("""
        SELECT m.pc,
               coalesce(sv.siren,'---------') || ' ' || left(coalesce(sv.legal_name,sv.trade_name),30)
                 || '   <-   ' ||
               coalesce(dp.siren,'---------') || ' ' || left(coalesce(dp.legal_name,dp.trade_name),30) AS pair
        FROM marked m
        JOIN staging.companies dp ON dp.id = m.dup_id
        JOIN staging.companies sv ON sv.id = m.survivor_id
        ORDER BY m.pc LIMIT %(n)s
    """), {"n": n})
    return cur.fetchall()


def apply_fuzzy(cur, method: str) -> int:
    cur.execute(build_fuzzy("""
        UPDATE staging.companies c
        SET duplicate_of_company_id = m.survivor_id,
            dedup_method            = %(method)s,
            dedup_checked_at        = NOW()
        FROM marked m
        WHERE c.id = m.dup_id
          AND c.duplicate_of_company_id IS NULL
    """), {"method": method})
    return cur.rowcount


def write_audit(cur, results: list[dict]) -> None:
    cur.execute("""
        INSERT INTO audit.audit_log
            (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
        VALUES ('staging.companies', NULL, 'duplicate_of_company_id', NULL, %s, %s, %s)
    """, (json.dumps({"passes": results}, ensure_ascii=False), "m1_s5b_dedup.py",
          "Duplicate marking pass"))


def main() -> None:
    ap = argparse.ArgumentParser(description="M1-S5b duplicate marking")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    log.info("Mode: %s", "DRY-RUN (no writes)" if args.dry_run else "APPLY")

    conn = get_conn()
    conn.autocommit = False
    try:
        results = []
        with conn.cursor() as cur:
            for method, key_expr in PASSES:
                log.info("")
                log.info("=== pass: %s ===", method)
                p = preview(cur, key_expr)
                log.info("  unmarked rows considered : %s", f"{p['considered']:,}")
                log.info("  duplicate groups         : %s", f"{p['groups']:,}")
                log.info("  rows to mark             : %s", f"{p['would_mark']:,}")
                log.info("  blocked by SIREN guard   : %s  (different legal entities)",
                         f"{p['blocked_by_siren_guard']:,}")

                for pc, pair in samples(cur, key_expr):
                    log.info("    [%s] %s", pc, pair)

                if args.dry_run:
                    results.append({"method": method, **p})
                    continue

                marked = apply_pass(cur, key_expr, method)
                log.info("  -> marked %s", f"{marked:,}")
                results.append({"method": method, "marked": marked, **p})

            # Pass 3 runs last so the exact passes have already collapsed the
            # easy groups; fuzzy only ever sees what they could not match.
            log.info("")
            log.info("=== pass: fuzzy_name_postal_phone ===")
            pf = preview_fuzzy(cur)
            log.info("  unmarked rows considered : %s", f"{pf['considered']:,}")
            log.info("  candidate pairs (sim>%.2f, same phone): %s",
                     FUZZY_SIMILARITY_MIN, f"{pf['candidate_pairs']:,}")
            log.info("  rows to mark             : %s", f"{pf['would_mark']:,}")
            log.info("  blocked by SIREN guard   : %s  (different legal entities)",
                     f"{pf['blocked_by_siren_guard']:,}")

            for pc, pair in samples_fuzzy(cur):
                log.info("    [%s] %s", pc, pair)

            if not args.dry_run:
                marked = apply_fuzzy(cur, "fuzzy_name_postal_phone")
                log.info("  -> marked %s", f"{marked:,}")
                results.append({"method": "fuzzy_name_postal_phone",
                                "marked": marked, **pf})
            else:
                results.append({"method": "fuzzy_name_postal_phone", **pf})

            if args.dry_run:
                conn.rollback()
                log.info("")
                log.info("DRY-RUN - nothing written.")
                return

            write_audit(cur, results)
            conn.commit()
            log.info("")
            log.info("Applied: %s rows marked across %d passes, 1 audit row.",
                     f"{sum(r['marked'] for r in results):,}", len(results))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
