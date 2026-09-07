r"""
M4 — unload ONE source file's STAGING contributions so the loader can re-run
=============================================================================
    python scripts/m4_unload_file.py --file "<name>" --dry-run
    python scripts/m4_unload_file.py --file "<name>" --confirm

The raw landing (raw.ingest_rows) is NEVER touched: it is the immutable copy
the reload reads nothing from but that proves what the file contained. What
is removed, in dependency order, for the file's source_file_id:

    contact_points, company_attributes, company_identifiers   (source_file_id = f)
    emails (via contacts), contacts                            (source_file_id = f)
    sites                                                      (source_file_id = f)
    company_sources                                            (source_file_id = f)
    companies CREATED by this file                             (source_file_id = f)
    fields COALESCE-filled onto pre-existing companies/sites by this file's
        rows are reset to NULL where they equal this file's values
        (naf_code_source, creation_date, legal_name/trade_name/naf_label on
        companies; address_line2, department, source_status on sites) —
        the SIRENE enrichment refills creation_date on its next pass
    source_files.row_count_imported -> NULL  (so m1_s3 RESUMES, not skips)

This is the ONLY script in the repo that issues DELETE. It refuses to run
without --confirm, prints every count before and after, and is scoped by
source_file_id everywhere. Why it exists: the first bakery load attached 18
unrelated bakeries to one SIREN through a broken registry lookup, and the
honest fix is to re-run a corrected loader, not to patch rows by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from config.ingest_specs import spec_for          # noqa: E402
from ingest_lib import get_conn, sha256_file       # noqa: E402

DATA_DIR = PROJECT_ROOT / "Data Globale 05 juillet 2026"

COUNTS = {
    "contact_points":      "SELECT count(*) FROM staging.contact_points      WHERE source_file_id = %(f)s",
    "company_attributes":  "SELECT count(*) FROM staging.company_attributes  WHERE source_file_id = %(f)s",
    "company_identifiers": "SELECT count(*) FROM staging.company_identifiers WHERE source_file_id = %(f)s",
    "emails":              "SELECT count(*) FROM staging.emails e JOIN staging.contacts c ON c.id = e.contact_id WHERE c.source_file_id = %(f)s",
    "contacts":            "SELECT count(*) FROM staging.contacts            WHERE source_file_id = %(f)s",
    "sites":               "SELECT count(*) FROM staging.sites               WHERE source_file_id = %(f)s",
    "company_sources":     "SELECT count(*) FROM staging.company_sources     WHERE source_file_id = %(f)s",
    "companies_created":   "SELECT count(*) FROM staging.companies           WHERE source_file_id = %(f)s",
    "companies_merged":    """SELECT count(DISTINCT cs.company_id) FROM staging.company_sources cs
                              JOIN staging.companies co ON co.id = cs.company_id
                              WHERE cs.source_file_id = %(f)s AND co.source_file_id <> %(f)s""",
    "raw_rows_kept":       "SELECT count(*) FROM raw.ingest_rows             WHERE source_file_id = %(f)s",
}

# Reset COALESCE-filled fields on companies that existed BEFORE this file,
# where the value equals what one of this file's rows would have written.
RESET_MERGED_COMPANIES = """
WITH mine AS (
    SELECT cs.company_id, r.raw_json
    FROM staging.company_sources cs
    JOIN raw.ingest_rows r ON r.source_file_id = cs.source_file_id AND r.row_index = cs.row_index
    WHERE cs.source_file_id = %(f)s)
UPDATE staging.companies co
   SET naf_code_source = CASE WHEN co.naf_code_source IS NOT NULL
                               AND replace(co.naf_code_source, '.', '') = ANY(%(naf_values)s::text[]) THEN NULL ELSE co.naf_code_source END,
       creation_date   = CASE WHEN co.creation_date::text = ANY(%(date_values)s::text[]) THEN NULL ELSE co.creation_date END
 WHERE co.source_file_id <> %(f)s
   AND co.id IN (SELECT company_id FROM mine)
"""

DELETES = [
    ("contact_points",      "DELETE FROM staging.contact_points      WHERE source_file_id = %(f)s"),
    ("company_attributes",  "DELETE FROM staging.company_attributes  WHERE source_file_id = %(f)s"),
    ("company_identifiers", "DELETE FROM staging.company_identifiers WHERE source_file_id = %(f)s"),
    ("contacts (+emails)",  "DELETE FROM staging.contacts            WHERE source_file_id = %(f)s"),
    ("sites",               "DELETE FROM staging.sites               WHERE source_file_id = %(f)s"),
    ("company_sources",     "DELETE FROM staging.company_sources     WHERE source_file_id = %(f)s"),
    ("companies_created",   "DELETE FROM staging.companies           WHERE source_file_id = %(f)s"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--confirm", action="store_true", help="actually delete (Ines's decision)")
    args = ap.parse_args()

    spec = spec_for(args.file)
    file_hash = sha256_file(DATA_DIR / spec["filename"])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, row_count_imported FROM staging.source_files WHERE file_hash = %s", (file_hash,))
    row = cur.fetchone()
    if not row:
        sys.exit("this file was never registered — nothing to unload")
    sfid = row[0]
    p = {"f": sfid}
    print(f"\n{spec['filename']}\n  source_file_id={sfid}  row_count_imported={row[1]}\n")
    before = {}
    for k, sql in COUNTS.items():
        cur.execute(sql, p)
        before[k] = cur.fetchone()[0]
        print(f"  {k:<20} {before[k]:>8,}")
    if args.dry_run:
        print("\n  DRY RUN — nothing deleted.")
        return 0

    # values this file's rows could have COALESCE-written onto merged companies
    cur.execute("""SELECT array_agg(DISTINCT upper(replace(coalesce(raw_json->>'NAF',''), '.', ''))),
                          array_agg(DISTINCT raw_json->>'date_creation_enrichi')
                   FROM raw.ingest_rows WHERE source_file_id = %(f)s""", p)
    nafs, dates = cur.fetchone()
    import re
    from datetime import datetime
    parsed = set()
    for d in (dates or []):
        if not d:
            continue
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y %m %d %H %M %S"):
            try:
                parsed.add(datetime.strptime(d[:19], fmt).date().isoformat()); break
            except ValueError:
                continue
    cur.execute(RESET_MERGED_COMPANIES, {**p, "naf_values": [n for n in (nafs or []) if n],
                                          "date_values": sorted(parsed)})
    print(f"\n  merged companies reset : {cur.rowcount:,}")
    conn.commit()

    for label, sql in DELETES:
        cur.execute(sql, p)
        print(f"  deleted {label:<20} {cur.rowcount:>8,}")
        conn.commit()

    cur.execute("UPDATE staging.source_files SET row_count_imported = NULL WHERE id = %(f)s", p)
    conn.commit()
    print("\n  source_files.row_count_imported = NULL -> m1_s3_ingest will RESUME this file.")
    for k, sql in COUNTS.items():
        cur.execute(sql, p)
        print(f"  after {k:<20} {cur.fetchone()[0]:>8,}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
