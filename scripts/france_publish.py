"""
FRANCE-PUBLISH — put a REBUILT version of some départements into the delivery tree
==================================================================================
`france_run.py` builds and delivers v1. When a département gets an enrichment
pass afterwards (PACA, 2026-09-20: site crawl, bienvenue-à-la-ferme, OSM,
directories, Pages Jaunes), it is rebuilt BY HAND as v2 —

    m6_s8 -> m6_s9 --version v2 -> m6_s12 --version v2 --baseline v1 --strict
    m7_s8 -> m7_s9 --version v2 --with-eleveurs --eleveurs-version v2
          -> m7_s12 --version v2 --baseline v1 --strict

— and this script then copies the gated trio (xlsx, csv, _sans_siret.csv) into
`exports/france_agriculture/<Région>/` and REMOVES the older versions of the
same département: two versions of one département side by side is exactly the
confusion `france_copy_v2.py` was written to avoid (that script stays as the
record of the 03 / 63 v3 principle fix; this one is its general form).

It refuses a département whose xlsx is missing, and it never runs a gate: run
the two `_check --strict` first, a file is published only because YOU saw
"0 failed". Versions stay single-digit (france_verify and the recap take
`sorted(glob)[-1]`, which is lexical: v10 < v2).

Writes `france_status_<tag>.csv` (newest status file wins in the recap), then:

    python scripts/france_verify.py --strict
    python scripts/france_run.py --recap

Usage:
    python scripts/france_publish.py --departements Provence-Alpes-Cote-d-Azur --version v2 --dry-run
    python scripts/france_publish.py --departements Provence-Alpes-Cote-d-Azur --version v2
    python scripts/france_publish.py --departements 84,13 --version v2 --tag paca
"""

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import france_run as fr                                     # noqa: E402
from france_lib import parse_departements, region_dir       # noqa: E402

SUFFIXES = (".xlsx", ".csv", "_sans_siret.csv")


def main() -> None:
    ap = argparse.ArgumentParser(description="publish a rebuilt version into exports/france_agriculture/")
    ap.add_argument("--departements", required=True, help="a list, a région name, or all")
    ap.add_argument("--version", required=True, help="v2, v3… (single digit)")
    ap.add_argument("--tag", default="", help="status file name: france_status_<tag>.csv (default: the version)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not re.fullmatch(r"v[2-9]", args.version):
        sys.exit("--version must be v2…v9: the delivery tree sorts versions lexically (v10 < v2)")
    depts = parse_departements(args.departements)
    status_path = fr.OUT_ROOT / f"france_status_{args.tag or args.version}.csv"

    missing = [d for d in depts if not (fr.M7_OUT / f"producteurs_{d}_{args.version}.xlsx").exists()]
    if missing:
        sys.exit(f"not built yet as {args.version}: {', '.join(missing)} — nothing published")

    rows = {}
    for dept in depts:
        dest = fr.OUT_ROOT / region_dir(dept)
        stale = [p for p in dest.glob(f"producteurs_{dept}_v*")
                 if not p.name.startswith(f"producteurs_{dept}_{args.version}")] if dest.exists() else []
        if args.dry_run:
            fr.log.info(f"[{dept}] would copy the {args.version} trio to {dest.name}/ and remove "
                        f"{[p.name for p in stale] or 'nothing'}")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        for p in stale:
            p.unlink()
            fr.log.info(f"[{dept}] removed superseded {p.name}")
        for suffix in SUFFIXES:
            p = fr.M7_OUT / f"producteurs_{dept}_{args.version}{suffix}"
            if p.exists():
                shutil.copy2(p, dest / p.name)
        counts = fr.read_xlsx_counts(dept)            # reads the DELIVERED file back
        rows[dept] = {"dept": dept, "region": region_dir(dept), "status": "ok", "failed_step": "",
                      "gate": f"0 failed ({args.version}, enrichment pass)",
                      "started": "", "finished": date.today().isoformat(), "minutes": "", **counts}
        fr.log.info(f"[{dept}] {args.version} -> {dest.name}/  rows={counts.get('rows')} "
                    f"tel={counts.get('phones')} mail={counts.get('emails')} joign={counts.get('joignables')}")
    if rows:
        fr.save_status(rows, status_path)
        fr.log.info(f"status -> {status_path.name}   next: france_verify.py --strict, france_run.py --recap")


if __name__ == "__main__":
    main()
