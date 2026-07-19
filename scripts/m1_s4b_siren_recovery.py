"""
M1-S4b — SIREN recovery
========================
Recovers SIREN for companies that arrived without one, by matching name + postal
code against the official registry.

Measured feasibility first (scripts/m1_s4b_siren_recovery_sample.py, 500-company
sample): 42% confident, 6% probable, 2.4% ambiguous, 14% weak, 35.6% no result.
THIS SCRIPT APPLIES CONFIDENT MATCHES ONLY - exactly one candidate whose
normalized name matches ours exactly. probable/ambiguous/weak are recorded as
attempted and left alone for a human.

REVERSIBLE. Every SIREN written here is stamped siren_recovered_at, so the run -
and only this run - can be undone:

    UPDATE staging.companies
       SET siren = NULL, siren_recovered_at = NULL, siren_recovery_method = NULL
     WHERE siren_recovered_at IS NOT NULL;

RESUMABLE. siren_recovery_checked_at is stamped on every attempted row and
committed per batch, so a killed run resumes where it stopped without re-querying.

COLLISION = DEDUPLICATION. staging.companies.siren is UNIQUE. If a recovered SIREN
already belongs to another company, that is not an error to route around - it is
proof the two rows are the same business. Those rows are marked
duplicate_of_company_id -> the existing SIREN holder, with
dedup_method = 'siren_recovery_collision', instead of having the SIREN written.
Recovery therefore doubles as a dedup pass on a hard key.

WHAT THIS DOES NOT DO
It does not enrich, re-qualify or re-dedup. Those are separate steps and must run
afterwards, in this order:
    1. m1_s4_sirene_enrich.py   - newly-SIRENed rows have sirene_last_checked_at
                                  NULL, so they are picked up automatically and
                                  gain naf_code, legal_form, etc.
    2. m1_s5_qualify.py --force - naf_code now exists, so these rows move from
                                  tier 2 (label-only) to tier 1 (official NAF)
    3. m1_s5b_dedup.py          - re-run to catch duplicates newly visible

Usage:
    python scripts/m1_s4b_siren_recovery.py --dry-run --limit 200
    python scripts/m1_s4b_siren_recovery.py
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
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import aiohttp
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

API_BASE       = "https://recherche-entreprises.api.gouv.fr/search"
BATCH_SIZE     = 300
MAX_CONCURRENT = 2
REQUEST_DELAY  = 0.4
TIMEOUT        = 15
METHOD         = "confident_name_postal"

LEGAL_FORMS = {
    "earl", "gaec", "sarl", "eurl", "sasu", "sas", "scea", "sci", "sca", "snc",
    "gfa", "cuma", "sc", "sa", "ea", "eleveur", "eleveurs", "elevage",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m1_s4b")


def norm_key(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(sorted(t for t in s.split() if t and t not in LEGAL_FORMS))


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url)


def fetch_batch(cur, size: int) -> list[tuple]:
    cur.execute(
        """
        SELECT DISTINCT ON (co.id)
               co.id, coalesce(co.legal_name, co.trade_name) AS name,
               btrim(s.postal_code) AS pc
        FROM staging.companies co
        JOIN staging.sites s ON s.company_id = co.id
        WHERE co.siren IS NULL
          AND co.siren_recovery_checked_at IS NULL
          AND co.qualification_status = 'qualified_unverified'
          AND co.duplicate_of_company_id IS NULL
          AND coalesce(co.legal_name, co.trade_name) IS NOT NULL
          AND btrim(s.postal_code) ~ '^[0-9]{5}$'
        ORDER BY co.id, s.id
        LIMIT %s
        """,
        (size,),
    )
    return cur.fetchall()


async def probe(session, sem, cid, name, pc) -> dict:
    async with sem:
        await asyncio.sleep(REQUEST_DELAY)
        url = f"{API_BASE}?{urlencode({'q': name, 'code_postal': pc, 'per_page': 10})}"
        for attempt in range(1, 4):
            try:
                async with session.get(url) as r:
                    if r.status == 429:
                        await asyncio.sleep(3.0 * (2 ** attempt))
                        continue
                    if r.status != 200:
                        return {"id": cid, "verdict": "http_error"}
                    data = await r.json(content_type=None)
                    break
            except Exception:
                if attempt == 3:
                    return {"id": cid, "verdict": "error"}
                await asyncio.sleep(2.0 * attempt)
        else:
            return {"id": cid, "verdict": "error"}

    results = data.get("results", [])
    if not results:
        return {"id": cid, "verdict": "no_result"}

    ours = norm_key(name)
    exact = [r for r in results if norm_key(r.get("nom_complet") or r.get("nom_raison_sociale")) == ours and ours]

    if len(exact) == 1:
        return {"id": cid, "verdict": "confident",
                "siren": (exact[0].get("siren") or "").strip(),
                "matched_name": exact[0].get("nom_complet")}
    if len(exact) > 1:
        return {"id": cid, "verdict": "ambiguous"}

    best = max(
        (SequenceMatcher(None, ours, norm_key(r.get("nom_complet") or r.get("nom_raison_sociale"))).ratio()
         for r in results), default=0.0)
    return {"id": cid, "verdict": "probable" if best >= 0.80 else "weak"}


def apply_batch(cur, probes: list[dict], dry_run: bool) -> dict:
    """Write confident matches; turn SIREN collisions into duplicate links."""
    stats = {"recovered": 0, "collision_deduped": 0, "skipped": 0}

    confident = [p for p in probes if p["verdict"] == "confident" and p.get("siren")]
    stats["skipped"] = len(probes) - len(confident)

    if confident:
        # Which of these SIRENs already belong to an existing company?
        cur.execute(
            "SELECT siren, id FROM staging.companies WHERE siren = ANY(%s)",
            ([p["siren"] for p in confident],),
        )
        owner = {s.strip(): i for s, i in cur.fetchall()}

        to_set, to_dedup, seen = [], [], {}
        for p in confident:
            s = p["siren"]
            if s in owner:                       # already held by another company
                to_dedup.append((owner[s], p["id"]))
            elif s in seen:                      # two rows in THIS batch matched the same SIREN
                to_dedup.append((seen[s], p["id"]))
            else:
                seen[s] = p["id"]
                to_set.append((s, p["id"]))

        if not dry_run:
            if to_set:
                # METHOD is a module constant, never user input, so embedding it in
                # the statement is safe. execute_values reserves the single %s for
                # its VALUES list, so a second placeholder cannot be bound here.
                psycopg2.extras.execute_values(cur, f"""
                    UPDATE staging.companies c
                    SET siren = v.siren,
                        siren_recovered_at = NOW(),
                        siren_recovery_method = '{METHOD}'
                    FROM (VALUES %s) AS v(siren, id)
                    WHERE c.id = v.id::uuid
                """, [(s, str(i)) for s, i in to_set], template="(%s, %s)")
            if to_dedup:
                psycopg2.extras.execute_values(cur, """
                    UPDATE staging.companies c
                    SET duplicate_of_company_id = v.survivor::uuid,
                        dedup_method = 'siren_recovery_collision',
                        dedup_checked_at = NOW()
                    FROM (VALUES %s) AS v(survivor, id)
                    WHERE c.id = v.id::uuid
                """, [(str(a), str(b)) for a, b in to_dedup], template="(%s, %s)")

        stats["recovered"] = len(to_set)
        stats["collision_deduped"] = len(to_dedup)

    if not dry_run and probes:
        psycopg2.extras.execute_values(cur, """
            UPDATE staging.companies c
            SET siren_recovery_checked_at = NOW()
            FROM (VALUES %s) AS v(id)
            WHERE c.id = v.id::uuid
        """, [(str(p["id"]),) for p in probes], template="(%s)")

    return stats


async def run(args) -> None:
    conn = get_conn()
    conn.autocommit = False
    totals = {"attempted": 0, "recovered": 0, "collision_deduped": 0}
    verdicts: dict[str, int] = {}
    started = time.time()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM (
                  SELECT DISTINCT co.id FROM staging.companies co
                  JOIN staging.sites s ON s.company_id = co.id
                  WHERE co.siren IS NULL AND co.siren_recovery_checked_at IS NULL
                    AND co.qualification_status='qualified_unverified'
                    AND co.duplicate_of_company_id IS NULL
                    AND coalesce(co.legal_name, co.trade_name) IS NOT NULL
                    AND btrim(s.postal_code) ~ '^[0-9]{5}$') t
            """)
            pending = cur.fetchone()[0]
            target = min(pending, args.limit) if args.limit else pending
            log.info("Pending: %s | this run: %s | mode: %s",
                     f"{pending:,}", f"{target:,}",
                     "DRY-RUN" if args.dry_run else "APPLY")
            if target == 0:
                log.info("Nothing to do.")
                return

            sem = asyncio.Semaphore(MAX_CONCURRENT)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                connector=aiohttp.TCPConnector(limit=MAX_CONCURRENT + 2),
            ) as session:
                while totals["attempted"] < target:
                    size = min(BATCH_SIZE, target - totals["attempted"])
                    batch = fetch_batch(cur, size)
                    if not batch:
                        break

                    probes = await asyncio.gather(
                        *(probe(session, sem, c, n, p) for c, n, p in batch))
                    for p in probes:
                        verdicts[p["verdict"]] = verdicts.get(p["verdict"], 0) + 1

                    st = apply_batch(cur, probes, args.dry_run)
                    if args.dry_run:
                        conn.rollback()
                    else:
                        conn.commit()

                    totals["attempted"] += len(batch)
                    totals["recovered"] += st["recovered"]
                    totals["collision_deduped"] += st["collision_deduped"]

                    done = totals["attempted"]
                    rate = done / max(time.time() - started, 1)
                    eta = (target - done) / rate / 60 if rate else 0
                    log.info("%6s/%s (%4.1f%%) | recovered %s | collisions %s | ETA %.0f min",
                             f"{done:,}", f"{target:,}", 100 * done / target,
                             f"{totals['recovered']:,}", f"{totals['collision_deduped']:,}", eta)

            if not args.dry_run:
                cur.execute("""
                    INSERT INTO audit.audit_log
                      (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
                    VALUES ('staging.companies', NULL, 'siren', NULL, %s, %s, %s)
                """, (json.dumps({**totals, "verdicts": verdicts, "method": METHOD}),
                      "m1_s4b_siren_recovery.py",
                      f"SIREN recovery, method={METHOD}"))
                conn.commit()
    finally:
        conn.close()

    log.info("")
    log.info("%-14s %8s %8s", "VERDICT", "COUNT", "SHARE")
    log.info("%s", "-" * 34)
    for v, c in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        log.info("%-14s %8d %7.1f%%", v, c, 100 * c / max(totals["attempted"], 1))
    log.info("%s", "-" * 34)
    log.info("Attempted            : %s", f"{totals['attempted']:,}")
    log.info("SIREN recovered      : %s", f"{totals['recovered']:,}")
    log.info("Deduped by collision : %s", f"{totals['collision_deduped']:,}")
    log.info("Elapsed              : %.1f min", (time.time() - started) / 60)


def main() -> None:
    ap = argparse.ArgumentParser(description="M1-S4b SIREN recovery (confident matches only)")
    ap.add_argument("--dry-run", action="store_true", help="probe and report, write nothing")
    ap.add_argument("--limit", type=int, help="cap rows attempted this run")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
