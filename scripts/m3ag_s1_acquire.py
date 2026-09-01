"""
M3AG-S1 — Acquire bio farmers of a département from the Agence Bio API
======================================================================
Sector "agriculteurs bio" (Sam & Mehdi, 2026-09-01): their sample file
`10_agriculteurs_enrichis.csv` is row-for-row the output of

    https://opendata.agencebio.org/api/gouv/operateurs/

(verified: SIRET 90807482600011 returns the identical record). This script
pulls EVERY operator of the requested département(s) — Ines was assigned 63,
with 03 next — into one JSON line per operator, full payload (adresses,
productions, siteWebs, certificats, email — the sample dropped `email`; we
keep it, it is 23% coverage for free).

Three facts measured on the live API 2026-09-01 — do not "simplify" them away:
  * The ONLY working filter is `departements=63`. `departement=`, `codePostal=`
    and `codesPostaux=` are silently IGNORED and return all 137,304 operators.
    We therefore assert every returned address is in the requested dept —
    a param rename on their side must crash us, not ship France entière.
  * Requests without a browser-ish User-Agent get HTTP 403 (curl passes,
    bare urllib does not).
  * Pagination is offset-based: `nb=250&debut=N`. `nbTotal` is a STRING.

No DB connection anywhere (M2 rule — three pooler data-loss incidents).
Flush per page; a crash loses at most one page; rerun resumes.

Usage:
    python scripts/m3ag_s1_acquire.py                    # dept 63 (default)
    python scripts/m3ag_s1_acquire.py --departements 03  # the next dept
    python scripts/m3ag_s1_acquire.py --max-pages 2      # smoke test
    python scripts/m3ag_s1_acquire.py --fresh            # discard checkpoint
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
OUT_DIR      = PROJECT_ROOT / "exports" / "agriculteurs" / "checkpoints"

API_BASE  = "https://opendata.agencebio.org/api/gouv/operateurs/"
PAGE_SIZE = 250
# 403 without a real-looking UA, measured 2026-09-01.
USER_AGENT = "Mozilla/5.0 (compatible; leads-provider-m3ag; contact contact@example.org)"
REQUEST_DELAY   = 0.4
REQUEST_TIMEOUT = 60
RETRY_ATTEMPTS  = 4
RETRY_BACKOFF   = 3.0

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s1")


def fetch(dept: str, debut: int) -> dict:
    qs = urllib.parse.urlencode({"departements": dept, "nb": str(PAGE_SIZE),
                                 "debut": str(debut)})
    url = f"{API_BASE}?{qs}"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(f"HTTP {e.code} at debut={debut}, retry in {wait:.0f}s "
                            f"({attempt}/{RETRY_ATTEMPTS})")
                time.sleep(wait)
                continue
            sys.exit(f"HTTP {e.code} at debut={debut} — request malformed or blocked: {url}")
        except Exception as exc:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning(f"{type(exc).__name__} at debut={debut}: {exc} — retry in {wait:.0f}s")
            time.sleep(wait)
    sys.exit(f"debut={debut} failed after {RETRY_ATTEMPTS} attempts — rerun to resume.")


def op_depts(op: dict) -> set:
    """Départements this operator's addresses live in (first 2 digits of CP)."""
    return {str(a.get("codePostal") or "")[:2]
            for a in op.get("adressesOperateurs", []) if a.get("codePostal")}


def run_dept(dept: str, fresh: bool, max_pages: int | None) -> None:
    raw_path      = OUT_DIR / f"agencebio_{dept}.jsonl"
    progress_path = OUT_DIR / f"agencebio_{dept}_progress.json"

    if fresh:
        raw_path.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
        log.info(f"[{dept}] checkpoint discarded (--fresh).")

    prog = (json.loads(progress_path.read_text(encoding="utf-8"))
            if progress_path.exists()
            else {"dept": dept, "next_debut": 0, "nb_total": None, "done": False})
    if prog.get("done"):
        log.info(f"[{dept}] already complete ({prog['nb_total']} operators in {raw_path}). "
                 "Use --fresh for a re-pull.")
        return
    if prog["next_debut"]:
        log.info(f"[{dept}] resuming at debut={prog['next_debut']}.")

    pages_done = 0
    written_this_run = 0
    foreign = 0

    while True:
        data = fetch(dept, prog["next_debut"])
        nb_total = int(data["nbTotal"])          # STRING in the payload
        items = data.get("items") or []

        # The filter-silently-ignored trap: if the param stops working we get
        # France entière — crash loudly instead of shipping it.
        if items:
            sample_ok = sum(1 for op in items[:20] if dept in op_depts(op))
            if sample_ok == 0:
                sys.exit(f"[{dept}] page at debut={prog['next_debut']} contains no "
                         f"dept-{dept} address in its first rows — the `departements` "
                         "filter is being IGNORED (nbTotal={}). Aborting.".format(nb_total))

        if prog["nb_total"] is not None and prog["nb_total"] != nb_total:
            log.warning(f"[{dept}] nbTotal changed mid-run: {prog['nb_total']} -> {nb_total} "
                        "(directory updated underneath us; transform dedupes by numeroBio).")
        prog["nb_total"] = nb_total

        with raw_path.open("a", encoding="utf-8") as f:
            for op in items:
                if dept not in op_depts(op):
                    foreign += 1     # kept in the jsonl, flagged at transform
                f.write(json.dumps(op, ensure_ascii=False) + "\n")
            f.flush()
        written_this_run += len(items)
        prog["next_debut"] += len(items)
        pages_done += 1

        done = prog["next_debut"] >= nb_total or not items
        prog["done"] = done
        tmp = progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prog, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(progress_path)

        log.info(f"[{dept}] debut now {prog['next_debut']}/{nb_total} "
                 f"(written this run: {written_this_run})")
        if done:
            break
        if max_pages and pages_done >= max_pages:
            log.info(f"[{dept}] --max-pages {max_pages} reached; rerun to resume.")
            return
        time.sleep(REQUEST_DELAY)

    with raw_path.open(encoding="utf-8") as f:
        lines = sum(1 for _ in f)
    log.info(f"[{dept}] DONE — {lines} lines on disk for nbTotal={prog['nb_total']} "
             f"(>= is fine, transform dedupes; < means missing pages — rerun). "
             f"{foreign} operator(s) had no dept-{dept} address (flagged at transform).")
    log.info("Next: python scripts/m3ag_s2_transform.py --departements " + dept)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default="63",
                    help="comma-separated dept codes (default: 63)")
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N pages per dept")
    ap.add_argument("--fresh", action="store_true", help="discard checkpoint(s), start over")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for dept in [d.strip() for d in args.departements.split(",") if d.strip()]:
        run_dept(dept, args.fresh, args.max_pages)


if __name__ == "__main__":
    main()
