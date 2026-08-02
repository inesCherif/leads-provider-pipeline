r"""
M1-S9-1 — Dirigeant backfill (contact names from the RNE, free)
===============================================================
Fills a contact name for the 14,061 live deliverable businesses that have a
SIREN and a phone but nobody to address. Source is the `dirigeants` array of
https://recherche-entreprises.api.gouv.fr — the same endpoint m1_s4 already
calls, whose dirigeants field m1_s4 throws away.

Measured on a deterministic random sample of 80 targets (2026-08-02): a usable
natural person came back for 80 of 80. This is the highest-yield free step left.

WHAT IT WRITES
    staging.contacts.first_name / last_name / full_name / job_title /
    job_function / name_source='rne_dirigeant', and flips is_generic_contact to
    FALSE — those placeholder rows now name a real person.
    staging.companies.dirigeants_checked_at / dirigeants_count (checkpoint).

    It UPDATES existing contact rows and INSERTS nothing. Every target business
    already owns a contact row (v_qualified_contacts INNER JOINs contacts, so a
    business without one could not be in the deliverable); 14,635 of the 14,637
    are is_generic_contact placeholders holding only a phone. Inserting extra
    contacts instead would change the row count v_deliverable_businesses
    collapses over and could silently reshuffle a shipped export.

WHO GETS PICKED when the RNE lists several (45% of targets)
    Decision taken by Ines 2026-08-02: one best dirigeant per business.
      1. drop `personne morale` entries — a holding company or an audit firm
         (DELOITTE & ASSOCIES turned up as a "dirigeant" in the sample) is not
         someone you mail;
      2. drop statutory auditors (`commissaire aux comptes`) — external, not a
         decision-maker;
      3. rank the rest by role: gérant/président/DG > associé > board member >
         autre > liquidateur (a business in liquidation is ranked last, never
         dropped, and is counted in the run summary);
      4. tiebreak on whether the surname appears in the company name
         (EARL ROGNANT -> ROGNANT), then alphabetically. That last key makes the
         choice total and reproducible — the lesson of migration 012, where a
         DISTINCT ON without a total tiebreak made the export non-deterministic.

NAME SPLITTING — deliberately conservative (Ines, 2026-08-02)
    first_name/last_name are filled ONLY when the API supplies `prenoms`
    separately. When it returns the whole name in one field ("DUPUY JEAN MARC",
    prenoms empty) full_name is set verbatim and first/last stay NULL: a wrong
    split silently produces wrong generated addresses in S9-7, which is worse
    than no split at all.
    `nom` may carry a nom d'usage in parentheses ("PINSON (BRISARD)"); the
    parenthetical is dropped from the stored name and the birth name kept. It is
    recoverable by re-querying the API if it is ever wanted.
    Names are stored ALL CAPS in "NOM PRENOM" order, matching the 74,499 names
    already in the base ("CROS SERGE", "ROGER THOMAS"). Title-casing belongs at
    export/mail-merge time, not in staging.

SAFETY
    - Idempotent: only companies with dirigeants_checked_at IS NULL are called,
      and the UPDATE only fills blank names whose name_source IS NULL. A
      client-supplied name is never overwritten.
    - Companies that already have a name on ANY contact are skipped for the
      write (they still get checkpointed): filling their *other* contact row
      could flip which site represents the business in v_deliverable_businesses,
      whose tiebreak includes full_name. Affects 2 companies.
    - Reconnects. m1_s4 holds one connection for a multi-hour run and dies on a
      pooler drop; this run is the same length, so it reconnects and replays the
      batch instead (replay is safe — every write is guarded).

Usage:
    python scripts/m1_s9c_dirigeants.py --dry-run            # 25 live API probes, writes nothing
    python scripts/m1_s9c_dirigeants.py --dry-run --limit 5
    python scripts/m1_s9c_dirigeants.py --limit 200          # a real but bounded first pass
    python scripts/m1_s9c_dirigeants.py                      # full run, ~1.5-2h, use background
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import aiohttp
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install aiohttp psycopg2-binary python-dotenv")

# ─── Configuration ────────────────────────────────────────────────────────────

API_BASE        = "https://recherche-entreprises.api.gouv.fr/search"
MAX_CONCURRENT  = 2      # proven safe in m1_s4 against this API's ~7 req/s cap
REQUEST_DELAY   = 0.4    # pacing inside the semaphore
REQUEST_TIMEOUT = 15
RETRY_ATTEMPTS  = 3
RETRY_BACKOFF   = 3.0
BATCH_SIZE      = 250    # API results buffered before one DB commit
DRY_RUN_DEFAULT = 25     # --dry-run without --limit probes this many (m1_s4's
                         # --dry-run hangs forever without --limit; this cannot)

SCRIPT_NAME = "m1_s9c_dirigeants.py"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m1_s9c")

# ─── Role ranking ─────────────────────────────────────────────────────────────
# Evaluated in order against the accent-stripped lowercase `qualite`; first match
# wins. Order is load-bearing: "vice-president" must be tested before
# "president", and "administrateur judiciaire" before "administrateur".

EXCLUDED_ROLE = re.compile(r"commissaire aux comptes")

ROLE_RULES = [
    (re.compile(r"liquidateur|administrateur judiciaire|mandataire|curateur"), 5),
    (re.compile(r"vice.?president|president.?adjoint"),                        3),
    (re.compile(r"\bgerant"),                                                  1),
    (re.compile(r"\bpresident"),                                               1),
    (re.compile(r"directeur general|\bdirecteur\b|\bdirectrice\b"),            1),
    (re.compile(r"\bexploitant"),                                              1),
    (re.compile(r"associe"),                                                   2),
    (re.compile(r"administrateur|tresorier|secretaire|membre du"),             3),
]
DEFAULT_RANK   = 4       # 'Autre', or no qualite at all (every sole trader)
WINDING_UP     = 5       # rank reserved for liquidation roles
OWNER_RANKS    = (1, 2)  # -> job_function 'owner'; everything else 'unknown'


def deaccent(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def role_rank(qualite: str | None) -> int | None:
    """Rank a dirigeant's role. None means 'never contact this one'."""
    if not qualite:
        return DEFAULT_RANK
    q = deaccent(qualite).lower()
    if EXCLUDED_ROLE.search(q):
        return None
    for pattern, rank in ROLE_RULES:
        if pattern.search(q):
            return rank
    return DEFAULT_RANK


_TOKEN = re.compile(r"[A-Z0-9]+")


def name_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(_TOKEN.findall(deaccent(text).upper()))


def clean_surname(nom: str) -> str:
    """'PINSON (BRISARD)' -> 'PINSON'. Keeps the birth name, drops the nom d'usage."""
    base = nom.split("(")[0].strip()
    return base or nom.strip()


def choose_dirigeant(dirigeants: list[dict], company_name: str | None) -> tuple[dict | None, int]:
    """Pick the single best natural person. Returns (chosen_or_None, usable_count)."""
    company = name_tokens(company_name)
    candidates = []
    for d in dirigeants or []:
        if d.get("type_dirigeant") != "personne physique":
            continue                                   # holding, audit firm, ...
        nom = (d.get("nom") or "").strip()
        if not nom:
            continue
        rank = role_rank(d.get("qualite"))
        if rank is None:
            continue                                   # commissaire aux comptes
        surname = clean_surname(nom)
        # Surname echoed in the company name is strong evidence this is THE
        # operator rather than a co-signatory: EARL ROGNANT -> ROGNANT ERIC.
        eponymous = bool(name_tokens(surname) & company)
        candidates.append((rank, not eponymous, deaccent(nom).upper(),
                           deaccent(d.get("prenoms") or "").upper(), d))
    if not candidates:
        return None, 0
    candidates.sort(key=lambda c: c[:4])               # total, so reproducible
    return candidates[0][4], len(candidates)


def build_contact(d: dict) -> dict:
    """Turn one chosen dirigeant into the columns staging.contacts wants."""
    nom_raw = (d.get("nom") or "").strip()
    surname = clean_surname(nom_raw)
    prenoms = (d.get("prenoms") or "").strip()
    qualite = (d.get("qualite") or "").strip() or None
    rank = role_rank(qualite)

    if prenoms:
        first = prenoms.split()[0]
        return {
            "first_name": first,
            "last_name":  surname,
            "full_name":  f"{surname} {first}",        # house order: NOM PRENOM
            "job_title":  qualite,
            "job_function": "owner" if rank in OWNER_RANKS else "unknown",
            "winding_up": rank == WINDING_UP,
        }
    # No separate first name: store what we were given, guess nothing.
    return {
        "first_name": None,
        "last_name":  None,
        "full_name":  nom_raw,
        "job_title":  qualite,
        "job_function": "owner" if rank in OWNER_RANKS else "unknown",
        "winding_up": rank == WINDING_UP,
    }


# ─── API ──────────────────────────────────────────────────────────────────────

async def fetch_dirigeants(session, siren: str, semaphore) -> list[dict] | None:
    """Returns the dirigeants array, [] if the company has none, None on failure."""
    url = f"{API_BASE}?q={siren}&page=1&per_page=10"
    async with semaphore:
        await asyncio.sleep(REQUEST_DELAY)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                    if resp.status == 429:
                        wait = RETRY_BACKOFF * (2 ** attempt)
                        log.warning("Rate limited on %s, waiting %.1fs", siren, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        log.warning("HTTP %s for SIREN %s", resp.status, siren)
                        return None
                    data = await resp.json(content_type=None)
                    # Full-text search endpoint, not an exact-SIREN lookup:
                    # results[0] is whatever ranked first. Taking it blindly would
                    # write another company's officer onto this row. Same guard
                    # m1_s4 needed (CLAUDE.md, fixed 2026-07-20).
                    match = next((r for r in data.get("results", [])
                                  if (r.get("siren") or "").strip() == siren), None)
                    if match is None:
                        return []                      # nothing to write, but asked
                    return match.get("dirigeants") or []
            except asyncio.TimeoutError:
                log.warning("Timeout for SIREN %s (attempt %d)", siren, attempt)
            except Exception as exc:
                log.warning("Error for SIREN %s: %s (attempt %d)", siren, exc, attempt)
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
    return None


# ─── DB ───────────────────────────────────────────────────────────────────────

TARGETS_SQL = """
SELECT q.business_id, q.siren, q.display_name
FROM public.v_enrichment_queue q
JOIN staging.companies c ON c.id = q.business_id
WHERE q.needs_dirigeant
  AND c.dirigeants_checked_at IS NULL
ORDER BY q.siren
"""

# Fill the placeholder contact(s) of this company.
#   - never touch a row that already has a name, or one we did not create
#   - the NOT EXISTS skips companies where some OTHER contact is already named:
#     naming the remaining blank row could flip which site represents the
#     business in v_deliverable_businesses, whose tiebreak includes full_name
UPDATE_CONTACT_SQL = """
UPDATE staging.contacts c
SET first_name         = %(first_name)s,
    last_name          = %(last_name)s,
    full_name          = %(full_name)s,
    job_title          = %(job_title)s,
    job_function       = %(job_function)s,
    name_source        = 'rne_dirigeant',
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

CHECKPOINT_SQL = """
UPDATE staging.companies
SET dirigeants_checked_at = NOW(),
    dirigeants_count      = %(count)s,
    updated_at            = NOW()
WHERE id = %(company_id)s
"""


class DB:
    """Connection that survives the pooler dropping a long run.

    m1_s4 holds one connection for its whole multi-hour run and dies on
    `server closed the connection unexpectedly` (CLAUDE.md, known gap). This run
    is the same length, so it reconnects and replays the batch. Replay is safe:
    every statement is guarded and idempotent.
    """

    def __init__(self):
        load_dotenv(PROJECT_ROOT / ".env")
        self.url = os.getenv("SUPABASE_DB_URL")
        if not self.url:
            sys.exit("SUPABASE_DB_URL not set in .env")
        self.conn = None
        self.connect()

    def connect(self, attempts: int = 4):
        for attempt in range(1, attempts + 1):
            try:
                self.conn = psycopg2.connect(
                    self.url, keepalives=1, keepalives_idle=30,
                    keepalives_interval=10, keepalives_count=5)
                self.conn.autocommit = False
                return
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                if attempt == attempts:
                    raise
                wait = 3 * attempt
                log.warning("Connect attempt %d/%d failed (%s) — retrying in %ds",
                            attempt, attempts, str(e).strip().splitlines()[0], wait)
                time.sleep(wait)

    def _reconnect(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.connect()

    def run(self, fn, attempts: int = 4):
        """Run fn(cur) in a transaction, reconnecting and replaying on a drop."""
        for attempt in range(1, attempts + 1):
            try:
                with self.conn.cursor() as cur:
                    result = fn(cur)
                self.conn.commit()
                return result
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                log.warning("DB error (%s) — attempt %d/%d",
                            str(e).strip().splitlines()[0], attempt, attempts)
                if attempt == attempts:
                    raise
                time.sleep(3 * attempt)
                self._reconnect()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def flush_batch(db: DB, writes: list[dict], checkpoints: list[dict]) -> int:
    """Persist one batch. Returns contact rows actually updated."""
    def _do(cur):
        touched = 0
        if writes:
            for w in writes:                       # rowcount is needed per row
                cur.execute(UPDATE_CONTACT_SQL, w)
                touched += cur.rowcount
        if checkpoints:
            psycopg2.extras.execute_batch(cur, CHECKPOINT_SQL, checkpoints,
                                          page_size=200)
        return touched
    return db.run(_do)


# ─── Run ──────────────────────────────────────────────────────────────────────

async def run(args) -> None:
    db = DB()
    dry = args.dry_run
    limit = args.limit or (DRY_RUN_DEFAULT if dry else None)

    if dry:
        log.info("DRY-RUN — calling the API for %d targets, writing nothing", limit)

    targets = db.run(lambda cur: (cur.execute(TARGETS_SQL), cur.fetchall())[1])
    total_pending = len(targets)
    if limit:
        targets = targets[:limit]
    log.info("Targets pending: %d | processing this run: %d", total_pending, len(targets))
    if not targets:
        log.info("Nothing to do — every target already has dirigeants_checked_at.")
        db.close()
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT + 2)

    stats = {"named": 0, "no_person": 0, "api_error": 0, "multi": 0,
             "winding_up": 0, "no_first_name": 0, "rows_updated": 0}
    writes: list[dict] = []
    checkpoints: list[dict] = []
    processed = 0
    start = time.time()

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [(cid, siren, name,
                  asyncio.create_task(fetch_dirigeants(session, siren, semaphore)))
                 for cid, siren, name in targets]

        for company_id, siren, display_name, task in tasks:
            dirigeants = await task
            processed += 1

            if dirigeants is None:
                stats["api_error"] += 1              # not checkpointed -> retried
                continue

            chosen, usable = choose_dirigeant(dirigeants, display_name)
            checkpoints.append({"company_id": str(company_id), "count": usable})

            if chosen is None:
                stats["no_person"] += 1
            else:
                contact = build_contact(chosen)
                if usable > 1:
                    stats["multi"] += 1
                if contact.pop("winding_up"):
                    stats["winding_up"] += 1
                if contact["first_name"] is None:
                    stats["no_first_name"] += 1
                stats["named"] += 1
                contact["company_id"] = str(company_id)
                writes.append(contact)
                if dry and stats["named"] <= 15:
                    log.info("  %-11s %-38.38s -> %-28.28s %-22.22s [1 of %d]",
                             siren, display_name or "", contact["full_name"],
                             contact["job_title"] or "(no role given)", usable)

            if not dry and len(checkpoints) >= BATCH_SIZE:
                stats["rows_updated"] += flush_batch(db, writes, checkpoints)
                writes, checkpoints = [], []
                rate = processed / (time.time() - start)
                remaining = (len(targets) - processed) / rate if rate else 0
                log.info("[%5.1f%%] %d/%d | named=%d no_person=%d err=%d | "
                         "%.1f/s | ETA %dm%02ds",
                         processed / len(targets) * 100, processed, len(targets),
                         stats["named"], stats["no_person"], stats["api_error"],
                         rate, int(remaining // 60), int(remaining % 60))

    if not dry:
        stats["rows_updated"] += flush_batch(db, writes, checkpoints)

    elapsed = time.time() - start
    log.info("%s", "─" * 70)
    log.info("Processed            %6d in %dm%02ds",
             processed, int(elapsed // 60), int(elapsed % 60))
    log.info("  named a person     %6d  (%.1f%%)",
             stats["named"], stats["named"] / processed * 100 if processed else 0)
    log.info("    of several       %6d   RNE listed more than one usable person",
             stats["multi"])
    log.info("    no first name    %6d   full_name only, split left NULL",
             stats["no_first_name"])
    log.info("    IN LIQUIDATION   %6d   best available role is a liquidator",
             stats["winding_up"])
    log.info("  no usable person   %6d  (RNE empty, or only companies/auditors)",
             stats["no_person"])
    log.info("  API errors         %6d  (not checkpointed — a re-run retries them)",
             stats["api_error"])
    log.info("  contact rows written %4d", stats["rows_updated"])
    log.info("%s", "─" * 70)

    if dry:
        log.info("--dry-run: nothing written.")
        db.close()
        return

    verified = db.run(lambda cur: (cur.execute(VERIFY_SQL), cur.fetchone())[1])
    log.info("Verified in DB — contacts named by this step: %d "
             "| businesses still needing one: %d", verified[0], verified[1])

    summary = dict(stats, targets_pending_before=total_pending,
                   contacts_named_total=verified[0], still_needing=verified[1])
    db.run(lambda cur: cur.execute(
        """
        INSERT INTO audit.audit_log
            (table_name, record_id, field_changed, old_value, new_value,
             changed_by, reason)
        VALUES ('staging.contacts', NULL, 'full_name', NULL, %s, %s, %s)
        """,
        (json.dumps(summary, ensure_ascii=False), SCRIPT_NAME,
         "M1-S9-1: contact names backfilled from the recherche-entreprises "
         "dirigeants array (one best natural person per business)")))
    log.info("Committed and logged to audit.audit_log.")
    db.close()


VERIFY_SQL = """
SELECT (SELECT count(*) FROM staging.contacts WHERE name_source = 'rne_dirigeant'),
       (SELECT count(*) FROM public.v_enrichment_queue WHERE needs_dirigeant)
"""


def main() -> None:
    ap = argparse.ArgumentParser(
        description="M1-S9-1 — backfill contact names from the RNE dirigeants array")
    ap.add_argument("--dry-run", action="store_true",
                    help=f"call the API for {DRY_RUN_DEFAULT} targets (or --limit) "
                         "and print what would be written; writes nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N businesses")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
