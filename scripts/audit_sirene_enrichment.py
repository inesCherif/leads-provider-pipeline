"""
Audit — SIRENE enrichment correctness
======================================
Checks whether m1_s4_sirene_enrich.py enriched companies with the RIGHT company's
data.

The concern: m1_s4 queries
    https://recherche-entreprises.api.gouv.fr/search?q=<siren>
which is a FULL-TEXT SEARCH endpoint, not an exact-SIREN lookup, and then accepts
results[0] without ever checking that the returned SIREN equals the requested one
(m1_s4_sirene_enrich.py:156-159). If the API's ranking ever put a different entity
first, that company was enriched with another company's legal name, NAF code,
legal form and creation date - and then stamped sirene_last_checked_at, so it is
never re-checked. Silent and permanent.

This script samples already-enriched companies, re-queries the API, and reports:
  - was results[0] the requested SIREN?
  - if not, did the correct SIREN appear anywhere in the results?
  - does the stored naf_code match what the correct entity actually has?

READ-ONLY. Makes no database writes of any kind.

Usage:
    python scripts/audit_sirene_enrichment.py            # 150 sample
    python scripts/audit_sirene_enrichment.py --sample 500
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import aiohttp
    import psycopg2
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

API_BASE       = "https://recherche-entreprises.api.gouv.fr/search"
MAX_CONCURRENT = 2
REQUEST_DELAY  = 0.4
TIMEOUT        = 15

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("audit")


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url)


def sample_enriched(cur, n: int) -> list[tuple]:
    """Random sample of companies m1_s4 claims to have enriched."""
    cur.execute(
        """
        SELECT siren, legal_name, btrim(naf_code)
        FROM staging.companies
        WHERE sirene_last_checked_at IS NOT NULL
          AND siren IS NOT NULL
          AND naf_code IS NOT NULL
        ORDER BY random()
        LIMIT %s
        """,
        (n,),
    )
    return cur.fetchall()


async def check_one(session, sem, siren, stored_name, stored_naf) -> dict:
    async with sem:
        await asyncio.sleep(REQUEST_DELAY)
        try:
            async with session.get(f"{API_BASE}?q={siren}&page=1&per_page=10") as r:
                if r.status != 200:
                    return {"siren": siren, "verdict": f"http_{r.status}"}
                data = await r.json(content_type=None)
        except Exception as e:
            return {"siren": siren, "verdict": f"error: {type(e).__name__}"}

    results = data.get("results", [])
    if not results:
        return {"siren": siren, "verdict": "no_results"}

    first_siren = (results[0].get("siren") or "").strip()
    # the entity that ACTUALLY has the SIREN we asked for
    correct = next((r for r in results if (r.get("siren") or "").strip() == siren), None)

    if first_siren == siren:
        verdict = "ok_first_is_correct"
    elif correct is not None:
        verdict = "WRONG_first_but_correct_present"
    else:
        verdict = "WRONG_correct_absent"

    out = {
        "siren": siren,
        "verdict": verdict,
        "stored_name": stored_name,
        "stored_naf": stored_naf,
        "first_siren": first_siren,
        "first_name": results[0].get("nom_raison_sociale"),
        "n_results": len(results),
    }
    if correct is not None:
        true_naf = (correct.get("activite_principale") or "").strip()
        out["true_naf"] = true_naf
        out["true_name"] = correct.get("nom_raison_sociale")
        out["naf_mismatch"] = bool(stored_naf and true_naf and stored_naf != true_naf)
    return out


async def run(sample_size: int) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            rows = sample_enriched(cur, sample_size)
    finally:
        conn.close()

    log.info("Auditing %d enriched companies against the live SIRENE API...", len(rows))
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    async with aiohttp.ClientSession(
        timeout=timeout, connector=aiohttp.TCPConnector(limit=MAX_CONCURRENT + 2)
    ) as session:
        results = await asyncio.gather(
            *(check_one(session, sem, s, n, naf) for s, n, naf in rows)
        )

    # ── summary ──────────────────────────────────────────────────────────────
    buckets: dict[str, int] = {}
    for r in results:
        buckets[r["verdict"]] = buckets.get(r["verdict"], 0) + 1

    log.info("")
    log.info("%-36s %8s %8s", "VERDICT", "COUNT", "SHARE")
    log.info("%s", "-" * 56)
    for v, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
        log.info("%-36s %8d %7.1f%%", v, n, 100 * n / len(results))
    log.info("%s", "-" * 56)

    wrong = [r for r in results if r["verdict"].startswith("WRONG")]
    naf_bad = [r for r in results if r.get("naf_mismatch")]

    log.info("")
    if not wrong:
        log.info("RESULT: no contamination found in this sample.")
        log.info("results[0] was the requested SIREN every time.")
    else:
        log.warning("RESULT: %d of %d sampled rows had results[0] != requested SIREN.",
                    len(wrong), len(results))
        for r in wrong[:10]:
            log.warning("  asked %s -> API returned %s (%s)",
                        r["siren"], r.get("first_siren"), r.get("first_name"))

    if naf_bad:
        log.warning("")
        log.warning("%d rows have a STORED naf_code that disagrees with the live API:",
                    len(naf_bad))
        for r in naf_bad[:10]:
            log.warning("  %s stored=%s live=%s  (%s)",
                        r["siren"], r["stored_naf"], r.get("true_naf"), r.get("true_name"))
    else:
        log.info("Stored naf_code agrees with the live API on every checked row.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit SIRENE enrichment correctness")
    ap.add_argument("--sample", type=int, default=150, help="rows to sample (default 150)")
    args = ap.parse_args()
    asyncio.run(run(args.sample))


if __name__ == "__main__":
    main()
