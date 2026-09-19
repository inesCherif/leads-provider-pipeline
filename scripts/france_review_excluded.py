"""
Consolidate every principle_excluded_<dept>_<version>.csv into ONE file for
Ines, with the rows most likely to be a mistake at the top.

Across France the name test dropped 598 rows, spread over 91 files. Most are
real — CHARCUT 116, VIGNOBLE 73, VITICULT 46 — but VIGNERON (117) and BRASSEUR
(37) are ordinary French surnames, and a farmer called Vigneron is a prospect,
not a winery. Reviewing 598 rows in 91 files is not a task anybody does; 150
ranked rows in one file is.

The ranking is evidence, not a guess:
  * SURNAME-SUSPECT — the word is a known surname AND the NAF is not a wine /
    cider / beer / pork code AND the name looks like a person ("A. VIGNERON",
    "C. VIGNERON"). These are the rescues.
  * CHECK — a surname-capable word, but the name does not read as a person.
  * CLEAR — a trade word (CHARCUT, VIGNOBLE, VITICULT…), or the NAF itself is
    wine / pork. Nothing to do.

Output: exports/producteurs/principle_excluded_FRANCE.csv
A row is rescued by copying its SIRET into config/principle_rescue.csv.

    python scripts/france_review_excluded.py
"""

import csv
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m7_lib import OUT_DIR, load_principle_rescue   # noqa: E402

OUT = OUT_DIR / "principle_excluded_FRANCE.csv"

# Words that are also ordinary French family names. A trade word like
# CHARCUT or VIGNOBLE never is.
SURNAME_WORDS = {"VIGNERON", "BRASSEUR", "PINOT", "VIGNE", "VIGNES", "CAVE", "COCHON"}
# NAF codes that make the match a fact about the activity, not the spelling.
WINE_PORK_NAF = re.compile(r"^(01\.21|11\.0|10\.13B|01\.46)")
# "A. VIGNERON", "C. VIGNERON", "VERONIQUE PINOT (VIGNERON)"
PERSON_RE = re.compile(r"^[A-ZÀ-Ü][A-Za-zÀ-ÿ'’\-]+\s+[A-ZÀ-Ü][A-Za-zÀ-ÿ'’\-]+(\s*\(.*\))?$")
# A legal form means a company, not a person.
COMPANY_RE = re.compile(r"\b(SAS|SARL|SCEA|EARL|GAEC|SCI|SA|SNC|EURL|SCA|CUMA|SOCIETE|STE)\b", re.I)


def verdict(row: dict) -> str:
    word = (row.get("mot_exclu") or "").upper()
    naf = (row.get("naf") or "").strip()
    name = (row.get("nom") or "").strip()
    if word not in SURNAME_WORDS or WINE_PORK_NAF.match(naf):
        return "CLEAR"
    if COMPANY_RE.search(name):
        return "CHECK"
    return "SURNAME-SUSPECT" if PERSON_RE.match(name) else "CHECK"


def main() -> None:
    rescued = load_principle_rescue()
    rows, seen = [], set()
    for p in sorted(OUT_DIR.glob("principle_excluded_*_v*.csv")):
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                key = (r.get("siret") or "", r.get("nom") or "")
                if key in seen:
                    continue
                seen.add(key)
                r["verdict"] = verdict(r)
                r["deja_rescape"] = "oui" if re.sub(r"\D", "", r.get("siret") or "") in rescued else ""
                rows.append(r)

    order = {"SURNAME-SUSPECT": 0, "CHECK": 1, "CLEAR": 2}
    rows.sort(key=lambda r: (order[r["verdict"]], r.get("mot_exclu", ""), r.get("dept", "")))

    fields = ["verdict", "deja_rescape", "dept", "origine", "siret", "siren", "nom",
              "denomination_legale", "enseigne", "naf", "mot_exclu", "commune", "code_postal"]
    with OUT.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    by = Counter(r["verdict"] for r in rows)
    print(f"\n{len(rows)} rows dropped on the name across France -> {OUT}")
    for v in ("SURNAME-SUSPECT", "CHECK", "CLEAR"):
        print(f"  {v:<16} {by.get(v, 0):>4}")
    print(f"\n  words: {dict(Counter(r['mot_exclu'].upper() for r in rows).most_common(8))}")
    print("\n  The SURNAME-SUSPECT rows are the ones to read. Rescue one by copying its")
    print("  SIRET into config/principle_rescue.csv, then rebuild that département.\n")
    for r in [x for x in rows if x["verdict"] == "SURNAME-SUSPECT"][:15]:
        print(f"    {r['dept']:<3} {r['naf']:<8} {r['nom'][:44]:<44} ({r['mot_exclu']})")


if __name__ == "__main__":
    main()
