"""
M3AG-S2 — Transform Agence Bio raw JSONL into one CSV row per operator
======================================================================
Input : exports/agriculteurs/checkpoints/agencebio_<dept>.jsonl  (m3ag_s1)
Output: exports/agriculteurs/checkpoints/operateurs_<dept>.csv

Column layout = Sam's sample file `10_agriculteurs_enrichis.csv` EXACTLY
(raisonSociale ... lon), so their loader keeps working; the places_*/final
columns are added later by the enrichment merge. Extra columns the sample
dropped are APPENDED at the end — most importantly `email` (23% coverage,
free, sitting in the API payload the sample was built from).

Rules:
  * Dedup by numeroBio (a resumed acquisition may re-fetch a page).
  * Address: the ACTIVE address in the requested département, preferring the
    one typed "Siège social"; an operator with no address in the dept is
    kept but flagged (flag_hors_dept=1) — never silently dropped.
  * flag_hors_agri=1 when NAF prefix not in {01,02,03,10,11} AND the
    operator has no "Production" activity — Sam's sample was unfiltered
    (it contains an ADAPEI and associations), so we flag, not filter.
    Ines's decision 2026-09-01.
  * Phones are normalised to 0X XX XX XX XX via m2lib_contact.norm_phone_fr
    (the sample shipped floats like `673447105.0` — a leading-zero loss).

Usage:
    python scripts/m3ag_s2_transform.py --departements 63,03
"""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone  # noqa: E402
from france_lib import in_dept as cp_in_dept  # noqa: E402

CHECK_DIR = PROJECT_ROOT / "exports" / "agriculteurs" / "checkpoints"

AGRI_NAF_PREFIXES = {"01", "02", "03", "10", "11"}

# Sam's sample columns, in their order, then our appended extras.
COLUMNS = ["raisonSociale", "siret", "gerant", "telephone", "telephoneCommerciale",
           "codeNAF", "siteWebs", "categories", "productions",
           "adresse", "codePostal", "ville", "lat", "lon",
           # appended (not in the sample):
           "email", "numeroBio", "activites", "flag_hors_agri", "flag_hors_dept"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s2")


def pick_address(op: dict, dept: str) -> tuple[dict | None, bool]:
    """(address, in_dept). Active + in-dept preferred; 'Siège social' first."""
    addrs = [a for a in op.get("adressesOperateurs", []) if a.get("active", True)]
    in_dept = [a for a in addrs if cp_in_dept(str(a.get("codePostal") or ""), dept)]
    pool, flag = (in_dept, False) if in_dept else (addrs, True)
    if not pool:
        return None, True
    siege = [a for a in pool if "Siège social" in (a.get("typeAdresseOperateurs") or [])]
    return (siege[0] if siege else pool[0]), flag


def phone(v) -> str:
    if not v:
        return ""
    s = str(v).strip().removesuffix(".0")
    if len(s) == 9 and s.isdigit():   # float-mangled leading zero (Sam's sample bug)
        s = "0" + s
    return normalize_fr_phone(s) or s


def row_for(op: dict, dept: str) -> dict:
    addr, hors_dept = pick_address(op, dept)
    naf = (op.get("codeNAF") or "").strip()
    activites = [a.get("nom", "") for a in op.get("activites", [])]
    hors_agri = int(naf[:2] not in AGRI_NAF_PREFIXES and "Production" not in activites)
    return {
        "raisonSociale": (op.get("raisonSociale") or op.get("denominationcourante") or "").strip(),
        "siret": (op.get("siret") or "").strip(),
        "gerant": (op.get("gerant") or "").strip(),
        "telephone": phone(op.get("telephone")),
        "telephoneCommerciale": phone(op.get("telephoneCommerciale")),
        "codeNAF": naf,
        "siteWebs": "; ".join(sorted({(s.get("url") or "").strip()
                                      for s in op.get("siteWebs", []) if s.get("url")})),
        "categories": "; ".join(c.get("nom", "") for c in op.get("categories", [])),
        "productions": "; ".join(p.get("nom", "") for p in op.get("productions", [])),
        "adresse": (addr or {}).get("lieu", ""),
        "codePostal": str((addr or {}).get("codePostal") or ""),
        "ville": (addr or {}).get("ville", ""),
        "lat": (addr or {}).get("lat", ""),
        "lon": (addr or {}).get("long", ""),
        "email": (op.get("email") or "").strip(),
        "numeroBio": op.get("numeroBio", ""),
        "activites": "; ".join(activites),
        "flag_hors_agri": hors_agri,
        "flag_hors_dept": int(hors_dept),
    }


def run_dept(dept: str) -> None:
    raw = CHECK_DIR / f"agencebio_{dept}.jsonl"
    out = CHECK_DIR / f"operateurs_{dept}.csv"
    if not raw.exists():
        sys.exit(f"[{dept}] {raw} missing — run m3ag_s1_acquire.py first.")

    seen: dict = {}
    with raw.open(encoding="utf-8") as f:
        for line in f:
            op = json.loads(line)
            key = op.get("numeroBio") or op.get("id")
            seen[key] = op          # last occurrence wins (freshest fetch)

    rows = [row_for(op, dept) for op in seen.values()]
    rows.sort(key=lambda r: (r["codePostal"], r["raisonSociale"]))

    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    stats = {
        "telephone": sum(1 for r in rows if r["telephone"] or r["telephoneCommerciale"]),
        "email": sum(1 for r in rows if r["email"]),
        "siteWebs": sum(1 for r in rows if r["siteWebs"]),
        "gerant": sum(1 for r in rows if r["gerant"]),
        "siret": sum(1 for r in rows if r["siret"]),
        "geo": sum(1 for r in rows if r["lat"] and r["lon"]),
        "hors_agri": sum(r["flag_hors_agri"] for r in rows),
        "hors_dept": sum(r["flag_hors_dept"] for r in rows),
    }
    log.info(f"[{dept}] {n} operators -> {out.name}")
    for k, v in stats.items():
        log.info(f"[{dept}]   {k:<10} {v:>5}  ({v/n:.0%})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default="63")
    args = ap.parse_args()
    for dept in [d.strip() for d in args.departements.split(",") if d.strip()]:
        run_dept(dept)


if __name__ == "__main__":
    main()
