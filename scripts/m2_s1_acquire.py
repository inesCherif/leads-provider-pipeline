"""
M2-S1 — Acquire boulangeries of département 13 from the public registry
=======================================================================
Sector 2 ("boulangerie") has NO client Excel: the companies themselves come
from the free registry API — this is the S9g design ("SIRENE bulk sourcing,
Sam's step 1") built for its intended first user.

    https://recherche-entreprises.api.gouv.fr/search
        ?activite_principale=10.71C,10.71B
        &departement=13
        &limite_matching_etablissements=100
        &per_page=25&page=N

Scope decided by Ines 2026-08-13: NAF 10.71C (boulangerie artisanale) +
10.71B (terminal de cuisson) — both run ovens, which is the energy-efficiency
angle. 47.24Z resellers excluded.

Output: one JSON line per legal unit (the FULL payload — dirigeants,
matching_etablissements, everything) appended to

    exports/boulangerie/checkpoints/api_raw.jsonl

with a sidecar progress file so a rerun resumes at the first unfinished page.
No DB connection anywhere — three data-loss incidents in this project came
from a connection held open across slow work; here there is none to lose.
Deduplication by SIREN happens at transform time (m2_s2), so re-fetching a
page after a resume is harmless.

Two traps this script exists to avoid (both measured on live API responses):
  * `departement=13` matches legal units with ANY établissement in dept 13 —
    the siège can be in another département entirely. The établissement rows
    are what we deliver, hence `limite_matching_etablissements=100`: the
    default truncates that array and would silently drop a chain's outlets.
  * The result set is capped at page*per_page = 10,000 by the API. We assert
    total_results is under it; if a wider scope ever trips this, split the
    query (e.g. by code_postal) instead of paginating past the cap.

Usage:
    python scripts/m2_s1_acquire.py               # full run (resumes if interrupted)
    python scripts/m2_s1_acquire.py --max-pages 2 # smoke test
    python scripts/m2_s1_acquire.py --fresh       # discard checkpoint, start over
"""

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
OUT_DIR      = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
RAW_PATH     = OUT_DIR / "api_raw.jsonl"
PROGRESS_PATH = OUT_DIR / "api_progress.json"

API_BASE = "https://recherche-entreprises.api.gouv.fr/search"
PARAMS = {
    # 10.71D added 2026-08-13 (Ines): patisseries share the oven/energy profile,
    # and Marseille's best-rated artisan bakers include 10.71D registrations.
    "activite_principale": "10.71C,10.71B,10.71D",
    "departement": "13",
    "limite_matching_etablissements": "100",
    "per_page": "25",
}
MAX_RESULTS_CAP = 10_000   # hard API limit on page*per_page
REQUEST_DELAY   = 0.4      # same pacing discipline as m1_s4_sirene_enrich.py
REQUEST_TIMEOUT = 15
RETRY_ATTEMPTS  = 4
RETRY_BACKOFF   = 3.0      # seconds, doubles per attempt

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s1")


def fetch_page(page: int) -> dict:
    """One GET with retry/backoff. 429 and 5xx retry; other 4xx are fatal
    (they mean the request itself is wrong, not the moment)."""
    qs = urllib.parse.urlencode({**PARAMS, "page": str(page)})
    url = f"{API_BASE}?{qs}"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "leads-provider-m2s1"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"HTTP {e.code} on page {page}, retrying in {wait:.0f}s "
                            f"(attempt {attempt}/{RETRY_ATTEMPTS})")
                time.sleep(wait)
                continue
            sys.exit(f"HTTP {e.code} on page {page} — request is malformed, aborting: {url}")
        except Exception as exc:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning(f"{type(exc).__name__} on page {page}: {exc} — retrying in {wait:.0f}s")
            time.sleep(wait)
    sys.exit(f"Page {page} failed after {RETRY_ATTEMPTS} attempts — rerun to resume from checkpoint.")


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        prog = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        if prog.get("params") != PARAMS:
            sys.exit("Checkpoint was written with DIFFERENT query params. "
                     "Use --fresh to discard it, or restore the old params.")
        return prog
    return {"params": PARAMS, "last_completed_page": 0,
            "total_results": None, "total_pages": None}


def save_progress(prog: dict) -> None:
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prog, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N pages (smoke test)")
    ap.add_argument("--fresh", action="store_true", help="discard checkpoint and JSONL, start over")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        RAW_PATH.unlink(missing_ok=True)
        PROGRESS_PATH.unlink(missing_ok=True)
        log.info("Checkpoint discarded (--fresh).")

    prog = load_progress()
    page = prog["last_completed_page"] + 1
    if prog["total_pages"] and page > prog["total_pages"]:
        log.info(f"Nothing to do: all {prog['total_pages']} pages already fetched "
                 f"({prog['total_results']} legal units in {RAW_PATH}).")
        return
    if page > 1:
        log.info(f"Resuming at page {page} (checkpoint found).")

    pages_done = 0
    written = 0
    truncation_suspects = []

    while True:
        data = fetch_page(page)
        total = data["total_results"]
        total_pages = data["total_pages"]

        if total >= MAX_RESULTS_CAP:
            sys.exit(f"total_results={total} >= {MAX_RESULTS_CAP}: the API caps pagination "
                     "there. Split the query (e.g. per code_postal) — do not ship a "
                     "silently truncated list.")
        if prog["total_results"] is not None and prog["total_results"] != total:
            log.warning(f"total_results changed mid-run: {prog['total_results']} -> {total} "
                        "(registry updated underneath us; the transform dedupes by SIREN).")
        prog["total_results"], prog["total_pages"] = total, total_pages

        results = data.get("results", [])
        # Append + flush page-by-page: a crash loses at most one page, and the
        # log reports written= (rows on disk), never collected= (rows in memory).
        with RAW_PATH.open("a", encoding="utf-8") as f:
            for r in results:
                if len(r.get("matching_etablissements", [])) >= 100:
                    truncation_suspects.append(r.get("siren"))
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
        written += len(results)
        prog["last_completed_page"] = page
        save_progress(prog)
        pages_done += 1
        log.info(f"page {page}/{total_pages}: written={len(results)} (run total {written}, "
                 f"expected overall {total})")

        if page >= total_pages:
            break
        if args.max_pages and pages_done >= args.max_pages:
            log.info(f"--max-pages {args.max_pages} reached, stopping (resume by rerunning).")
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    with RAW_PATH.open(encoding="utf-8") as f:
        lines = sum(1 for _ in f)
    log.info(f"JSONL now holds {lines} lines for total_results={prog['total_results']} "
             f"(>= is fine before dedup; < means pages are missing — rerun to resume).")
    if truncation_suspects:
        log.warning(f"{len(truncation_suspects)} legal unit(s) hit the 100-établissement "
                    f"cap and may be truncated: {truncation_suspects[:10]}")
    if prog["last_completed_page"] >= (prog["total_pages"] or 0):
        log.info("DONE — next step: python scripts/m2_s2_transform.py")


if __name__ == "__main__":
    main()
