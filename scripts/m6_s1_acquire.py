"""
M6-S1 — Acquire the ÉLEVEURS (livestock farmers) of départements 03 and 63 from the registry
============================================================================================
Sector 6 has no fresh client Excel for this cut: the population comes from
the free registry API, exactly as M2 (bakeries) and M5 (gîtes) did. This
file is `m5_s1_acquire.py` with ONE QUERY PER NAF CODE per département and
one progress sidecar per (dept, NAF), so that no single query can approach
the API's 10,000-result cap (page*per_page). All NAF codes append to one
JSONL per dept; m6_s2 dedupes by SIREN.

    https://recherche-entreprises.api.gouv.fr/search
        ?activite_principale=<one NAF>
        &departement=<dept>
        &limite_matching_etablissements=100
        &per_page=25&page=N

Scope (Ines 2026-09-09): all livestock — 01.41Z 01.42Z 01.43Z 01.44Z
01.45Z 01.46Z 01.47Z 01.49Z 01.50Z (m6_lib.NAF_SCOPE). Équins and "autres
animaux" included and tagged at transform time.

Output, per département: one JSON line per legal unit (FULL payload —
dirigeants, matching_etablissements, siege, nature_juridique…) appended to

    exports/eleveurs/checkpoints/api_raw_<dept>.jsonl

with a sidecar progress file per (dept, NAF) so a rerun resumes at the
first unfinished page. No DB connection anywhere.

Two traps inherited from M2 (measured on live responses):
  * `departement=` matches legal units with ANY établissement in the dept —
    the siège can be elsewhere. Rows are built from matching_etablissements,
    hence `limite_matching_etablissements=100`.
  * The result set is capped at page*per_page = 10,000 — hence one query
    per NAF code; the script still hard-aborts if a single code reaches it.

Usage:
    python scripts/m6_s1_acquire.py --departements 03,63 --max-pages 1    # smoke
    python scripts/m6_s1_acquire.py --departements 03,63                  # full (resumes)
    python scripts/m6_s1_acquire.py --departements 63 --fresh             # discard 63's checkpoint
"""

import argparse
import csv
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
from m6_lib import CHECK_DIR, DEPARTEMENTS, NAF_SCOPE   # noqa: E402

API_BASE = "https://recherche-entreprises.api.gouv.fr/search"
MAX_RESULTS_CAP = 10_000
REQUEST_DELAY   = 0.7      # 0.4 hit HTTP 429 four times in a row (measured on M7, 2026-09-11)
REQUEST_TIMEOUT = 15
RETRY_ATTEMPTS  = 6
RETRY_BACKOFF   = 3.0
CAPPED_PATH     = CHECK_DIR / "capped_cells.csv"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m6_s1")


def params_for(dept: str, naf: str, active_only: bool = False) -> dict:
    p = {
        "activite_principale": naf,
        "departement": dept,
        "limite_matching_etablissements": "100",
        "per_page": "25",
    }
    if active_only:
        # A CESSÉE legal unit has no active établissement, and m6_s2 drops
        # closed établissements anyway — same population, ~1/3 fewer pages.
        p["etat_administratif"] = "A"
    return p


def note_capped(dept: str, naf: str, total: int) -> None:
    """A (dept, NAF) cell over the API's 10,000-result pagination cap: record
    it and let the run continue, instead of aborting the other 95 départements."""
    new = not CAPPED_PATH.exists()
    with CAPPED_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        if new:
            w.writerow(["dept", "naf", "total_results", "note"])
        w.writerow([dept, naf, total, "over the 10,000 pagination cap — re-pull split per code_postal"])
    log.error(f"[{dept} {naf}] total_results={total} >= {MAX_RESULTS_CAP} — SKIPPED, "
              f"logged in {CAPPED_PATH.name}. This cell needs a per-code_postal split.")


def fetch_page(params: dict, page: int) -> dict:
    """One GET with retry/backoff. 429 and 5xx retry; other 4xx are fatal."""
    qs = urllib.parse.urlencode({**params, "page": str(page)})
    url = f"{API_BASE}?{qs}"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "leads-provider-m6s1"})
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


def run_dept(dept: str, naf: str, max_pages: int | None, fresh: bool,
             active_only: bool = False) -> None:
    raw_path = CHECK_DIR / f"api_raw_{dept}.jsonl"
    progress_path = CHECK_DIR / f"api_progress_{dept}_{naf.replace('.', '')}.json"
    params = params_for(dept, naf, active_only)
    if fresh:
        raw_path.unlink(missing_ok=True)
        for p in CHECK_DIR.glob(f"api_progress_{dept}_*.json"):
            p.unlink()
        log.info(f"[{dept}] checkpoint discarded (--fresh).")

    prog = load_progress(progress_path, params)
    page = prog["last_completed_page"] + 1
    if prog["total_pages"] and page > prog["total_pages"]:
        log.info(f"[{dept} {naf}] nothing to do: all {prog['total_pages']} pages already fetched "
                 f"({prog['total_results']} legal units in {raw_path.name}).")
        return
    if page > 1:
        log.info(f"[{dept} {naf}] resuming at page {page} (checkpoint found).")

    pages_done = written = 0
    truncation_suspects = []
    while True:
        data = fetch_page(params, page)
        total, total_pages = data["total_results"], data["total_pages"]
        if total >= MAX_RESULTS_CAP:
            note_capped(dept, naf, total)
            return
        if prog["total_results"] is not None and prog["total_results"] != total:
            log.warning(f"[{dept}] total_results changed mid-run: {prog['total_results']} -> {total} "
                        "(registry updated underneath us; m6_s2 dedupes by SIREN).")
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
        log.info(f"[{dept} {naf}] page {page}/{total_pages}: written={len(results)} "
                 f"(run total {written}, expected overall {total})")

        if page >= total_pages:
            break
        if max_pages and pages_done >= max_pages:
            log.info(f"[{dept} {naf}] --max-pages {max_pages} reached, stopping (resume by rerunning).")
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
        log.info(f"[{dept} {naf}] DONE ({prog['total_results']} legal units).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default=",".join(DEPARTEMENTS))
    ap.add_argument("--naf", default=",".join(sorted(NAF_SCOPE)),
                    help="comma-separated activite_principale codes")
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N pages per dept (smoke test)")
    ap.add_argument("--fresh", action="store_true", help="discard the dept's checkpoint and JSONL")
    ap.add_argument("--active-only", action="store_true",
                    help="add etat_administratif=A (same population, ~1/3 fewer pages). "
                         "Changes the query params, so a dept already pulled without it needs --fresh.")
    ap.add_argument("--skip-naf", default="",
                    help="comma-separated NAF codes NOT to pull, e.g. 01.46Z (porcins — "
                         "Ines's principle; m7_s9 drops them from the deliverable anyway)")
    args = ap.parse_args()

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    skip = {n.strip() for n in args.skip_naf.split(",") if n.strip()}
    nafs = [n.strip() for n in args.naf.split(",") if n.strip() and n.strip() not in skip]
    if skip:
        log.info(f"skipping NAF {sorted(skip)} — not pulled at all")
    for dept in [d.strip() for d in args.departements.split(",") if d.strip()]:
        for i, naf in enumerate(nafs):
            run_dept(dept, naf, args.max_pages, args.fresh and i == 0, args.active_only)
        log.info(f"[{dept}] all NAF codes done — next: python scripts/m6_s2_transform.py --departements {dept}")


if __name__ == "__main__":
    main()
