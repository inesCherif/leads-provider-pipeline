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

Exit code 0 = all pass, 1 = at least one FAIL. Safe to run any time: it issues
only SELECTs.

Usage:
    python scripts/check_data_quality.py
    python scripts/check_data_quality.py --strict   # warnings also fail the run
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import psycopg2
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

# (name, sql returning ONE row: (value, detail), predicate, severity, why)
# predicate receives the value and returns True when healthy.
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
        "deliverable population is stable (80k-100k)",
        "SELECT count(*), NULL FROM public.v_deliverable_businesses",
        lambda v: 80_000 <= v <= 100_000, "FAIL",
        "A large swing means a qualification or dedup change moved the base. "
        "Investigate before shipping, even if the change was intentional.",
    ),
    (
        "duplicate rate is within a sane band (3-10%)",
        """SELECT round(100.0 * count(*) FILTER (WHERE duplicate_of_company_id IS NOT NULL)
                        / NULLIF(count(*), 0), 2), NULL
           FROM staging.companies""",
        lambda v: 3.0 <= float(v) <= 10.0, "FAIL",
        "A spike is over-merging (the fake 12.5% premium-rate-phone result); a "
        "collapse to 0 means dedup was reverted. Both are silent.",
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
        "qualification rule_version is uniform across qualified rows",
        """SELECT count(DISTINCT qualification_rule_version),
                  string_agg(DISTINCT qualification_rule_version, ', ')
           FROM staging.companies WHERE qualification_status <> 'unqualified'""",
        lambda v: v <= 1, "WARN",
        "Mixed versions mean a re-qualification did not finish, so verdicts "
        "from two different policies are live at once.",
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


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


def main() -> int:
    ap = argparse.ArgumentParser(description="Data quality assertions")
    ap.add_argument("--strict", action="store_true",
                    help="treat WARN as failure (use in CI)")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    failures = warnings = 0

    print(f"\n{'RESULT':<7} {'CHECK':<52} VALUE")
    print("-" * 100)

    for name, sql, ok, severity, why in CHECKS:
        try:
            cur.execute(sql)
            row = cur.fetchone()
            value, detail = (row[0], row[1]) if row else (None, None)
            value = 0 if value is None else value
            healthy = ok(value)
        except Exception as exc:              # a broken check must not pass silently
            print(f"{'ERROR':<7} {name:<52} {exc}")
            failures += 1
            continue

        if healthy:
            print(f"{'ok':<7} {name:<52} {value}")
            continue

        tag = "FAIL" if severity == "FAIL" else "warn"
        print(f"{tag:<7} {name:<52} {value}")
        print(f"        why: {why}")
        if detail:
            print(f"        e.g.: {str(detail)[:220]}")
        if severity == "FAIL":
            failures += 1
        else:
            warnings += 1

    print("-" * 100)
    print(f"{len(CHECKS)} checks · {failures} failed · {warnings} warnings")

    conn.close()
    if failures or (args.strict and warnings):
        print("\nDATA QUALITY: NOT OK")
        return 1
    print("\nDATA QUALITY: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
