"""
M1-S4b (sampler) — SIREN recovery feasibility
==============================================
Measures whether the ~83k companies with no SIREN can be matched back to the
official registry by name + postal code, BEFORE committing to a multi-hour run.

READ-ONLY. Makes no database writes. Its only output is a match-rate report.

WHY THIS MATTERS
A missing SIREN is the single root cause of three separate problems:
  - those companies cannot be SIRENE-enriched (no NAF code, no legal form)
  - they cannot be reliably deduplicated (SIRET/SIREN is the only hard key)
  - they are stuck in the lower-confidence qualification tier 2
Recovering it collapses all three at once. But a full run is ~4.6 hours at the
API's pacing, so measure the hit rate on a sample first.

METHOD
For each sampled company, query
    recherche-entreprises.api.gouv.fr/search?q=<name>&code_postal=<postal>
and compare the returned entity names against ours. The postal filter is what
makes name matching tractable - it narrows the candidate set to one commune.

Names are compared on the same normalized key the dedup pass uses: unaccent,
lowercase, strip punctuation, drop French legal forms (EARL/GAEC/SARL/SCEA/GFA),
then SORT the word tokens so "MAGINIER FREDERIC" and "FREDERIC MAGINIER" agree.

VERDICTS
  confident   exactly one candidate whose normalized name matches ours exactly
  probable    one candidate, high but inexact similarity (>= 0.80)
  ambiguous   several plausible candidates - would need a human or a tiebreak
  weak        best similarity below 0.80
  no_result   the API returned nothing for that name in that postal code

Only `confident` should be auto-applied in a real run. `probable` and `ambiguous`
are reported so the size of the reviewable middle is known.

Usage:
    python scripts/m1_s4b_siren_recovery_sample.py                # 500 sample
    python scripts/m1_s4b_siren_recovery_sample.py --sample 1000
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode

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
PROBABLE_MIN   = 0.80

LEGAL_FORMS = {
    "earl", "gaec", "sarl", "eurl", "sasu", "sas", "scea", "sci", "sca", "snc",
    "gfa", "cuma", "sc", "sa", "ea", "eleveur", "eleveurs", "elevage",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m1_s4b")


def norm_key(name: str | None) -> str:
    """Same normalization as the dedup pass: unaccent, strip legal forms, sort tokens."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    toks = [t for t in s.split() if t and t not in LEGAL_FORMS]
    return " ".join(sorted(toks))


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url)


def sample_no_siren(cur, n: int) -> list[tuple]:
    """Companies with no SIREN that have both a usable name and a postal code."""
    cur.execute(
        """
        SELECT DISTINCT ON (co.id)
               co.id, coalesce(co.legal_name, co.trade_name) AS name,
               btrim(s.postal_code) AS pc
        FROM staging.companies co
        JOIN staging.sites s ON s.company_id = co.id
        WHERE co.siren IS NULL
          AND co.qualification_status = 'qualified_unverified'
          AND co.duplicate_of_company_id IS NULL
          AND coalesce(co.legal_name, co.trade_name) IS NOT NULL
          AND btrim(s.postal_code) ~ '^[0-9]{5}$'
        ORDER BY co.id, s.id
        LIMIT %s
        """,
        (n,),
    )
    return cur.fetchall()


async def probe(session, sem, cid, name, pc) -> dict:
    async with sem:
        await asyncio.sleep(REQUEST_DELAY)
        url = f"{API_BASE}?{urlencode({'q': name, 'code_postal': pc, 'per_page': 10})}"
        try:
            async with session.get(url) as r:
                if r.status != 200:
                    return {"verdict": f"http_{r.status}"}
                data = await r.json(content_type=None)
        except Exception as e:
            return {"verdict": f"error_{type(e).__name__}"}

    results = data.get("results", [])
    if not results:
        return {"verdict": "no_result", "name": name, "pc": pc}

    ours = norm_key(name)
    scored = []
    for r in results:
        cand = r.get("nom_complet") or r.get("nom_raison_sociale")
        k = norm_key(cand)
        scored.append((1.0 if k and k == ours else similarity(ours, k), r, cand))
    scored.sort(key=lambda t: -t[0])

    best_sim, best, best_name = scored[0]
    exact = [s for s in scored if s[0] == 1.0]

    if len(exact) == 1:
        verdict = "confident"
    elif len(exact) > 1:
        verdict = "ambiguous"
    elif best_sim >= PROBABLE_MIN:
        verdict = "probable" if sum(1 for s in scored if s[0] >= PROBABLE_MIN) == 1 else "ambiguous"
    else:
        verdict = "weak"

    return {"verdict": verdict, "name": name, "pc": pc, "sim": round(best_sim, 2),
            "matched_siren": best.get("siren"), "matched_name": best_name,
            "matched_naf": best.get("activite_principale"), "n_results": len(results)}


async def run(n: int) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            rows = sample_no_siren(cur, n)
            cur.execute("""
                SELECT count(*) FROM staging.companies co
                JOIN staging.sites s ON s.company_id = co.id
                WHERE co.siren IS NULL AND co.qualification_status='qualified_unverified'
                  AND co.duplicate_of_company_id IS NULL
                  AND coalesce(co.legal_name, co.trade_name) IS NOT NULL
                  AND btrim(s.postal_code) ~ '^[0-9]{5}$'
            """)
            eligible = cur.fetchone()[0]
    finally:
        conn.close()

    log.info("Eligible population (no SIREN, has name + postal): %s", f"{eligible:,}")
    log.info("Sampling %d...", len(rows))

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        connector=aiohttp.TCPConnector(limit=MAX_CONCURRENT + 2),
    ) as session:
        out = await asyncio.gather(*(probe(session, sem, c, nm, pc) for c, nm, pc in rows))

    buckets: dict[str, int] = {}
    for r in out:
        buckets[r["verdict"]] = buckets.get(r["verdict"], 0) + 1
    total = len(out)

    log.info("")
    log.info("%-12s %8s %8s", "VERDICT", "COUNT", "SHARE")
    log.info("%s", "-" * 32)
    for v, c in sorted(buckets.items(), key=lambda kv: -kv[1]):
        log.info("%-12s %8d %7.1f%%", v, c, 100 * c / total)
    log.info("%s", "-" * 32)

    conf = buckets.get("confident", 0)
    prob = buckets.get("probable", 0)
    log.info("")
    log.info("Auto-appliable (confident)     : %.1f%%  -> ~%s of %s companies",
             100 * conf / total, f"{round(eligible * conf / total):,}", f"{eligible:,}")
    log.info("Reviewable middle (probable)   : %.1f%%  -> ~%s",
             100 * prob / total, f"{round(eligible * prob / total):,}")
    hours = eligible / 5 / 3600
    log.info("Full-run cost at ~5 req/s      : ~%.1f hours", hours)

    for label in ("confident", "probable", "ambiguous", "weak"):
        ex = [r for r in out if r["verdict"] == label][:3]
        if ex:
            log.info("")
            log.info("  %s examples:", label)
            for r in ex:
                log.info("    ours=%-32s -> %s %s (sim=%s, n=%s)",
                         (r.get("name") or "")[:32], r.get("matched_siren"),
                         (r.get("matched_name") or "")[:34], r.get("sim"), r.get("n_results"))


def main() -> None:
    ap = argparse.ArgumentParser(description="M1-S4b SIREN recovery feasibility sample")
    ap.add_argument("--sample", type=int, default=500)
    args = ap.parse_args()
    asyncio.run(run(args.sample))


if __name__ == "__main__":
    main()
