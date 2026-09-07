"""
M3AG-S13 — SIRENE liveness: is each operator's SIRET still active?
===================================================================
The Agence Bio list is current, but a certified farm can have ceased since
its last certification. Before a call sheet ships, ask the registry.

One GET per 14-digit SIRET on recherche-entreprises.api.gouv.fr (the same
endpoint as m1_s4 / m2_s1, free, no key). Two states are recorded:
  etat_ul    legal unit      A = active, C = cessée
  etat_etab  establishment   A = active, F = fermé     (the SIRET itself)
plus `date_fermeture` and a note. A "Liquidateur" among the officers is
noted too (m2_s2 rule: the état can still read 'A' throughout a liquidation).

THE RULE (repo-wide): our failure is not evidence about the company. A
timeout, a 5xx or an empty answer is written as note=`non trouvé` with empty
états; downstream keeps such a row and labels it. And if more than 20 % of
the SIRETs come back not-found, the run aborts — that is our network, not
the registry.

Output: checkpoints/sirene_etat_<dept>.csv  (append + flush every 50 rows,
resumes by skipping SIRETs already present; --redo re-checks everything)

Usage:
    python scripts/m3ag_s13_sirene_etat.py --departement 63
    python scripts/m3ag_s13_sirene_etat.py --departement 63 --limit 20
"""

import argparse
import csv
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m3ag_lib import CHECK_DIR, read_csv, append_rows   # noqa: E402

API = "https://recherche-entreprises.api.gouv.fr/search"
FIELDS = ["row_id", "siret", "etat_ul", "etat_etab", "date_fermeture", "note", "checked_at"]
DELAY = 0.17            # ~6 req/s, under the API's 7 req/s per IP
FLUSH_EVERY = 50
RETRIES = 4
NOT_FOUND_CEILING = 0.20

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s13")


def fetch(siret: str) -> dict:
    """Registry answer for one SIRET. Never raises: failures become a note."""
    url = f"{API}?q={siret}&page=1&per_page=10"
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                time.sleep(2 ** attempt)
                continue
            return {"note": f"non trouvé (HTTP {e.code})"}
        except Exception as e:                       # timeout, DNS, reset
            if attempt < RETRIES:
                time.sleep(2 ** attempt)
                continue
            return {"note": f"non trouvé ({type(e).__name__})"}
    # exact-SIREN guard, as m1_s4: a mis-ranked hit must not speak for our SIRET
    unit = next((r for r in data.get("results", []) if r.get("siren") == siret[:9]), None)
    if unit is None:
        return {"note": "non trouvé (aucun résultat)"}
    etab = next((e for e in unit.get("matching_etablissements") or [] if e.get("siret") == siret), None)
    if etab is None and (unit.get("siege") or {}).get("siret") == siret:
        etab = unit["siege"]
    notes = []
    if any("liquidateur" in (d.get("qualite") or "").lower() for d in unit.get("dirigeants") or []):
        notes.append("liquidateur")
    if etab is None:
        notes.append("établissement non listé")
    return {
        "etat_ul": unit.get("etat_administratif") or "",
        "etat_etab": (etab or {}).get("etat_administratif") or "",
        "date_fermeture": (etab or {}).get("date_fermeture") or unit.get("date_fermeture") or "",
        "note": "; ".join(notes),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departement", default="63")
    ap.add_argument("--limit", type=int, default=0, help="only N SIRETs (pilot)")
    ap.add_argument("--redo", action="store_true", help="re-check SIRETs already in the checkpoint")
    args = ap.parse_args()
    dept = args.departement

    ops = read_csv(CHECK_DIR / f"operateurs_{dept}.csv", delim=",")
    if not ops:
        sys.exit(f"operateurs_{dept}.csv missing — run m3ag_s2_transform.py first.")
    out_path = CHECK_DIR / f"sirene_etat_{dept}.csv"
    done = set() if args.redo else {r["siret"] for r in read_csv(out_path)}

    targets, seen = [], set()
    for op in ops:
        s = (op.get("siret") or "").strip()
        if len(s) != 14 or not s.isdigit() or s in done or s in seen:
            continue
        seen.add(s)
        targets.append((s, op.get("numeroBio", "")))
    if args.limit:
        targets = targets[:args.limit]
    log.info(f"[{dept}] {len(ops)} operators, {len(targets)} SIRET(s) to check "
             f"({len(done)} already done, {sum(1 for o in ops if len((o.get('siret') or '').strip()) != 14)} without a 14-digit SIRET)")

    buf, stats, nf = [], Counter(), 0
    for i, (siret, nb) in enumerate(targets, 1):
        r = fetch(siret)
        if r.get("note", "").startswith("non trouvé"):
            nf += 1
        row = {"row_id": siret, "siret": siret,
               "etat_ul": r.get("etat_ul", ""), "etat_etab": r.get("etat_etab", ""),
               "date_fermeture": r.get("date_fermeture", ""), "note": r.get("note", ""),
               "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        stats[(row["etat_ul"], row["etat_etab"]) if row["etat_ul"] else row["note"]] += 1
        buf.append(row)
        if len(buf) >= FLUSH_EVERY:
            append_rows(out_path, FIELDS, buf)
            buf = []
            log.info(f"  {i}/{len(targets)} written  {dict(stats)}")
        # the sanity gate, checked as we go: > 20 % not found after a real
        # sample means OUR side is failing — stop rather than write more noise
        if i >= 50 and nf / i > NOT_FOUND_CEILING:
            append_rows(out_path, FIELDS, buf)
            sys.exit(f"ABORT: {nf}/{i} not found ({nf/i:.0%} > {NOT_FOUND_CEILING:.0%}). "
                     "Almost certainly our network, not the registry. Rows so far are "
                     "kept; re-run resumes.")
        time.sleep(DELAY)
    append_rows(out_path, FIELDS, buf)

    allrows = read_csv(out_path)
    c = Counter((r["etat_ul"], r["etat_etab"]) for r in allrows if r["etat_ul"])
    log.info("─" * 62)
    log.info(f"written={len(allrows)} -> {out_path}")
    for (ul, et), n in c.most_common():
        log.info(f"  unité {ul} / établissement {et or '-':<2}  {n:>5}")
    log.info(f"  non trouvé                        {sum(1 for r in allrows if not r['etat_ul']):>5}")
    log.info(f"  liquidateur noté                  {sum(1 for r in allrows if 'liquidateur' in r['note']):>5}")
    log.info(f"  CLOSED (unité C ou établissement F){sum(1 for r in allrows if r['etat_ul'] == 'C' or r['etat_etab'] == 'F'):>5}")


if __name__ == "__main__":
    main()
