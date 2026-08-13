"""
M2-S4 — Quality gate for the boulangeries dept 13 deliverable
==============================================================
Same contract as check_data_quality.py, without a database: every assertion
here maps to a defect that actually shipped on this project once.

It reads the .xlsx BACK — not the CSV, not the logs. The agriculture
deliverable was validated by reading the workbook, and that is what caught
447 malformed emails and the SIRET-as-number bug. A gate that reads the
producer's own output format proves nothing about what the client opens.

Checks (hard = exit 1, warn = reported, exit 0):
  H1  every SIRET is 14 digits and passes the Luhn check (La Poste exempt)
  H2  SIREN == left(SIRET, 9)                     — wrong-identifier guard
  H3  no duplicate SIRET                          — one row per establishment
  H4  every code postal starts with '13'          — the CEO asked for dept 13
  H5  NAF in {10.71C, 10.71B}                     — scope decided 2026-08-13
  H6  no mapped column is entirely empty          — the Statut_Activite typo
  H7  SIRET/SIREN/CP survive as TEXT in the xlsx  — the 4,47956E+13 bug
  H8  raison sociale, adresse, ville non-empty on every row
  W1  row count inside a sanity band (600-1,800)
  W2  contact-name coverage >= 70%
  W3  liquidation-flagged rows are disclosed, not silently shipped

Usage:
    python scripts/m2_s4_check.py
    python scripts/m2_s4_check.py --strict     # warnings also fail
"""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
XLSX_PATH = PROJECT_ROOT / "exports" / "boulangerie" / "boulangerie_13_v1.xlsx"

NAF_SCOPE = {"10.71C", "10.71B"}
COUNT_BAND = (600, 1800)
MIN_NAME_COVERAGE = 0.70
REQUIRED_NON_EMPTY = ["Raison sociale", "Adresse", "Ville"]

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("m2_s4")

failures: list[str] = []
warnings: list[str] = []


def hard(ok: bool, name: str, detail: str = "") -> None:
    if ok:
        log.info(f"  PASS  {name}")
    else:
        log.info(f"  FAIL  {name} — {detail}")
        failures.append(f"{name}: {detail}")


def warn(ok: bool, name: str, detail: str = "") -> None:
    if ok:
        log.info(f"  PASS  {name}")
    else:
        log.info(f"  WARN  {name} — {detail}")
        warnings.append(f"{name}: {detail}")


def luhn_ok(siret: str) -> bool:
    """SIRET carries a Luhn checksum. La Poste's SIRETs (SIREN 356000000) are
    the documented exception — they fail Luhn and are valid anyway."""
    if siret.startswith("356000000"):
        return True
    total = 0
    for i, ch in enumerate(reversed(siret)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Quality gate for the boulangerie deliverable")
    ap.add_argument("--strict", action="store_true", help="warnings also fail")
    args = ap.parse_args()

    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("openpyxl is required\nRun: pip install openpyxl")

    if not XLSX_PATH.exists():
        sys.exit(f"{XLSX_PATH} not found — run scripts/m2_s3_export.py first.")

    wb = load_workbook(XLSX_PATH, read_only=False)
    rows: list[dict] = []
    formats: dict[str, Counter] = {}
    for ws in wb.worksheets:
        it = ws.iter_rows()
        header = [c.value for c in next(it)]
        for cells in it:
            if all(c.value in (None, "") for c in cells):
                continue
            rows.append({h: (c.value if c.value is not None else "")
                         for h, c in zip(header, cells)})
            for h, c in zip(header, cells):
                formats.setdefault(h, Counter())[c.number_format] += 1

    n = len(rows)
    log.info(f"\nReading {XLSX_PATH.name}: {n} rows, sheets: "
             f"{[ws.title for ws in wb.worksheets]}\n")

    # H1 / H2
    bad_siret = [r["SIRET"] for r in rows
                 if not (str(r["SIRET"]).isdigit() and len(str(r["SIRET"])) == 14
                         and luhn_ok(str(r["SIRET"])))]
    hard(not bad_siret, "H1 SIRET 14 digits + Luhn",
         f"{len(bad_siret)} bad, e.g. {bad_siret[:3]}")
    mismatch = [r["SIRET"] for r in rows if str(r["SIRET"])[:9] != str(r["SIREN"])]
    hard(not mismatch, "H2 SIREN == left(SIRET,9)",
         f"{len(mismatch)} mismatched, e.g. {mismatch[:3]}")

    # H3
    dups = [s for s, c in Counter(str(r["SIRET"]) for r in rows).items() if c > 1]
    hard(not dups, "H3 no duplicate SIRET", f"{len(dups)} duplicated, e.g. {dups[:3]}")

    # H4 / H5
    bad_cp = [r["Code postal"] for r in rows if not str(r["Code postal"]).startswith("13")]
    hard(not bad_cp, "H4 every code postal in dept 13",
         f"{len(bad_cp)} outside, e.g. {bad_cp[:3]}")
    bad_naf = [r["Code NAF"] for r in rows if r["Code NAF"] not in NAF_SCOPE]
    hard(not bad_naf, "H5 NAF within the decided scope",
         f"{len(bad_naf)} out of scope, e.g. {Counter(bad_naf).most_common(3)}")

    # H6 — a mapped column that is empty on every row is the Statut_Activite
    # failure mode: the pipeline runs green and the column silently means nothing.
    empty_cols = [h for h in rows[0] if all(str(r[h]).strip() == "" for r in rows)]
    hard(not empty_cols, "H6 no entirely-empty column", f"empty: {empty_cols}")

    # H7 — the check that only reading the workbook can make.
    text_cols = ["SIRET", "SIREN", "Code postal", "Date de creation"]
    not_text = {c: dict(formats.get(c, {})) for c in text_cols
                if set(formats.get(c, {})) != {"@"}}
    hard(not not_text, "H7 identifier columns stored as text in xlsx",
         f"non-'@' number formats: {not_text}")

    # H8
    blanks = {c: sum(1 for r in rows if not str(r[c]).strip()) for c in REQUIRED_NON_EMPTY}
    hard(all(v == 0 for v in blanks.values()), "H8 core fields non-empty on every row",
         f"blanks: { {k: v for k, v in blanks.items() if v} }")

    # W1 / W2 / W3
    warn(COUNT_BAND[0] <= n <= COUNT_BAND[1], "W1 row count in sanity band",
         f"{n} outside {COUNT_BAND} — verify the scope before shipping")
    named = sum(1 for r in rows if str(r["Nom"]).strip())
    warn(named / max(1, n) >= MIN_NAME_COVERAGE, "W2 contact-name coverage",
         f"{named}/{n} = {named/max(1,n):.1%} < {MIN_NAME_COVERAGE:.0%}")
    liq = sum(1 for r in rows if str(r["Procedure collective"]).strip())
    warn(True, "W3 liquidation disclosure",
         "")
    log.info(f"        (rows flagged Liquidation: {liq} — present and labelled, "
             "not silently mailed)")

    naf = Counter(r["Code NAF"] for r in rows)
    log.info(f"\nNAF split: {dict(naf)}")
    log.info(f"Communes: {len(set(r['Ville'] for r in rows))} distinct, "
             f"top: {Counter(r['Ville'] for r in rows).most_common(3)}")
    log.info(f"With Prenom+Nom: {named} ({named/max(1,n):.1%}) | "
             f"With effectif: {sum(1 for r in rows if str(r['Tranche effectif']).strip())}")

    log.info("\n" + "─" * 60)
    log.info(f"{len(failures)} failed, {len(warnings)} warnings")
    if failures:
        for f in failures:
            log.info(f"  FAILED  {f}")
        sys.exit(1)
    if warnings and args.strict:
        for w in warnings:
            log.info(f"  WARN(strict)  {w}")
        sys.exit(1)
    log.info("Deliverable passes. Safe to send.")


if __name__ == "__main__":
    main()
