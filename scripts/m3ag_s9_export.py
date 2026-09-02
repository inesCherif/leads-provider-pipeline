"""
M3AG-S9 — Merge enrichment into Sam's column layout and write the deliverable
=============================================================================
Input : checkpoints/operateurs_<dept>.csv   (m3ag_s2 — the API baseline)
        checkpoints/matched_<dept>.csv      (m3ag_s8 — accepted matches only)
Output: exports/agriculteurs/agriculteurs_<dept>_<version>.xlsx  and .csv

Column contract = Sam's sample `10_agriculteurs_enrichis.csv`, EXACT order,
so whatever loads their file loads ours. places_* stay empty until a Places
run happens; PJ enrichment gets its own appended columns instead of being
disguised as Places output. `telephone_final` prefers the operator's OWN
declaration to Agence Bio (owner-declared beats directory), then the PJ
match — provenance is recorded in `source_telephone`, never implied.

Usage:
    python scripts/m3ag_s9_export.py --departement 63 --version v1
"""

import argparse
import csv
import logging
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m1_s8_export import ILLEGAL_XML          # noqa: E402  (the one reusable bit)

CHECK_DIR = PROJECT_ROOT / "exports" / "agriculteurs" / "checkpoints"
OUT_DIR   = PROJECT_ROOT / "exports" / "agriculteurs"

COLUMNS = ["raisonSociale", "siret", "gerant", "telephone", "telephoneCommerciale",
           "codeNAF", "siteWebs", "categories", "productions",
           "adresse", "codePostal", "ville", "lat", "lon",
           "places_phone", "places_website", "places_name", "error",
           "telephone_final", "website_final",
           # appended — not in Sam's sample:
           "email", "numeroBio", "activites", "flag_hors_agri",
           "pj_phone", "pj_mobile", "pj_name", "pj_method",
           "source_telephone", "source_website"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s9")


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v if v is not None else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departement", default="63")
    ap.add_argument("--version", default="v1")
    args = ap.parse_args()
    dept = args.departement

    ours_path = CHECK_DIR / f"operateurs_{dept}.csv"
    match_path = CHECK_DIR / f"matched_{dept}.csv"
    if not ours_path.exists():
        sys.exit(f"{ours_path} missing — run m3ag_s2_transform.py first.")
    with ours_path.open(encoding="utf-8-sig", newline="") as fh:
        ops = list(csv.DictReader(fh))

    matches = {}
    if match_path.exists():
        with match_path.open(encoding="utf-8-sig", newline="") as fh:
            for m in csv.DictReader(fh, delimiter=";"):
                matches[m["row_id"]] = m
    else:
        log.warning(f"{match_path} not found — exporting the API baseline only.")

    rows = []
    for op in ops:
        rid = op["siret"] or f"NB{op['numeroBio']}"
        m = matches.get(rid, {})
        api_tel = op["telephone"] or op["telephoneCommerciale"]
        pj_tel = (m.get("phone") or m.get("mobile") or "").strip()
        tel_final, tel_src = "", ""
        if api_tel:
            tel_final, tel_src = api_tel, "agencebio"     # owner-declared
        elif pj_tel:
            tel_final, tel_src = pj_tel, "pagesjaunes"
        site_api = (op["siteWebs"].split("; ")[0] if op["siteWebs"] else "")
        site_pj = (m.get("website") or "").strip()
        site_final, site_src = "", ""
        if site_api:
            site_final, site_src = site_api, "agencebio"
        elif site_pj:
            site_final, site_src = site_pj, "pagesjaunes"

        row = {c: "" for c in COLUMNS}
        for c in ("raisonSociale", "siret", "gerant", "telephone",
                  "telephoneCommerciale", "codeNAF", "siteWebs", "categories",
                  "productions", "adresse", "codePostal", "ville", "lat", "lon",
                  "email", "numeroBio", "activites", "flag_hors_agri"):
            row[c] = clean(op.get(c, ""))
        row.update({
            "telephone_final": tel_final, "website_final": site_final,
            "pj_phone": clean(m.get("phone", "")),
            "pj_mobile": clean(m.get("mobile", "")),
            "pj_name": clean(m.get("listing_name", "")),
            "pj_method": clean(m.get("method", "")),
            "source_telephone": tel_src, "source_website": site_src,
        })
        rows.append(row)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"agriculteurs_{dept}_{args.version}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    try:
        from openpyxl import Workbook
    except ImportError:
        sys.exit("openpyxl required: pip install openpyxl")
    xlsx_path = OUT_DIR / f"agriculteurs_{dept}_{args.version}.xlsx"
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Agriculteurs")
    ws.append(COLUMNS)
    for r in rows:
        vals = []
        for c in COLUMNS:
            v = r[c]
            # SIRET/CP as text — French Excel mangles them as numbers.
            if c in ("siret", "codePostal") and v:
                vals.append(str(v))
            else:
                vals.append(v)
        ws.append(vals)
    wb.save(xlsx_path)

    n = len(rows)
    tel = sum(1 for r in rows if r["telephone_final"])
    tel_pj = sum(1 for r in rows if r["source_telephone"] == "pagesjaunes")
    mail = sum(1 for r in rows if r["email"])
    site = sum(1 for r in rows if r["website_final"])
    reach = sum(1 for r in rows if r["telephone_final"] or r["email"] or r["website_final"])
    log.info("─" * 62)
    log.info(f"[{dept}] {n} rows -> {xlsx_path.name} + .csv")
    log.info(f"  telephone_final   {tel:>5}  ({tel/n:.1%})   of which PJ: {tel_pj}")
    log.info(f"  email             {mail:>5}  ({mail/n:.1%})")
    log.info(f"  website_final     {site:>5}  ({site/n:.1%})")
    log.info(f"  joignables        {reach:>5}  ({reach/n:.1%})  (phone OR email OR site)")


if __name__ == "__main__":
    main()
