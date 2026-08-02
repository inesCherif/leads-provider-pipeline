r"""
M1-S9-2 — Contact names parsed from the business name (free, no API)
=====================================================================
Fills a contact name for the deliverable businesses that have NO SIREN, so S9-1
could never reach them: the `dirigeants` array is fetched by SIREN, and 21,941
live businesses do not have one. Their only name is the source `trade_name`.

MEASURED FIRST, THEN SCOPED (docs/m1_s9_progress.md, entry "S9-2")
    The plan doc assumed "for sole traders the legal name often IS the person's
    name". For THIS population that is true of a minority: most of the 21,941
    are farm or brand names (FERME DE LA TONNELLERIE, Vente a la ferme,
    Complice des Abeilles). Measured buckets:

        any first-name token present      7,164  (32.7%)  <- upper bound
        CONFIDENT (what this writes)      3,355  (15.3%)  <- sampled 21/25 correct
        longer names with a first name    3,022            <- NOT written
        both tokens are first names         195            <- NOT written
        surname is an initial               422            <- NOT written

    Ines chose the confident bucket only (2026-08-02). A wrong split produces a
    plausible, undeliverable generated address in S9-7 — a silent failure — and
    this pipeline is precision-biased everywhere else.

THE GAZETTEER COMES FROM OUR OWN DATA
    No external first-name list. S9-1 stored 14,408 `prenoms` that the RNE
    supplied as a separate field — 1,112 distinct French first names, already in
    staging.contacts. That is a domain-matched gazetteer we earned for free, and
    it is why the matching token can be identified regardless of order:
        GOURDIN PATRICK  -> PATRICK is in the gazetteer -> surname is GOURDIN
        RICHARD GILLIS   -> RICHARD is in the gazetteer -> surname is GILLIS

THE CONFIDENT RULE
    After stripping legal forms and agricultural nouns (EARL, SCEA, FERME,
    DOMAINE, LA/LE/DE/DU...), accept only when ALL hold:
      1. exactly 2 tokens remain
      2. exactly 1 of them is in the gazetteer  (2 = ambiguous: JULIEN JEAN)
      3. both are >= 3 characters               (rejects `Olivier B.`)
      4. no SAINT / SAINTE / ST anywhere in the original name

    Guard 4 is the fix for the only failure mode the sample showed: saints'
    names are also first names, so `Domaine Saint Georges` and `DOMAINE SAINTE
    ODILE` parsed as people. All 4 of the 25 sampled errors were this shape.

    Stripping before counting is load-bearing. Without it `EARL VINCENT` and
    `EARL THERESE` read as people, when VINCENT and THERESE are the FAMILY
    names and EARL is the legal form.

WHAT IT WRITES
    staging.contacts.first_name / last_name / full_name /
    name_source='source_name_parse', and flips is_generic_contact to FALSE.
    job_title / job_function stay NULL — parsing a name tells us nothing about
    the person's role, and inventing 'owner' would be a guess.

    Names are stored ALL CAPS in "NOM PRENOM" order, matching the 74,499 names
    already in the base ("CROS SERGE"). Title-casing belongs at export time.

    It UPDATES existing contact rows and INSERTS nothing — same reasoning as
    S9-1: v_deliverable_businesses picks one contact per business with a
    DISTINCT ON whose tiebreak includes full_name, so ADDING a row could flip
    which contact represents a business and silently reshuffle a shipped export.

SAFETY
    - Idempotent BY CONSTRUCTION, like m1_s5b_dedup: the write itself is the
      record that it ran. A named business no longer matches needs_name_parse,
      so a re-run selects it no more. No checkpoint column needed.
    - Never overwrites: the UPDATE requires blank full_name AND name_source IS
      NULL, so client-supplied names (74,499) and S9-1 names are untouchable.
    - Skips companies where any OTHER contact is already named, for the
      view-reshuffle reason above.
    - Read-only until you drop --dry-run. --dry-run prints the parse decisions.

Usage:
    python scripts/m1_s9d_nameparse.py --dry-run --limit 40   # eyeball the parses
    python scripts/m1_s9d_nameparse.py --dry-run              # full preview, no writes
    python scripts/m1_s9d_nameparse.py                        # apply (seconds, no API)
"""

import argparse
import json
import logging
import os
import re
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
log = logging.getLogger("m1_s9d")

SCRIPT_NAME = "m1_s9d_nameparse.py"
NAME_SOURCE = "source_name_parse"

# ─── Tokens stripped before the 2-token test ─────────────────────────────────
# Legal forms (same vocabulary as m1_s5b_dedup.py), civility titles, and the
# agricultural nouns this provider puts in front of a farmer's name.
STRIP_TOKENS = {
    # legal forms
    "EARL", "GAEC", "SARL", "EURL", "SASU", "SAS", "SCEA", "SCI", "SCA", "SNC",
    "GFA", "CUMA", "SC", "SA", "EA", "EI", "COOP", "SCV", "SEP",
    # civility
    "M", "MR", "MME", "MLLE", "MONSIEUR", "MADAME",
    # agricultural / brand nouns
    "ELEVAGE", "ELEVEUR", "ELEVEUSE", "FERME", "DOMAINE", "EXPLOITATION",
    "AGRICOLE", "AGRICULTEUR", "PRODUCTEUR", "PRODUCTEURS", "VERGER", "RUCHER",
    "HARAS", "ECURIE", "ECURIES", "CHATEAU", "MAS", "CLOS", "BERGERIE",
    # articles / prepositions
    "LA", "LE", "LES", "DE", "DU", "DES", "ET", "AU", "AUX", "D", "L", "EN",
}

# Guard 4: saints' names are also first names. Any of these anywhere in the
# ORIGINAL name disqualifies the row. All 4 sampled false positives were this.
SAINT_RE = re.compile(r"\b(SAINT|SAINTE|SAINTS|SAINTES|ST|STE)\b")

GAZETTEER_SQL = """
SELECT DISTINCT upper(btrim(first_name)) AS fn
FROM staging.contacts
WHERE name_source = 'rne_dirigeant'
  AND first_name IS NOT NULL
  AND length(btrim(first_name)) >= 3
"""

TARGETS_SQL = """
SELECT q.business_id, q.display_name
FROM public.v_enrichment_queue q
WHERE q.needs_name_parse
ORDER BY q.business_id
"""

# Identical guards to m1_s9c_dirigeants.py. See the module docstring.
UPDATE_CONTACT_SQL = """
UPDATE staging.contacts c
SET first_name         = %(first_name)s,
    last_name          = %(last_name)s,
    full_name          = %(full_name)s,
    name_source        = %(name_source)s,
    is_generic_contact = FALSE,
    updated_at         = NOW()
WHERE c.company_id = %(company_id)s
  AND btrim(COALESCE(c.full_name, '')) = ''
  AND c.name_source IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM staging.contacts c2
      WHERE c2.company_id = c.company_id
        AND btrim(COALESCE(c2.full_name, '')) <> ''
  )
"""

AUDIT_SQL = """
INSERT INTO audit.audit_log
    (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
VALUES
    ('staging.contacts', NULL, 'full_name', NULL, %s, %s, %s)
"""


def deaccent(s: str) -> str:
    """NFD-decompose and drop combining marks. 'JÉRÔME' -> 'JEROME'."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


def tokenise(display_name: str) -> list[str]:
    """Uppercase, de-accent, non-letters to spaces, split. Digits are dropped:
    they are never part of a person's name here ('ECOUTE TON CHIEN 21 39')."""
    cleaned = re.sub(r"[^A-Za-z]+", " ", deaccent(display_name).upper())
    return [t for t in cleaned.split() if t]


def parse_name(display_name: str, gazetteer: set[str]) -> tuple[str, str] | None:
    """Return (first_name, last_name) if the confident rule holds, else None."""
    raw_tokens = tokenise(display_name)
    if not raw_tokens:
        return None

    # Guard 4 — checked against the ORIGINAL token stream, before stripping,
    # because STRIP_TOKENS does not contain SAINT and must not: 'Saint' is a
    # meaningful part of the name, it is just not a person's first name here.
    if SAINT_RE.search(" ".join(raw_tokens)):
        return None

    kept = [(i, t) for i, t in enumerate(raw_tokens) if t not in STRIP_TOKENS]
    tokens = [t for _, t in kept]

    if len(tokens) != 2:                                    # rule 1
        return None
    if any(len(t) < 3 for t in tokens):                     # rule 3
        return None

    # Rule 5 — the two tokens must be ADJACENT in the original name. A person's
    # forename and surname sit next to each other; a preposition between them
    # means the phrase is descriptive, not a name:
    #     AUX ARMES DE DIANE -> ARMES <DE> DIANE   rejected
    #     M PHILIPPE PRIVAT  -> PHILIPPE PRIVAT    kept (the M is before, not between)
    # Cost: nobiliary particles ('PIERRE DE VILLENEUVE') are rejected too. In a
    # precision-biased pipeline losing a real name beats writing a fake one.
    if kept[1][0] - kept[0][0] != 1:                        # rule 5
        return None

    hits = [t for t in tokens if t in gazetteer]
    if len(hits) != 1:                                      # rule 2
        return None

    first = hits[0]
    last = tokens[0] if tokens[1] == first else tokens[1]
    if first == last:
        return None
    return first, last


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    # Keepalives: the pooler drops sessions that look idle mid-query. m1_s4 lacks
    # this and dies on long runs (CLAUDE.md gotchas, 2026-07-22).
    return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


def run(args) -> None:
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(GAZETTEER_SQL)
    gazetteer = {r[0] for r in cur.fetchall()}
    log.info("First-name gazetteer: %d distinct names (from S9-1 RNE prenoms)",
             len(gazetteer))
    if len(gazetteer) < 200:
        sys.exit("Gazetteer suspiciously small — has S9-1 run? Aborting rather "
                 "than writing a handful of low-coverage parses.")

    cur.execute(TARGETS_SQL)
    targets = cur.fetchall()
    log.info("Businesses needing a parsed name: %d", len(targets))

    stats = {"targets": len(targets), "parsed": 0, "rejected": 0,
             "rows_updated": 0, "no_row_matched": 0}
    shown = 0

    for business_id, display_name in targets:
        parsed = parse_name(display_name or "", gazetteer)
        if parsed is None:
            stats["rejected"] += 1
            continue
        first, last = parsed
        stats["parsed"] += 1

        if args.dry_run and shown < args.limit:
            log.info("  %-55s -> %s / %s", (display_name or "")[:55], first, last)
            shown += 1

        if args.dry_run:
            continue

        cur.execute(UPDATE_CONTACT_SQL, {
            "first_name":  first,
            "last_name":   last,
            "full_name":   f"{last} {first}",      # NOM PRENOM, matching the base
            "name_source": NAME_SOURCE,
            "company_id":  business_id,
        })
        if cur.rowcount:
            stats["rows_updated"] += cur.rowcount
        else:
            stats["no_row_matched"] += 1

        if args.limit and stats["parsed"] >= args.limit:
            log.info("--limit reached, stopping")
            break

    log.info("%s", "-" * 70)
    log.info("Targets                 %6d", stats["targets"])
    log.info("  parsed confidently    %6d  (%.1f%%)", stats["parsed"],
             100.0 * stats["parsed"] / stats["targets"] if stats["targets"] else 0)
    log.info("  rejected by the rule  %6d", stats["rejected"])
    if not args.dry_run:
        log.info("  contact rows written  %6d", stats["rows_updated"])
        log.info("  no blank row to fill  %6d", stats["no_row_matched"])
    log.info("%s", "-" * 70)

    if args.dry_run:
        log.info("DRY-RUN — nothing written.")
        conn.rollback()
        conn.close()
        return

    cur.execute(AUDIT_SQL, (
        json.dumps({"step": "S9-2", "script": SCRIPT_NAME,
                    "gazetteer_size": len(gazetteer), **stats},
                   ensure_ascii=False),
        SCRIPT_NAME,
        "S9-2 name parsing from business name, confident bucket only",
    ))
    conn.commit()

    cur.execute("SELECT count(*) FROM staging.contacts WHERE name_source = %s",
                (NAME_SOURCE,))
    total = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.v_enrichment_queue WHERE needs_name_parse")
    left = cur.fetchone()[0]
    log.info("Verified in DB — named by this step: %d | still needing a parse: %d",
             total, left)
    conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="M1-S9-2: parse contact names from the business name")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    ap.add_argument("--limit", type=int, default=40,
                    help="dry-run: how many parses to print. real run: max rows "
                         "to write (default 40; pass 0 for no cap)")
    args = ap.parse_args()
    if not args.dry_run and args.limit == 40:
        args.limit = 0          # a real run defaults to everything
    run(args)


if __name__ == "__main__":
    main()
