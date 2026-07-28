"""
M1-S8 — Campaign export
=======================
Turns the qualified pool in public.v_qualified_contacts into delivery-ready CSV
files for the client. This is the only step that takes data OUT of the database;
everything upstream leaves it in Postgres.

READ-ONLY. This script issues a single SELECT and never writes to the database.

Two files, split by channel (Ines, 2026-07-22) — the two channels have different
downstream workflows, so they ship separately:
    <sector>_email.csv   leads with an email  -> input to M1-S6 verify-at-send-time
    <sector>_phone.csv   leads with only a phone -> telesales

One row per real business, not per contact. The view is contact-grained and keeps
duplicates on purpose (migration 008: a duplicate sometimes holds an email the
survivor lacks), so this script does the collapsing in two DISTINCT ON passes:

  pass 1  one contact per business  — drop is_duplicate rows, then pick the best
          contact per business_id. The view's LATERAL already picked the best
          email *within* a contact; this picks the best *contact* within a business.
  pass 2  one business per family   — collapse shared_address_group. A farmer often
          operates through several legal entities at one address (sole trader +
          EARL, or EARL + SCI holding the land). They are legally distinct and are
          deliberately NOT merged in staging, but they share one decision-maker, so
          a campaign must only contact them once. Businesses with no shared address
          are their own group via the COALESCE.

Both passes use the same preference order — has an email, then tier 1, then a
verified email — with a final tiebreak on business_id so the output is byte-stable
across runs. Without that tiebreak the monthly refresh would emit spurious diffs.

Usage:
    pip install psycopg2-binary python-dotenv

    # Show what would be written, touch nothing (do this first):
    python scripts/m1_s8_export.py --dry-run

    # Write exports/agriculture_email.csv and exports/agriculture_phone.csv:
    python scripts/m1_s8_export.py

    # Slice it:
    python scripts/m1_s8_export.py --tier 1 --department 34,30,12
    python scripts/m1_s8_export.py --channel email
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
from collections import Counter
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
log = logging.getLogger("m1_s8")

SECTOR_SLUG = "agriculture"

# ─── Output columns ───────────────────────────────────────────────────────────
# (view column, client-facing header). French headers: the client works in French
# and opens these in Excel. business_id is carried through as a stable join key so
# campaign sends can be tracked back to staging.companies.

COLUMNS = [
    ("business_id",      "ID"),
    ("siren",            "SIREN"),
    ("siret",            "SIRET"),
    ("display_name",     "Raison sociale"),
    ("source_status",    "Statut"),
    ("legal_name",       "Denomination INSEE"),
    ("trade_name",       "Nom commercial"),
    ("naf_code",         "Code NAF"),
    ("naf_label",        "Activite"),
    ("address_line1",    "Adresse"),
    ("postal_code",      "Code postal"),
    ("city",             "Ville"),
    ("department_code",  "Departement"),
    ("full_name",        "Contact"),
    ("job_title",        "Fonction"),
    ("phone_main",       "Telephone"),
    ("email_address",    "Email"),
    ("tier",             "Niveau de confiance"),
]

TIER_LABEL = {
    "tier1_official_naf": "1 - NAF officiel (INSEE)",
    "tier2_source_label": "2 - libelle source",
}

# Columns Excel would silently corrupt if it guessed the type. SIRET is 14 digits
# and becomes 4,47956E+13 as a number; postal codes and department codes lose their
# leading zero. In .xlsx these are forced to text format; there is no equivalent
# protection in CSV, which is why xlsx is the client-facing format.
TEXT_COLUMNS = {"business_id", "siren", "siret", "naf_code",
                "postal_code", "department_code", "phone_main"}


# Control characters are illegal in the XML that .xlsx is built from, and openpyxl
# hard-errors on them. They are present in the source Excel files — an address
# reading "3 Av de Rivesaltes" carries an embedded control char that is invisible
# in a text editor and that CSV passed through silently. Strip, don't fail: the
# character is noise, and one bad address should not sink a 76k-row deliverable.
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def cell_value(row: dict, col: str) -> str:
    """One row's value for one column, normalised for a human reader."""
    return ILLEGAL_XML.sub("", _raw_value(row, col)).strip()


def _raw_value(row: dict, col: str) -> str:
    if col == "tier":
        return TIER_LABEL.get(row["tier"], row["tier"] or "")
    if col == "source_status":
        # The source file's own verdict, recovered by m1_s9a. Uppercased when
        # closed so it is impossible to miss while scrolling a 76k-row sheet.
        # 'Non trouvé' and NULL both mean the provider could not confirm the
        # business — say so plainly rather than leaving a blank cell.
        st = (row.get("source_status") or "").strip()
        if not st or st.lower().startswith(("non trouv", "introuvable")):
            return "Non verifie"
        return "FERME" if row.get("source_closed") else st
    # NB: the legal_name -> trade_name fallback is NOT applied here. It is done in
    # SQL as display_name ("Raison sociale"); "Denomination INSEE" stays strictly
    # the official INSEE name and is blank where there is none, on purpose.
    if col == "postal_code":
        # French postal codes are always 5 digits; 1,508 rows arrived with the
        # leading zero stripped upstream (01250 stored as 1250). Restore it — the
        # same 4-digit repair migration 006 already does to derive department_code.
        pc = (row.get("postal_code") or "").strip()
        return pc.zfill(5) if pc.isdigit() and len(pc) == 4 else pc
    return row.get(col) or ""

# ─── Selection ────────────────────────────────────────────────────────────────
# One query, two DISTINCT ON passes. Filters are applied AFTER collapsing so that
# --department never changes which entity represents a family: the family's chosen
# entity is decided once, globally, then filtered. Deciding it per-filter would
# make the same business appear under different SIRENs in different exports.

SELECT_SQL = """
WITH per_business AS (
    SELECT DISTINCT ON (business_id)
        business_id, shared_address_group, tier,
        siren, siret, legal_name, trade_name, naf_code, naf_label,
        address_line1, postal_code, city, department_code,
        full_name, job_title, phone_main, email_address,
        source_status, source_closed
    FROM public.v_qualified_contacts
    WHERE NOT is_duplicate
    ORDER BY
        business_id,
        -- A business with several sites: never let a site the source reported
        -- closed represent it while a live one exists.
        source_closed ASC,
        (email_address IS NOT NULL) DESC,
        (tier = 'tier1_official_naf') DESC,
        email_verified DESC NULLS LAST,
        -- the view exposes no contact_id, so tiebreak on the contact's own fields.
        -- Two contacts tying on all four are identical for export purposes.
        full_name NULLS LAST, email_address NULLS LAST, phone_main NULLS LAST
),
per_family AS (
    SELECT DISTINCT ON (COALESCE(shared_address_group, business_id::text))
        *
    FROM per_business
    ORDER BY
        COALESCE(shared_address_group, business_id::text),
        -- Same rule one level up: when a farmer's entities share an address and
        -- one has closed, the live sibling is the one worth contacting.
        source_closed ASC,
        (email_address IS NOT NULL) DESC,
        (tier = 'tier1_official_naf') DESC,
        business_id
)
SELECT
    *,
    -- legal_name is only populated where SIRENE supplied it. 44,876 qualified rows
    -- have none — every tier-2 row (no SIREN, so no official name) plus 7,700 tier-1
    -- ones — but all of them carry the source Excel's trade_name. Exporting
    -- legal_name alone ships a file blank in half its most important column.
    -- (Do not write a literal percent sign anywhere in this SQL: psycopg2 reads it
    --  as a parameter placeholder even inside a comment, and the query dies with
    --  "TypeError: dict is not a sequence".)
    -- Exactly 1 business in the whole dataset has neither.
    COALESCE(NULLIF(btrim(legal_name), ''), trade_name) AS display_name
FROM per_family
WHERE (%(tier)s IS NULL OR tier = %(tier)s)
  AND (%(departments)s IS NULL OR department_code = ANY(%(departments)s))
  AND (NOT %(exclude_closed)s OR NOT source_closed)
ORDER BY business_id
"""

TIER_ARG = {"1": "tier1_official_naf", "2": "tier2_source_label", "both": None}


def connect(attempts: int = 4):
    """Connect, retrying transient pooler failures.

    The Supabase session pooler intermittently rejects a fresh connection with
    EAUTHTIMEOUT or drops it as 'connection already closed'. Both look like bad
    credentials but are transient — this bit m1_s4 repeatedly (see CLAUDE.md).
    A bounded retry is the difference between a re-runnable export and one that
    needs babysitting.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    for attempt in range(1, attempts + 1):
        try:
            # TCP keepalives are load-bearing here, not a nicety. The selection
            # query runs for tens of seconds server-side while the client sends
            # nothing, and the pooler (or the NAT in front of it) drops what looks
            # like an idle SSL session — surfacing as 'SSL SYSCALL error: EOF
            # detected' mid-execute. Keepalives hold the socket open. Same failure
            # mode CLAUDE.md documents for m1_s4.
            return psycopg2.connect(
                url,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
            )
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if attempt == attempts:
                raise
            wait = 3 * attempt
            log.warning("Connection attempt %d/%d failed (%s) — retrying in %ds",
                        attempt, attempts, str(e).strip().splitlines()[0], wait)
            time.sleep(wait)


def fetch(tier: str, departments: list[str] | None,
          exclude_closed: bool = False, attempts: int = 4) -> list[dict]:
    """Run the selection, reconnecting if the pooler drops us mid-query.

    Retrying the *connection* is not enough: this query scans ~100k rows and the
    pooler will close an idle-looking SSL session part-way through with 'SSL
    connection has been closed unexpectedly'. Observed 2026-07-22. The whole
    fetch-and-reconnect has to be retryable, which is the fix CLAUDE.md still
    lists as outstanding for m1_s4. Safe to retry blindly — this is read-only.
    """
    for attempt in range(1, attempts + 1):
        conn = connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(SELECT_SQL,
                            {"tier": TIER_ARG[tier], "departments": departments,
                             "exclude_closed": exclude_closed})
                return [dict(r) for r in cur.fetchall()]
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if attempt == attempts:
                raise
            wait = 5 * attempt
            log.warning("Query attempt %d/%d dropped (%s) — reconnecting in %ds",
                        attempt, attempts, str(e).strip().splitlines()[0], wait)
            time.sleep(wait)
        finally:
            # The pooler has a limited slot count; psycopg2's context manager ends
            # the transaction but never closes the connection.
            try:
                conn.close()
            except Exception:
                pass


def write_csv(path: Path, rows: list[dict], delimiter: str = ";") -> None:
    """CSV for systems and imports — NOT the client-facing format.

    Two encoding traps, both hit in testing:
      - utf-8-sig, not utf-8. The data is full of French accents (EARL DES PRÉS);
        Excel reads a BOM-less UTF-8 file as cp1252 and renders them as mojibake.
      - ';' not ',' as the delimiter. French/European Excel uses the semicolon as
        its list separator, so a comma-delimited file opens with every row crammed
        into column A. That is what a comma default looked like on 2026-07-22.

    Even with both fixed, CSV cannot stop Excel retyping SIRET as 4,47956E+13 or
    eating the leading zero on postal code 01250. Use xlsx for the client.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        w.writerow([header for _, header in COLUMNS])
        for r in rows:
            w.writerow([cell_value(r, col) for col, _ in COLUMNS])


def write_xlsx(path: Path, rows: list[dict]) -> None:
    """Client-facing format. Identifier columns forced to text so Excel cannot
    retype them; no delimiter to get wrong; header frozen and bolded."""
    try:
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font
    except ImportError:
        sys.exit("openpyxl is required for --format xlsx\nRun: pip install openpyxl")

    path.parent.mkdir(parents=True, exist_ok=True)
    # write_only keeps 76k rows off the heap; openpyxl is otherwise memory-hungry.
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=SECTOR_SLUG[:31])
    ws.freeze_panes = "A2"

    head = []
    for _, header in COLUMNS:
        c = WriteOnlyCell(ws, value=header)
        c.font = Font(bold=True)
        head.append(c)
    ws.append(head)

    for r in rows:
        out = []
        for col, _ in COLUMNS:
            c = WriteOnlyCell(ws, value=cell_value(r, col))
            if col in TEXT_COLUMNS:
                c.number_format = "@"
            out.append(c)
        ws.append(out)
    wb.save(path)


def report(rows: list[dict], email_rows: list[dict], phone_rows: list[dict],
           unreachable: list[dict]) -> None:
    """Print what the caller needs to judge the export — measured, not assumed."""
    log.info("─" * 68)
    log.info("Businesses selected            %7d", len(rows))
    log.info("  -> email file                %7d", len(email_rows))
    log.info("  -> phone file                %7d", len(phone_rows))
    log.info("  -> dropped (no email, no phone) %4d", len(unreachable))

    # Email addresses are shared between businesses in the source data, so the
    # collapse above does not guarantee one row per address. A campaign that mails
    # the same address twice looks like spam, so surface it rather than assume.
    addresses = [r["email_address"] for r in email_rows if r["email_address"]]
    dupe_addresses = sum(c - 1 for c in Counter(a.lower() for a in addresses).values() if c > 1)
    log.info("Distinct email addresses       %7d", len(set(a.lower() for a in addresses)))
    if dupe_addresses:
        log.warning("Repeated email addresses       %7d  (same address on >1 business)",
                    dupe_addresses)

    # Businesses the source file reported closed (recovered by m1_s9a). These are
    # kept by default and flagged, not silently dropped — 'Fermé' is the data
    # provider's verdict, not a registry check. Use --exclude-closed to drop them.
    closed = sum(1 for r in rows if r.get("source_closed"))
    unverified = sum(1 for r in rows if not (r.get("source_status") or "").strip())
    if closed:
        log.warning("Reported CLOSED by source      %7d  (flagged FERME; "
                    "--exclude-closed drops them)", closed)
        log.warning("  of which have an email       %7d  <- do not mail without review",
                    sum(1 for r in rows if r.get("source_closed") and r["email_address"]))
    log.info("Status not verifiable          %7d  (no SIRET to match on)", unverified)

    no_dept = sum(1 for r in rows if not r.get("department_code"))
    no_name = sum(1 for r in rows if not (r.get("full_name") or "").strip())
    log.info("Rows with no department        %7d  (geographic filtering is partial)", no_dept)
    log.info("Rows with no contact name      %7d  (reachable, not personally addressable)",
             no_name)

    tiers = Counter(r["tier"] for r in rows)
    log.info("Tier 1 / tier 2                %7d / %d",
             tiers["tier1_official_naf"], tiers["tier2_source_label"])
    log.info("─" * 68)


def main() -> None:
    ap = argparse.ArgumentParser(description="M1-S8 campaign export (read-only)")
    ap.add_argument("--channel", choices=["email", "phone", "both"], default="both",
                    help="which file(s) to write (default: both)")
    ap.add_argument("--tier", choices=["1", "2", "both"], default="both",
                    help="1 = official NAF, 2 = source label only (default: both)")
    ap.add_argument("--department", metavar="34,30,12",
                    help="comma-separated department codes to keep")
    ap.add_argument("--exclude-closed", action="store_true",
                    help="drop businesses the source file reported as closed "
                         "(default: keep them, flagged FERME in the Statut column)")
    ap.add_argument("--format", choices=["xlsx", "csv", "both"], default="xlsx",
                    help="xlsx = client deliverable (default); csv = for systems")
    ap.add_argument("--limit", type=int, help="cap rows written, for spot checks")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the counts, write no files")
    ap.add_argument("--out-dir", default=str(PROJECT_ROOT / "exports"),
                    help="output directory (default: exports/)")
    args = ap.parse_args()

    departments = None
    if args.department:
        departments = [d.strip() for d in args.department.split(",") if d.strip()]
        log.info("Filtering to departments: %s", ", ".join(departments))

    rows = fetch(args.tier, departments, exclude_closed=args.exclude_closed)

    if args.limit:
        rows = rows[: args.limit]
        log.warning("--limit %d applied: this is a SAMPLE, not a deliverable", args.limit)

    email_rows = [r for r in rows if r["email_address"]]
    phone_rows = [r for r in rows if not r["email_address"] and r["phone_main"]]
    unreachable = [r for r in rows if not r["email_address"] and not r["phone_main"]]

    report(rows, email_rows, phone_rows, unreachable)

    if args.dry_run:
        log.info("--dry-run: nothing written")
        return

    out_dir = Path(args.out_dir)
    formats = ["xlsx", "csv"] if args.format == "both" else [args.format]
    channels = [("email", email_rows), ("phone", phone_rows)]

    written = []
    for name, subset in channels:
        if args.channel not in (name, "both"):
            continue
        for fmt in formats:
            p = out_dir / f"{SECTOR_SLUG}_{name}.{fmt}"
            (write_xlsx if fmt == "xlsx" else write_csv)(p, subset)
            written.append((p, len(subset)))

    for path, n in written:
        log.info("Wrote %s  (%d rows)", path, n)


if __name__ == "__main__":
    main()
