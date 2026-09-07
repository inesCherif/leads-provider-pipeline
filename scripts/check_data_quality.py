r"""
Data quality assertions — run after any stage that writes
==========================================================
Database constraints (CHECK, NOT NULL, UNIQUE, REFERENCES) already guard
STRUCTURE. They cannot guard DISTRIBUTION, and every bug in this project's
history was a distribution bug: a column that went 0% populated, a collapse
that stopped being unique, a duplicate rate that tripled.

Each assertion below maps to a real incident (docs/course/12-the-mistakes.md):

    fill rate > 0 on mapped columns   the Statut_Activite mapping typo
    business_id unique in the view    the non-deterministic DISTINCT ON
    SIREN = left(SIRET,9)             a wrong identifier importing another
                                      company's data
    duplicate rate in a sane band     the fake 12.5% premium-rate dedup
    email shape                       the 685 dot-less domains
    rule_version uniform              a qualification edit that silently
                                      selected nothing
    every file sector has a rule set  M4: a typo'd sector at ingestion would
                                      be qualified by NOBODY, silently

MULTI-SECTOR (M4, 2026-09-07). Three checks used to assert GLOBAL bands
(deliverable 80k-100k, duplicate rate 3-10%, one rule_version) that were
really agriculture's bands. They now run PER SECTOR against
config.sector_rules.SECTOR_BANDS, scoped through staging.company_sources.
A sector with no companies yet is skipped, not failed.

Exit code 0 = all pass, 1 = at least one FAIL. Safe to run any time: it issues
only SELECTs.

Usage:
    python scripts/check_data_quality.py                    # global + every sector
    python scripts/check_data_quality.py --sector tourisme  # global + one sector
    python scripts/check_data_quality.py --strict           # warnings also fail the run
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import psycopg2
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

from config.sector_rules import SECTORS, SECTOR_BANDS, get_sector, source_to_sector_key

# (name, sql returning ONE row: (value, detail), predicate, severity, why)
# predicate receives the value and returns True when healthy.
# SQL may use %(all_sources)s (every source sector that has a rule set),
# %(src_list)s / %(key_list)s (parallel arrays mapping source sector -> rule key).
CHECKS = [
    (
        "raw rows reconcile with source_files.row_count_imported",
        """SELECT count(*) FILTER (WHERE sf.row_count_imported IS DISTINCT FROM c.n),
                  string_agg(sf.file_name || ': declared=' || sf.row_count_imported
                             || ' actual=' || c.n, '; ')
           FROM staging.source_files sf
           JOIN LATERAL (SELECT count(*) n FROM raw.ingest_rows r
                         WHERE r.source_file_id = sf.id) c ON TRUE
           WHERE sf.row_count_imported IS DISTINCT FROM c.n""",
        lambda v: v == 0, "FAIL",
        "Rows were dropped between the raw landing and the counter. "
        "Reconciliation is the cheapest way to catch silent loss.",
    ),
    (
        "every contact/site/company carries a source_file_id",
        """SELECT (SELECT count(*) FROM staging.companies WHERE source_file_id IS NULL)
                + (SELECT count(*) FROM staging.sites     WHERE source_file_id IS NULL)
                + (SELECT count(*) FROM staging.contacts  WHERE source_file_id IS NULL),
                  NULL""",
        lambda v: v == 0, "FAIL",
        "Lineage is structural here; an orphan row means the NOT NULL was bypassed.",
    ),
    (
        "every company has at least one company_sources membership",
        """SELECT count(*), NULL FROM staging.companies co
           WHERE NOT EXISTS (SELECT 1 FROM staging.company_sources cs WHERE cs.company_id = co.id)""",
        lambda v: v == 0, "FAIL",
        "Migration 018: sector is membership. A company with no membership row "
        "belongs to no sector and is qualified by nobody.",
    ),
    (
        "every source_files.sector has a rule set (SECTORS[*].source_sectors)",
        """SELECT count(*), string_agg(sector, ', ') FROM (
             SELECT DISTINCT sector FROM staging.source_files WHERE sector IS NOT NULL) s
           WHERE NOT (sector = ANY(%(all_sources)s::text[]))""",
        lambda v: v == 0, "FAIL",
        "A sector value with no rule set is never selected by any qualification "
        "pass - its rows stay 'unqualified' forever, silently. Fix FILE_SPECS "
        "or add the SECTORS entry.",
    ),
    (
        "companies belonging to more than one sector (pct)",
        """SELECT round(100.0 * count(*) FILTER (WHERE n > 1) / NULLIF(count(*), 0), 2),
                  count(*) FILTER (WHERE n > 1) || ' companies in >1 sector'
           FROM (SELECT cs.company_id, count(DISTINCT m.key) n
                 FROM staging.company_sources cs
                 JOIN staging.source_files sf ON sf.id = cs.source_file_id
                 JOIN unnest(%(src_list)s::text[], %(key_list)s::text[]) m(src, key) ON m.src = sf.sector
                 GROUP BY cs.company_id) x""",
        lambda v: float(v) <= 5.0, "WARN",
        "Expected small (farm stays in agriculture AND tourism). A company in two "
        "sectors keeps ONE qualification verdict - the last pass wins - so if this "
        "grows past a few percent, verdicts need to become per-sector.",
    ),
    (
        "SIREN is the first 9 chars of SIRET wherever both exist",
        """SELECT count(*), string_agg(DISTINCT co.siren || '/' || s.siret, ', ')
           FROM staging.sites s JOIN staging.companies co ON co.id = s.company_id
           WHERE s.siret IS NOT NULL AND co.siren IS NOT NULL
             AND left(s.siret, 9) <> co.siren""",
        lambda v: v == 0, "FAIL",
        "A mismatch means a row carries another company's identifier — the "
        "failure mode the m1_s4 results[0] bug would have caused at scale.",
    ),
    (
        "SIRET is exactly 14 digits where present",
        """SELECT count(*), NULL FROM staging.sites
           WHERE siret IS NOT NULL AND btrim(siret) !~ '^[0-9]{14}$'""",
        lambda v: v == 0, "FAIL",
        "Validity: a malformed SIRET matches nothing and corrupts enrichment.",
    ),
    (
        "postal_code is 5 digits where present",
        """SELECT count(*), NULL FROM staging.sites
           WHERE postal_code IS NOT NULL AND btrim(postal_code) !~ '^[0-9]{5}$'""",
        lambda v: v == 0, "WARN",
        "4-digit codes are the Excel leading-zero bug; repaired at ingest since "
        "2026-08-02 but historic rows may remain.",
    ),
    (
        "business_id is unique in v_deliverable_businesses",
        """SELECT count(*) - count(DISTINCT business_id), NULL
           FROM public.v_deliverable_businesses""",
        lambda v: v == 0, "FAIL",
        "THE regression test for the non-deterministic DISTINCT ON. If the "
        "collapse stops being unique the deliverable double-counts businesses.",
    ),
    (
        "no email row is missing an @ or a dotted domain",
        """SELECT count(*), string_agg(email_address, ', ')
           FROM (SELECT email_address FROM staging.emails
                 WHERE verification_status <> 'invalid'
                   AND email_address !~ '^[^@[:space:]]+@[^@[:space:]]+\\.[A-Za-z]{2,}$'
                 LIMIT 20) x""",
        lambda v: v == 0, "WARN",
        "Malformed addresses are guaranteed hard bounces and damage the "
        "sending domain's reputation for the whole campaign.",
    ),
    (
        "at most one primary email per contact",
        """SELECT count(*), NULL FROM (
             SELECT contact_id FROM staging.emails WHERE is_primary
             GROUP BY contact_id HAVING count(*) > 1) x""",
        lambda v: v == 0, "FAIL",
        "The schema documents this invariant but cannot enforce it; the "
        "LATERAL best-email pick assumes it.",
    ),
    (
        "no email domain has a second domain welded onto it",
        r"""SELECT count(*), string_agg(email_address, ', ')
           FROM (SELECT email_address FROM staging.emails
                 WHERE verification_status <> 'invalid'
                   AND lower(split_part(email_address, '@', 2))
                       ~ '\.(fr|com|net|org)[a-z]{2,}\.'
                 LIMIT 20) x""",
        lambda v: v == 0, "FAIL",
        "S9-F, 2026-08-02: 91 addresses read orange.frnadoo.fr / "
        "gmail.commail.fr. They pass the malformed-address check above because "
        "they are syntactically valid, and several resolve to WILDCARDED "
        "TYPOSQUAT domains with catch-all MX - so the mail is delivered to a "
        "stranger rather than bounced. Test the domain, never the whole "
        "address: French names like '.francois.' match this pattern in the "
        "local part and over-count by 11%.",
    ),
    (
        "no business ships an email we have marked invalid",
        """SELECT count(*), string_agg(email_address, ', ')
           FROM (SELECT email_address FROM public.v_deliverable_businesses
                 WHERE email_status = 'invalid' LIMIT 20) x""",
        lambda v: v == 0, "FAIL",
        "Migration 015: the LATERAL best-email pick PREFERRED a valid address "
        "but did not EXCLUDE an invalid one, so a contact whose only address "
        "was invalid still shipped it. This is the regression test for that - "
        "and a prerequisite for Phase 5 verification, which will mark "
        "thousands invalid with no replacement to offer.",
    ),
    (
        "no shipped email is a contact_point we marked invalid or malformed",
        """SELECT count(*), string_agg(DISTINCT e.email_address, ', ')
           FROM staging.emails e
           JOIN staging.contacts c ON c.id = e.contact_id
           JOIN staging.contact_points cp
             ON cp.company_id = c.company_id AND cp.kind = 'email'
            AND cp.value_norm = lower(e.email_address)
            AND cp.verdict IN ('invalid', 'malformed')
           WHERE e.verification_status <> 'invalid'""",
        lambda v: v == 0, "FAIL",
        "M4: proven bounces and glued/spaced addresses live in contact_points so "
        "they are never re-proposed. If one is also in staging.emails as "
        "non-invalid, a loader bypassed the gate.",
    ),
    (
        "no non-dialable contact_point phone is a dialled phone_main",
        r"""SELECT count(*), string_agg(DISTINCT cp.value_norm, ', ')
           FROM staging.contact_points cp
           JOIN staging.contacts c ON c.company_id = cp.company_id
           WHERE cp.kind = 'phone' AND cp.is_dialable = false
             AND regexp_replace(COALESCE(c.phone_main, ''), '\D', '', 'g') = cp.value_norm
             AND NOT EXISTS (SELECT 1 FROM staging.contact_points d
                             WHERE d.company_id = cp.company_id AND d.kind = 'phone'
                               AND d.value_norm = cp.value_norm AND d.is_dialable)""",
        lambda v: v == 0, "FAIL",
        "M4: a search-snippet or one-witness 'piste' number is 76% right at best "
        "and must never reach the dialled column unless another source makes it "
        "dialable (gates H13/H14 of sector 2, now in the DB).",
    ),
    (
        "contact_points.value_norm is well-formed for its kind",
        r"""SELECT count(*), string_agg(kind || ':' || value_norm, ', ')
           FROM (SELECT kind, value_norm FROM staging.contact_points
                 WHERE (kind = 'phone'   AND value_norm !~ '^0[1-9][0-9]{8}$')
                    OR (kind = 'email'   AND value_norm !~ '^[^@[:space:]]+@[^@[:space:]]+\.[a-z]{2,}$')
                    OR (kind = 'website' AND value_norm !~ '^[a-z0-9.-]+\.[a-z]{2,}$')
                    OR (kind IN ('facebook','instagram','linkedin') AND value_norm !~ '^https?://')
                 LIMIT 20) x""",
        lambda v: v == 0, "FAIL",
        "M4: value_norm is the join key across sources; an unnormalised value "
        "silently fails to corroborate the same fact from another witness.",
    ),
    (
        "no company is 'pending' while holding a NAF code",
        """SELECT count(*), NULL FROM staging.companies
           WHERE qualification_status = 'pending' AND naf_code IS NOT NULL""",
        lambda v: v == 0, "WARN",
        "'pending' means undecidable. With a NAF code it IS decidable, so the "
        "row was missed by the last qualification pass.",
    ),
    (
        "every mapped source column is non-empty in staging",
        """SELECT count(*), string_agg(col, ', ') FROM (
             SELECT 'sites.source_status' AS col
               WHERE (SELECT count(*) FROM staging.sites WHERE source_status IS NOT NULL) = 0
             UNION ALL SELECT 'companies.naf_label'
               WHERE (SELECT count(*) FROM staging.companies WHERE naf_label IS NOT NULL) = 0
             UNION ALL SELECT 'contacts.phone_main'
               WHERE (SELECT count(*) FROM staging.contacts WHERE phone_main IS NOT NULL) = 0
             UNION ALL SELECT 'contacts.full_name'
               WHERE (SELECT count(*) FROM staging.contacts WHERE full_name IS NOT NULL) = 0
           ) x""",
        lambda v: v == 0, "FAIL",
        "A 0%-populated mapped column is the Statut_Activite bug's signature. "
        "It is a bug until proven otherwise.",
    ),
]

# Per-sector checks. SQL may use %(sources)s (the sector's source_sectors as a
# text[]). `band` names the SECTOR_BANDS entry used by the predicate factory.
SECTOR_CHECKS = [
    (
        "deliverable population within the sector's band",
        """SELECT count(*), NULL FROM public.v_deliverable_businesses
           WHERE sectors && %(sources)s::text[]""",
        "deliverable", "FAIL",
        "A large swing means a qualification or dedup change moved the base. "
        "Investigate before shipping, even if the change was intentional. "
        "Bands live in config.sector_rules.SECTOR_BANDS.",
    ),
    (
        "duplicate rate within the sector's band (pct)",
        """SELECT round(100.0 * count(*) FILTER (WHERE co.duplicate_of_company_id IS NOT NULL)
                        / NULLIF(count(*), 0), 2), NULL
           FROM (SELECT DISTINCT cs.company_id FROM staging.company_sources cs
                 JOIN staging.source_files sf ON sf.id = cs.source_file_id
                 WHERE sf.sector = ANY(%(sources)s::text[])) m
           JOIN staging.companies co ON co.id = m.company_id""",
        "dup_pct", "FAIL",
        "A spike is over-merging (the fake 12.5% premium-rate-phone result); a "
        "collapse to 0 means dedup was reverted. Both are silent.",
    ),
    (
        "qualification rule_version is uniform within the sector",
        """SELECT count(DISTINCT co.qualification_rule_version),
                  string_agg(DISTINCT co.qualification_rule_version, ', ')
           FROM (SELECT DISTINCT cs.company_id FROM staging.company_sources cs
                 JOIN staging.source_files sf ON sf.id = cs.source_file_id
                 WHERE sf.sector = ANY(%(sources)s::text[])) m
           JOIN staging.companies co ON co.id = m.company_id
           WHERE co.qualification_status <> 'unqualified'""",
        "uniform", "WARN",
        "Mixed versions mean a re-qualification did not finish, so verdicts "
        "from two different policies are live at once.",
    ),
]

SECTOR_COUNT_SQL = """
    SELECT count(DISTINCT cs.company_id) FROM staging.company_sources cs
    JOIN staging.source_files sf ON sf.id = cs.source_file_id
    WHERE sf.sector = ANY(%(sources)s::text[])"""


def band_predicate(kind: str, bands: dict):
    if kind == "uniform":
        return lambda v: v <= 1
    lo, hi = bands[kind]
    return lambda v: lo <= float(v) <= hi


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


def run_check(cur, name, sql, params, ok, severity, why, counters):
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        value, detail = (row[0], row[1]) if row else (None, None)
        value = 0 if value is None else value
        healthy = ok(value)
    except Exception as exc:              # a broken check must not pass silently
        print(f"{'ERROR':<7} {name:<58} {exc}")
        counters["fail"] += 1
        return

    if healthy:
        print(f"{'ok':<7} {name:<58} {value}")
        return

    tag = "FAIL" if severity == "FAIL" else "warn"
    print(f"{tag:<7} {name:<58} {value}")
    print(f"        why: {why}")
    if detail:
        print(f"        e.g.: {str(detail)[:220]}")
    counters["fail" if severity == "FAIL" else "warn"] += 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Data quality assertions")
    ap.add_argument("--strict", action="store_true",
                    help="treat WARN as failure (use in CI)")
    ap.add_argument("--sector", choices=sorted(SECTORS),
                    help="run the per-sector checks for this sector only "
                         "(default: every sector that has companies)")
    args = ap.parse_args()

    src_map = source_to_sector_key()
    global_params = {
        "all_sources": list(src_map),
        "src_list":    list(src_map),
        "key_list":    [src_map[s] for s in src_map],
    }

    conn = get_conn()
    cur = conn.cursor()
    counters = {"fail": 0, "warn": 0}
    n_checks = 0

    print(f"\n{'RESULT':<7} {'CHECK':<58} VALUE")
    print("-" * 100)

    for name, sql, ok, severity, why in CHECKS:
        run_check(cur, name, sql, global_params, ok, severity, why, counters)
        n_checks += 1

    sector_keys = [args.sector] if args.sector else sorted(SECTORS)
    for key in sector_keys:
        sector = get_sector(key)
        params = {"sources": list(sector["source_sectors"])}
        cur.execute(SECTOR_COUNT_SQL, params)
        n_companies = cur.fetchone()[0]
        if n_companies == 0:
            print(f"{'skip':<7} [{key}] no companies yet")
            continue
        print(f"{'':<7} [{key}] {n_companies} companies")
        bands = SECTOR_BANDS.get(key)
        if bands is None:
            print(f"{'FAIL':<7} [{key}] has no SECTOR_BANDS entry")
            counters["fail"] += 1
            n_checks += 1
            continue
        for name, sql, band_kind, severity, why in SECTOR_CHECKS:
            run_check(cur, f"[{key}] {name}", sql, params,
                      band_predicate(band_kind, bands), severity, why, counters)
            n_checks += 1

    print("-" * 100)
    print(f"{n_checks} checks · {counters['fail']} failed · {counters['warn']} warnings")

    conn.close()
    if counters["fail"] or (args.strict and counters["warn"]):
        print("\nDATA QUALITY: NOT OK")
        return 1
    print("\nDATA QUALITY: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
