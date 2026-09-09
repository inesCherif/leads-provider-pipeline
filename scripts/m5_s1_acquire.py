"""
M5-S1 — Acquire gîtes + campings of départements 03 and 63 from the registry
============================================================================
Sector 5 has no client Excel: the population comes from the free registry
API, exactly as M2 did for bakeries (this file is `m2_s1_acquire.py` with
the département and NAF scope on the command line and one checkpoint per
département, so M2's own checkpoint — whose params are hashed — is untouched).

    https://recherche-entreprises.api.gouv.fr/search
        ?activite_principale=55.20Z,55.30Z
        &departement=<dept>
        &limite_matching_etablissements=100
        &per_page=25&page=N

Scope (Ines 2026-09-09): NAF 55.30Z (terrains de camping) + 55.20Z
(hébergement touristique de courte durée: gîtes, meublés, chambres d'hôtes,
résidences, villages de vacances). Phase 1 = private operators; public ones
are flagged at transform time, never dropped here. Hotels (55.10Z) are out.

Output, per département: one JSON line per legal unit (FULL payload —
dirigeants, matching_etablissements, siege, nature_juridique…) appended to

    exports/hebergement/checkpoints/api_raw_<dept>.jsonl

with a sidecar progress file so a rerun resumes at the first unfinished page.
No DB connection anywhere. Dedup by SIREN happens in m5_s2.

Two traps inherited from M2 (measured on live responses):
  * `departement=` matches legal units with ANY établissement in the dept —
    the siège can be elsewhere. Rows are built from matching_etablissements,
    hence `limite_matching_etablissements=100`.
  * The result set is capped at page*per_page = 10,000. Measured today:
    03 = 87 + 789, 63 = 168 + 1,513 legal units — far under it.

Usage:
    python scripts/m5_s1_acquire.py --departements 03,63 --max-pages 2   # smoke
    python scripts/m5_s1_acquire.py --departements 03,63                 # full (resumes)
    python scripts/m5_s1_acquire.py --departements 63 --fresh            # discard 63's checkpoint
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
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m5_lib import CHECK_DIR, DEPARTEMENTS, NAF_SCOPE   # noqa: E402

API_BASE = "https://recherche-entreprises.api.gouv.fr/search"
MAX_RESULTS_CAP = 10_000
REQUEST_DELAY   = 0.4
REQUEST_TIMEOUT = 15
RETRY_ATTEMPTS  = 4
RETRY_BACKOFF   = 3.0

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m5_s1")


def params_for(dept: str, naf: str) -> dict:
    return {
        "activite_principale": naf,
        "departement": dept,
        "limite_matching_etablissements": "100",
        "per_page": "25",
    }


def fetch_page(params: dict, page: int) -> dict:
    """One GET with retry/backoff. 429 and 5xx retry; other 4xx are fatal."""
    qs = urllib.parse.urlencode({**params, "page": str(page)})
    url = f"{API_BASE}?{qs}"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "leads-provider-m5s1"})
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


def load_progress(progress_path: Path, params: dict) -> dict:
    if progress_path.exists():
        prog = json.loads(progress_path.read_text(encoding="utf-8"))
        if prog.get("params") != params:
            sys.exit(f"{progress_path.name} was written with DIFFERENT query params. "
                     "Use --fresh to discard it, or restore the old params.")
        return prog
    return {"params": params, "last_completed_page": 0,
            "total_results": None, "total_pages": None}


def save_progress(progress_path: Path, prog: dict) -> None:
    tmp = progress_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prog, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(progress_path)


def run_dept(dept: str, naf: str, max_pages: int | None, fresh: bool) -> None:
    raw_path = CHECK_DIR / f"api_raw_{dept}.jsonl"
    progress_path = CHECK_DIR / f"api_progress_{dept}.json"
    params = params_for(dept, naf)
    if fresh:
        raw_path.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
        log.info(f"[{dept}] checkpoint discarded (--fresh).")

    prog = load_progress(progress_path, params)
    page = prog["last_completed_page"] + 1
    if prog["total_pages"] and page > prog["total_pages"]:
        log.info(f"[{dept}] nothing to do: all {prog['total_pages']} pages already fetched "
                 f"({prog['total_results']} legal units in {raw_path.name}).")
        return
    if page > 1:
        log.info(f"[{dept}] resuming at page {page} (checkpoint found).")

    pages_done = written = 0
    truncation_suspects = []
    while True:
        data = fetch_page(params, page)
        total, total_pages = data["total_results"], data["total_pages"]
        if total >= MAX_RESULTS_CAP:
            sys.exit(f"[{dept}] total_results={total} >= {MAX_RESULTS_CAP}: the API caps "
                     "pagination there. Split the query (per NAF or code_postal).")
        if prog["total_results"] is not None and prog["total_results"] != total:
            log.warning(f"[{dept}] total_results changed mid-run: {prog['total_results']} -> {total} "
                        "(registry updated underneath us; m5_s2 dedupes by SIREN).")
        prog["total_results"], prog["total_pages"] = total, total_pages

        results = data.get("results", [])
        # Append + flush page-by-page: a crash loses at most one page, and the
        # log reports written= (rows on disk), never collected=.
        with raw_path.open("a", encoding="utf-8") as f:
            for r in results:
                if len(r.get("matching_etablissements", [])) >= 100:
                    truncation_suspects.append(r.get("siren"))
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
        written += len(results)
        prog["last_completed_page"] = page
        save_progress(progress_path, prog)
        pages_done += 1
        log.info(f"[{dept}] page {page}/{total_pages}: written={len(results)} "
                 f"(run total {written}, expected overall {total})")

        if page >= total_pages:
            break
        if max_pages and pages_done >= max_pages:
            log.info(f"[{dept}] --max-pages {max_pages} reached, stopping (resume by rerunning).")
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    with raw_path.open(encoding="utf-8") as f:
        lines = sum(1 for _ in f)
    log.info(f"[{dept}] JSONL now holds {lines} lines for total_results={prog['total_results']} "
             f"(>= is fine before dedup; < means pages are missing — rerun to resume).")
    if truncation_suspects:
        log.warning(f"[{dept}] {len(truncation_suspects)} legal unit(s) hit the 100-établissement "
                    f"cap and may be truncated: {truncation_suspects[:10]}")
    if prog["last_completed_page"] >= (prog["total_pages"] or 0):
        log.info(f"[{dept}] DONE — next: python scripts/m5_s2_transform.py --departements {dept}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default=",".join(DEPARTEMENTS))
    ap.add_argument("--naf", default=",".join(sorted(NAF_SCOPE)),
                    help="comma-separated activite_principale codes")
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N pages per dept (smoke test)")
    ap.add_argument("--fresh", action="store_true", help="discard the dept's checkpoint and JSONL")
    args = ap.parse_args()

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    for dept in [d.strip() for d in args.departements.split(",") if d.strip()]:
        run_dept(dept, args.naf, args.max_pages, args.fresh)


if __name__ == "__main__":
    main()
