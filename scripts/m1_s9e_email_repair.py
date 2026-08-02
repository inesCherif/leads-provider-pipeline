r"""
M1-S9-E — Repair source emails whose domain lost its dot
=========================================================
Found 2026-08-02 while verifying the regenerated export: 447 of the 13,435
deliverable emails have a domain with NO DOT — gmailcom, orangefr, wanadoofr,
hotmailfr. Every one of them is a guaranteed hard bounce.

NOT OUR BUG — verified against raw.ingest_rows before touching anything:

    SELECT raw_json->>'Email' FROM raw.ingest_rows
    WHERE lower(raw_json->>'Email') ~ '@(gmailcom|orangefr)$';
    -> 'adecmr08110@gmailcom'   the source file itself is missing the dot

705 source rows are affected, 87,856 are fine. m1_s3_ingest only ever checked
that an address contained '@', so they passed straight through.

WHY THIS MATTERS MORE THAN 3.3% SUGGESTS
    Hard bounces are the single fastest way to wreck a sending domain's
    reputation. A campaign that opens with 447 bounces gets throttled or
    blacklisted, which damages every *valid* address in the same send. The cost
    is not 447 lost leads, it is the deliverability of all 13,435.

THE REPAIR IS DETERMINISTIC, NOT A GUESS
    The TLD sits at the end of the string and is unambiguous, so the dot's
    position is recoverable: gmailcom -> gmail.com, educagrifr -> educagri.fr,
    chateaudelagabellefr -> chateaudelagabelle.fr.

    Longest TLD wins, deliberately: 'com' must be tried before 'om' (Oman) or
    'm', otherwise gmailcom would become gmailc.om. Only the TLDs listed below
    are accepted — an unrecognised ending is left alone rather than guessed at.
    684 of 687 repair; the 3 that do not are left untouched and still flagged.

NON-DESTRUCTIVE, USING THE SCHEMA AS DESIGNED
    staging.emails was built for MULTIPLE candidates per contact with
    independent verification_status (migration 001). So this does not rewrite
    the source value:
        - the malformed row     -> verification_status='invalid', is_primary=FALSE
        - the repaired address  -> INSERTed as a new 'candidate' row, is_primary=TRUE
    The original stays visible and the change is reversible. 'invalid' is
    honest here: a domain with no dot cannot resolve, so we KNOW it is bad
    without needing a verifier.

IDEMPOTENT
    Selection excludes rows already marked 'invalid', so a second run finds
    nothing. The ON CONFLICT DO NOTHING covers the case where the repaired
    address already exists for that contact.

Usage:
    python scripts/m1_s9e_email_repair.py --dry-run
    python scripts/m1_s9e_email_repair.py
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
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m1_s9e")

SCRIPT_NAME = "m1_s9e_email_repair.py"

# Longest first — 'com' must beat 'om'/'m'. Restricted to TLDs actually present
# in this data plus the obvious French/EU ones; anything else is left alone.
#
# 'bzh' (Brittany) is included: keltikfoodbzh -> keltikfood.bzh, and no plausible
# domain ends in those three letters by accident.
#
# 'me' is deliberately EXCLUDED even though protonme -> proton.me would be
# correct. Any domain ending in the letters "me" would match it — fermelapomme
# would become fermelapom.me. One recovered address is not worth a rule that
# silently corrupts others on the next ingest. That address stays flagged.
KNOWN_TLDS = ["coop", "info", "biz", "bzh", "com", "net", "org", "pro", "eu",
              "fr", "be", "ch", "io"]

SELECT_SQL = """
SELECT e.id, e.contact_id, e.email_address, e.source_file_id
FROM staging.emails e
WHERE e.email_address LIKE '%@%'
  AND split_part(e.email_address, '@', 2) NOT LIKE '%.%'
  AND e.verification_status <> 'invalid'
ORDER BY e.id
"""

INSERT_SQL = """
INSERT INTO staging.emails
    (contact_id, email_address, is_primary, verification_status, source_file_id)
VALUES %s
ON CONFLICT (contact_id, email_address) DO NOTHING
"""

INVALIDATE_SQL = """
UPDATE staging.emails
SET verification_status = 'invalid',
    is_primary          = FALSE
WHERE id = ANY(%s::uuid[])
"""

AUDIT_SQL = """
INSERT INTO audit.audit_log
    (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
VALUES ('staging.emails', NULL, 'email_address', NULL, %s, %s, %s)
"""


def repair(address: str) -> str | None:
    """Insert the missing dot before a recognised TLD. None if not repairable."""
    local, _, domain = address.partition("@")
    if not local or not domain or "." in domain:
        return None
    low = domain.lower()
    for tld in KNOWN_TLDS:                      # KNOWN_TLDS is longest-first
        if low.endswith(tld) and len(low) > len(tld):
            return f"{local}@{low[:-len(tld)]}.{tld}"
    return None


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


def run(args) -> None:
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(SELECT_SQL)
    rows = cur.fetchall()
    log.info("Malformed addresses (no dot in domain): %d", len(rows))

    to_insert, to_invalidate, unrepairable = [], [], []
    for email_id, contact_id, address, source_file_id in rows:
        fixed = repair(address)
        if fixed is None:
            unrepairable.append(address)
            continue
        to_insert.append((str(contact_id), fixed, True, "candidate",
                          str(source_file_id)))
        to_invalidate.append(email_id)

    log.info("  repairable            %6d", len(to_insert))
    log.info("  NOT repairable        %6d  (left untouched)", len(unrepairable))
    for a in unrepairable[:10]:
        log.info("      %s", a)

    for _, fixed, *_ in to_insert[:15]:
        log.info("  e.g. -> %s", fixed)

    if args.dry_run:
        log.info("DRY-RUN — nothing written.")
        conn.rollback()
        conn.close()
        return

    if to_insert:
        # Chunked explicitly: psycopg2 issues one statement per page and
        # cur.rowcount then reports only the LAST page. The first run of this
        # script logged "185 inserted" for 685 rows because of exactly that —
        # the same trap already fixed in m1_s9d_nameparse.py and not carried
        # here. An under-reported write looks like silent data loss.
        CHUNK = 500
        inserted = 0
        for i in range(0, len(to_insert), CHUNK):
            psycopg2.extras.execute_values(
                cur, INSERT_SQL, to_insert[i:i + CHUNK], page_size=CHUNK)
            inserted += cur.rowcount
        # Invalidate only AFTER the repaired rows exist, so a contact can never
        # be left with its only address marked invalid and no replacement.
        cur.execute(INVALIDATE_SQL, (to_invalidate,))
        invalidated = cur.rowcount
        log.info("  repaired rows inserted %5d", inserted)
        log.info("  malformed invalidated  %5d", invalidated)

        cur.execute(AUDIT_SQL, (
            json.dumps({"step": "S9-E", "script": SCRIPT_NAME,
                        "malformed": len(rows), "inserted": inserted,
                        "invalidated": invalidated,
                        "unrepairable": len(unrepairable)}, ensure_ascii=False),
            SCRIPT_NAME,
            "Repair source emails whose domain lost its dot (gmailcom -> gmail.com)",
        ))
        conn.commit()

    cur.execute("""
        SELECT count(*) FROM public.v_deliverable_businesses
        WHERE email_address IS NOT NULL
          AND email_address !~ '^[^@\\s]+@[^@\\s]+\\.[A-Za-z]{2,}$'
    """)
    log.info("Verified — malformed emails left in the deliverable: %d",
             cur.fetchone()[0])
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Repair dot-less email domains")
    ap.add_argument("--dry-run", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
