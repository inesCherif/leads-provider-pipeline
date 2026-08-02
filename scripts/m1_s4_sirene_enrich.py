"""
M1-S4 — SIRENE Enrichment
==========================
Enriches staging.companies with official INSEE data via the public API:
  https://recherche-entreprises.api.gouv.fr

For each company with a SIREN that hasn't been enriched yet
(sirene_last_checked_at IS NULL), we:
  1. Call the API
  2. Extract: legal_name, naf_code, legal_form,
              employee_bracket, creation_date, sirene_etat
  3. UPDATE staging.companies (only fill NULL fields — never overwrite)
  4. Mark sirene_last_checked_at = NOW()

naf_label is deliberately NOT written here, and must not be. It holds the raw
`Activite` string from the source Excel, and tier-2 qualification runs its
AGRI_INCLUDE / AGRI_EXCLUDE regexes directly against it (m1_s5_qualify.py).
Overwriting it with an official SIRENE label would silently change the verdict
for every tier-2 row. The API returns `activite_principale` as a bare code with
no label anyway. If the official label is ever wanted, add a SEPARATE column —
do not repurpose this one. (An earlier version of this docstring claimed
naf_label was extracted; it never was.)

Design principles:
  - Idempotent: already-enriched rows (sirene_last_checked_at NOT NULL) are skipped
  - Non-destructive: only fills NULL columns, never overwrites existing values
  - Resumable: checkpoints every BATCH_SIZE rows → safe to kill and restart
  - Rate-limited: respects the API's ~7 req/sec limit
  - Async: uses asyncio + aiohttp for parallel requests (up to MAX_CONCURRENT)

Usage:
    pip install aiohttp python-dotenv psycopg2-binary
    python scripts/m1_s4_sirene_enrich.py

    # Dry-run (no DB writes, just shows what would change):
    python scripts/m1_s4_sirene_enrich.py --dry-run

    # Limit to N companies (for testing):
    python scripts/m1_s4_sirene_enrich.py --limit 100
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import aiohttp
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install aiohttp psycopg2-binary python-dotenv")

# ─── Configuration ────────────────────────────────────────────────────────────

PROJECT_ROOT    = Path(__file__).parent.parent
API_BASE        = "https://recherche-entreprises.api.gouv.fr/search"
BATCH_SIZE      = 500       # rows fetched from DB per batch
MAX_CONCURRENT  = 2         # parallel API calls (2 is safe: API allows ~4 req/sec)
REQUEST_TIMEOUT = 10        # seconds per request
RETRY_ATTEMPTS  = 3         # retries on transient failures
RETRY_BACKOFF   = 3.0       # seconds between retries (doubles each time)
REQUEST_DELAY   = 0.4       # seconds between individual requests within semaphore

# INSEE legal form codes → human-readable labels
NAF_LABEL_FALLBACK = {
    None: None,
}

LEGAL_FORM_CODES = {
    "1000": "Entrepreneur individuel",
    "2110": "Indivision",
    "5202": "SARL",
    "5498": "SA",
    "5710": "SAS",
    "5720": "SASU",
    "6597": "SCEA",
    "6598": "EARL",
    "6599": "GAEC",
    "6540": "SARL agricole",
    "6560": "Groupement agricole",
    "9210": "Association loi 1901",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("m1_s4")

# ─── API helpers ──────────────────────────────────────────────────────────────

def parse_api_result(result: dict) -> dict:
    """
    Extract the enrichment fields we care about from one API result object.
    Returns a dict with keys matching staging.companies columns.
    """
    naf_code = result.get("activite_principale")  # e.g. "01.41Z"
    nature_juridique = result.get("nature_juridique")  # e.g. "6598"

    # Normalize NAF: API returns "01.41Z" → we store "01.41Z" as-is (max 6 chars)
    naf_code_clean = naf_code[:6] if naf_code else None

    # Legal form: try our lookup table, fall back to raw code
    legal_form = LEGAL_FORM_CODES.get(nature_juridique, nature_juridique)

    # Employee bracket (tranche effectif salarié)
    employee_bracket = result.get("tranche_effectif_salarie")
    # "NN" means "non renseigné" (not provided) → treat as None
    if employee_bracket == "NN":
        employee_bracket = None

    # Creation date
    creation_date_raw = result.get("date_creation")
    creation_date = None
    if creation_date_raw and creation_date_raw != "1900-01-01":
        creation_date = creation_date_raw  # already "YYYY-MM-DD" string

    # Administrative state (A = active, F = fermé)
    etat = result.get("etat_administratif")  # "A" or "F"
    if etat not in ("A", "F"):
        etat = None

    # Legal name from SIRENE (most authoritative)
    legal_name = result.get("nom_raison_sociale")

    return {
        "legal_name_sirene": legal_name,
        "naf_code": naf_code_clean,
        "legal_form": legal_form,
        "employee_bracket": employee_bracket,
        "creation_date": creation_date,
        "sirene_etat": etat,
    }


async def fetch_siren(session: aiohttp.ClientSession, siren: str, semaphore: asyncio.Semaphore) -> dict | None:
    """
    Call the API for one SIREN. Returns parsed enrichment dict or None on failure.
    """
    # per_page=10, not 1: the exact-SIREN guard below needs candidates to match
    # against. With per_page=1 a mis-ranked top hit would hide the correct entity.
    url = f"{API_BASE}?q={siren}&page=1&per_page=10"
    async with semaphore:
        await asyncio.sleep(REQUEST_DELAY)  # gentle pacing within the semaphore
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                    if resp.status == 429:
                        wait = RETRY_BACKOFF * (2 ** attempt)
                        log.warning(f"Rate limited for {siren}, waiting {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        log.warning(f"HTTP {resp.status} for SIREN {siren}")
                        return None
                    data = await resp.json(content_type=None)
                    results = data.get("results", [])
                    if not results:
                        return {"_not_found": True}
                    # This is a full-text SEARCH endpoint, not an exact-SIREN
                    # lookup, so results[0] is whatever ranked first - not
                    # necessarily the company we asked for. Taking it blindly
                    # would write another company's name/NAF/legal form onto this
                    # row and then stamp sirene_last_checked_at, making the error
                    # silent and permanent. Match explicitly instead.
                    match = next(
                        (r for r in results
                         if (r.get("siren") or "").strip() == siren),
                        None,
                    )
                    if match is None:
                        log.warning(
                            f"SIREN {siren} not in results (top hit was "
                            f"{(results[0].get('siren') or '?').strip()}) - skipping"
                        )
                        return {"_not_found": True}
                    return parse_api_result(match)
            except asyncio.TimeoutError:
                log.warning(f"Timeout for SIREN {siren} (attempt {attempt})")
            except Exception as exc:
                log.warning(f"Error for SIREN {siren}: {exc} (attempt {attempt})")

            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF * attempt)

    return None  # all retries exhausted


# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    # Keepalives are load-bearing, not a nicety. This script holds one
    # connection for a multi-hour run and used to die after ~3,000 rows with
    # "server closed the connection unexpectedly" / "SSL SYSCALL error: EOF":
    # the Supabase pooler drops sessions it considers idle, and a session
    # waiting on a slow batch looks idle. Probe every 30s so it doesn't.
    # (Same fix as m1_s8_export.py; this was the last script still missing it.)
    return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


def reconnect(conn):
    """Replace a dead connection. Returns (conn, cur).

    Idempotency is what makes this safe: every write is COALESCE-guarded and
    gated on sirene_last_checked_at, so replaying a batch after a drop cannot
    double-write or overwrite. Before this existed the run had to be babysat
    with a shell retry loop — and a bash `for` loop exits 0 even when every
    attempt failed, so its exit code proved nothing.
    """
    try:
        conn.close()
    except Exception:
        pass
    new = get_conn()
    new.autocommit = False
    return new, new.cursor()


def fetch_unenriched_batch(cur, limit_total: int | None, offset: int,
                           batch_size: int, dry_run: bool = False) -> list[tuple]:
    """
    Returns list of (company_id, siren) tuples not yet enriched.
    Respects --limit via limit_total.

    A real run needs no OFFSET: each committed batch stamps
    sirene_last_checked_at, so the processed rows stop matching and the same
    query naturally returns the next slice.

    A DRY RUN writes no checkpoint, so that self-advancing property disappears
    and the identical 500 rows come back forever — the loop's exit condition
    ("the query returned nothing") could never be reached. That is the
    long-documented "--dry-run hangs without --limit" bug. Paginating with an
    explicit OFFSET in dry-run mode gives the loop its own way to terminate,
    independent of the side effect the flag disables.
    """
    effective_limit = batch_size
    if limit_total is not None:
        remaining = limit_total - offset
        effective_limit = min(batch_size, remaining)
        if effective_limit <= 0:
            return []

    cur.execute(
        f"""
        SELECT id, siren
        FROM staging.companies
        WHERE siren IS NOT NULL
          AND sirene_last_checked_at IS NULL
        ORDER BY siren
        LIMIT %s
        {"OFFSET %s" if dry_run else ""}
        """,
        (effective_limit, offset) if dry_run else (effective_limit,),
    )
    return cur.fetchall()


def apply_enrichment(cur, company_id: str, enrichment: dict, dry_run: bool) -> bool:
    """
    UPDATE staging.companies for one row.
    - Only fills NULL columns (non-destructive).
    - Returns True if any field was actually updated.
    """
    if dry_run:
        return True

    # legal_name: use SIRENE official name only if our current value is NULL
    fields_to_set = []
    values = []

    legal_name_sirene = enrichment.pop("legal_name_sirene", None)
    if legal_name_sirene:
        fields_to_set.append("legal_name = COALESCE(legal_name, %s)")
        values.append(legal_name_sirene)

    for col in ("naf_code", "legal_form", "employee_bracket", "creation_date", "sirene_etat"):
        val = enrichment.get(col)
        if val is not None:
            fields_to_set.append(f"{col} = COALESCE({col}, %s)")
            values.append(val)

    # Always mark as checked + update updated_at
    fields_to_set.append("sirene_last_checked_at = NOW()")
    fields_to_set.append("updated_at = NOW()")

    values.append(str(company_id))
    cur.execute(
        f"UPDATE staging.companies SET {', '.join(fields_to_set)} WHERE id = %s",
        values,
    )
    return True


def mark_not_found(cur, company_id: str, dry_run: bool):
    """Mark a company as checked but not found in SIRENE."""
    if not dry_run:
        cur.execute(
            "UPDATE staging.companies SET sirene_last_checked_at = NOW(), updated_at = NOW() WHERE id = %s",
            (str(company_id),),
        )


# ─── Main async worker ────────────────────────────────────────────────────────

async def enrich_batch(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    batch: list[tuple],
    cur,
    dry_run: bool,
) -> tuple[int, int, int]:
    """
    Process one batch of (company_id, siren) pairs.
    Returns (enriched_count, not_found_count, error_count).
    """
    tasks = {
        company_id: asyncio.create_task(fetch_siren(session, siren, semaphore))
        for company_id, siren in batch
    }

    enriched = not_found = errors = 0

    for company_id, task in tasks.items():
        result = await task
        if result is None:
            errors += 1
            # Don't mark as checked — will retry next run
        elif result.get("_not_found"):
            mark_not_found(cur, company_id, dry_run)
            not_found += 1
        else:
            apply_enrichment(cur, company_id, result, dry_run)
            enriched += 1

    return enriched, not_found, errors


async def run(args):
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    dry_run = args.dry_run
    limit_total = args.limit

    if dry_run:
        log.info("DRY-RUN mode — no DB writes will be made")

    # Count total work
    cur.execute(
        "SELECT count(*) FROM staging.companies WHERE siren IS NOT NULL AND sirene_last_checked_at IS NULL"
    )
    total_pending = cur.fetchone()[0]
    effective_total = min(total_pending, limit_total) if limit_total else total_pending
    log.info(f"Companies to enrich: {effective_total:,} (total pending: {total_pending:,})")

    if effective_total == 0:
        log.info("Nothing to do — all companies already enriched.")
        conn.close()
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT + 2)
    
    total_enriched = total_not_found = total_errors = 0
    offset = 0
    start_time = time.time()

    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                batch = fetch_unenriched_batch(cur, limit_total, offset,
                                               BATCH_SIZE, dry_run)
            except psycopg2.OperationalError as exc:
                log.warning("DB connection lost while fetching (%s) — reconnecting", exc)
                conn, cur = reconnect(conn)
                continue
            if not batch:
                break

            batch_start = time.time()
            try:
                enriched, not_found, errors = await enrich_batch(
                    session, semaphore, batch, cur, dry_run)
                if not dry_run:
                    conn.commit()
            except psycopg2.OperationalError as exc:
                # The pooler dropped us mid-batch. Reconnect and REPLAY this
                # batch: the uncommitted writes rolled back, and the checkpoint
                # for those rows is therefore still NULL, so the next fetch
                # returns them again. `continue` without advancing `offset` is
                # deliberate.
                log.warning("DB connection lost mid-batch (%s) — reconnecting "
                            "and replaying this batch", exc)
                conn, cur = reconnect(conn)
                continue

            total_enriched  += enriched
            total_not_found += not_found
            total_errors    += errors
            offset          += len(batch)

            elapsed = time.time() - start_time
            batch_time = time.time() - batch_start
            rate = len(batch) / batch_time if batch_time > 0 else 0
            pct = offset / effective_total * 100 if effective_total else 0
            remaining_secs = (effective_total - offset) / rate if rate > 0 else 0
            eta = f"{int(remaining_secs // 60)}m{int(remaining_secs % 60):02d}s"

            log.info(
                f"[{pct:5.1f}%] processed={offset:,} | enriched={total_enriched:,} | "
                f"not_found={total_not_found:,} | errors={total_errors:,} | "
                f"rate={rate:.1f}/s | ETA={eta}"
            )

            if limit_total and offset >= limit_total:
                break

    elapsed_total = time.time() - start_time
    log.info(
        f"\n{'='*60}\n"
        f"M1-S4 SIRENE Enrichment complete\n"
        f"  Total processed : {offset:,}\n"
        f"  Enriched        : {total_enriched:,}\n"
        f"  Not found       : {total_not_found:,}\n"
        f"  Errors (skipped): {total_errors:,}\n"
        f"  Time elapsed    : {int(elapsed_total // 60)}m{int(elapsed_total % 60):02d}s\n"
        f"{'='*60}"
    )
    conn.close()


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="M1-S4: Enrich staging.companies from SIRENE API")
    parser.add_argument("--dry-run", action="store_true", help="Fetch from API but do not write to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max number of companies to process (for testing)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
