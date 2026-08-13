"""
M2-S3 — Build the client-facing Excel for boulangeries dept 13 (V1)
====================================================================
Reads exports/boulangerie/checkpoints/etablissements.csv (m2_s2) and writes

    exports/boulangerie/boulangerie_13_v1.xlsx    <- the CEO's deliverable
    exports/boulangerie/boulangerie_13_v1.csv     <- for systems, not the client

xlsx is the client format, for reasons already paid for on this project:
French Excel splits CSV on ';' not ',', turns a 14-digit SIRET into
4,47956E+13, and eats the leading zero of a postal code. In .xlsx the
identifier columns are forced to text and none of that can happen.

This deliberately does NOT import m1_s8_export.write_xlsx: that writer is
welded to agriculture's module-level COLUMNS/SECTOR_SLUG and its cell_value
subscripts row["tier"], which sector-2 rows do not have. Refactoring the live
agriculture export to share ~30 lines would risk a shipped deliverable for no
gain. The two constants worth sharing — ILLEGAL_XML and the text-column
rationale — are imported/mirrored, and the divergence is intentional.

Usage:
    python scripts/m2_s3_export.py
    python scripts/m2_s3_export.py --exclude-liquidation   # drop the 20 flagged
    python scripts/m2_s3_export.py --split-naf             # one sheet per NAF code
"""

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.m1_s8_export import ILLEGAL_XML          # noqa: E402
from scripts.m2_s2_transform import NAF_LABELS        # noqa: E402  (single source of scope truth)

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OUT_DIR   = PROJECT_ROOT / "exports" / "boulangerie"
SRC_PATH  = CHECK_DIR / "etablissements.csv"
BASENAME  = "boulangerie_13_v1"

# (csv_field, French header shown to the client) — order is the column order.
COLUMNS = [
    ("siret",                "SIRET"),
    ("siren",                "SIREN"),
    ("raison_sociale",       "Raison sociale"),
    ("enseigne",             "Enseigne"),
    ("activite",             "Activite"),
    ("naf_code",             "Code NAF"),
    ("adresse",              "Adresse"),
    ("code_postal",          "Code postal"),
    ("commune",              "Ville"),
    ("prenom",               "Prenom"),
    ("nom",                  "Nom"),
    ("fonction",             "Fonction"),
    ("contact",              "Contact (personne morale)"),
    ("forme_juridique",      "Forme juridique"),
    ("date_creation",        "Date de creation"),
    ("anciennete_ans",       "Anciennete (ans)"),
    ("tranche_effectif",     "Tranche effectif"),
    ("est_siege",            "Siege social"),
    ("procedure_collective", "Procedure collective"),
]

# Excel would silently retype these. SIRET/SIREN become scientific notation,
# postal codes lose their leading zero, and a real date is re-read as MM/DD in
# another locale (01/08/1993 -> 8 January). Forced to text format "@".
TEXT_COLUMNS = {"siret", "siren", "code_postal", "naf_code", "date_creation"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s3")


def load_rows() -> list[dict]:
    if not SRC_PATH.exists():
        sys.exit(f"{SRC_PATH} not found — run scripts/m2_s2_transform.py first.")
    with SRC_PATH.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def cell(row: dict, col: str) -> str:
    return ILLEGAL_XML.sub("", (row.get(col) or "")).strip()


def write_xlsx(path: Path, sheets: dict[str, list[dict]]) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font
    except ImportError:
        sys.exit("openpyxl is required\nRun: pip install openpyxl")

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title[:31])
        ws.freeze_panes = "A2"
        head = []
        for _, header in COLUMNS:
            c = WriteOnlyCell(ws, value=header)
            c.font = Font(bold=True)
            head.append(c)
        ws.append(head)
        for r in rows:
            out = []
            for col, _ in COLUMNS:
                c = WriteOnlyCell(ws, value=cell(r, col))
                if col in TEXT_COLUMNS:
                    c.number_format = "@"
                out.append(c)
            ws.append(out)
    wb.save(path)


def write_csv(path: Path, rows: list[dict]) -> None:
    """utf-8-sig (BOM) or French accents render as mojibake; ';' or every row
    lands in column A. Same two traps as the agriculture export."""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow([h for _, h in COLUMNS])
        for r in rows:
            w.writerow([cell(r, c) for c, _ in COLUMNS])


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the boulangeries dept 13 Excel (V1)")
    ap.add_argument("--exclude-liquidation", action="store_true",
                    help="drop rows flagged Liquidation (default: ship them flagged)")
    ap.add_argument("--split-naf", action="store_true",
                    help="one sheet per NAF code instead of a single sheet")
    args = ap.parse_args()

    rows = load_rows()
    n_all = len(rows)
    if args.exclude_liquidation:
        rows = [r for r in rows if not r.get("procedure_collective")]
        log.info(f"--exclude-liquidation: {n_all - len(rows)} rows dropped")

    if args.split_naf:
        sheets = {f"{code} {NAF_LABELS[code]}"[:31]: [r for r in rows if r["naf_code"] == code]
                  for code in NAF_LABELS
                  if any(r["naf_code"] == code for r in rows)}
    else:
        sheets = {"Boulangeries 13": rows}

    xlsx_path = OUT_DIR / f"{BASENAME}.xlsx"
    csv_path  = OUT_DIR / f"{BASENAME}.csv"
    write_xlsx(xlsx_path, sheets)
    write_csv(csv_path, rows)

    n_by_naf = Counter(r["naf_code"] for r in rows)
    n_named = sum(1 for r in rows if r["nom"])
    n_liq = sum(1 for r in rows if r["procedure_collective"])
    log.info("─" * 64)
    log.info(f"Rows exported                  {len(rows):>6}")
    for code, label in NAF_LABELS.items():
        if n_by_naf.get(code):
            log.info(f"  {code} {label:<34.34} {n_by_naf[code]:>6}")
    log.info(f"  with a named contact         {n_named:>6} ({n_named/max(1,len(rows)):.1%})")
    log.info(f"  flagged Liquidation          {n_liq:>6}")
    log.info(f"sheets: {', '.join(sheets)}")
    log.info(f"written -> {xlsx_path}")
    log.info(f"written -> {csv_path}")
    log.info("Next: python scripts/m2_s4_check.py")


if __name__ == "__main__":
    main()
