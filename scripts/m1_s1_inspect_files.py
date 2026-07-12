"""
M1-S1 — File Inspection Script
================================
PURPOSE : Read-only. Inspects all three XLSX files in the pilot data folder
          and produces a structured report to help determine:
            1. What columns each file has (and how they differ)
            2. How many rows each file has
            3. Completeness (% non-null) per column
            4. Sample rows (first 5)
            5. SIRET/SIREN overlap across files → are they pipeline stages or independent?
            6. Email / phone / status column presence and fill rate

NO WRITES. NO DATABASE. No files are modified.

REQUIREMENTS:
    pip install pandas openpyxl
"""

import hashlib
import os
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency: run  pip install pandas openpyxl  then retry.")

# ---------------------------------------------------------------------------
# Configuration — adjust DATA_DIR if needed
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent.parent / "Data Globale 05 juillet 2026"

FILES = [
    "Copie de agriculteurs total.xlsx",
    "Copie de Eleveurs_siret_results (liste des sirets qui on pu etre chercher dans data gouv).xlsx",
    "Copie de Eleveurs_verified (liste de toutes les eleveurs avec Siret ).xlsx",
]

# Columns we specifically look for (case-insensitive substring match)
INTEREST_COLS = {
    "siret":    ["siret"],
    "siren":    ["siren"],
    "email":    ["email", "mail", "courriel", "e-mail"],
    "phone":    ["tel", "phone", "portable", "mobile", "fixe", "numero"],
    "status":   ["statut", "etat", "status", "actif", "ferme", "cessation"],
    "name":     ["nom", "name", "denomination", "raison"],
    "address":  ["adresse", "address", "voie", "rue", "libelle_voie"],
    "postal":   ["code_postal", "cp", "postal", "zip"],
    "city":     ["commune", "ville", "city", "localite"],
    "dept":     ["departement", "dept", "dep"],
    "naf":      ["naf", "ape", "activite", "code_activite"],
    "manager":  ["dirigeant", "gerant", "responsable", "contact", "prenom", "first"],
}

SEP = "=" * 70


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def find_interest_cols(df_cols: list[str]) -> dict[str, list[str]]:
    """Return a dict: interest_category → [matching actual column names]"""
    results = {}
    lower_cols = [c.lower() for c in df_cols]
    for category, keywords in INTEREST_COLS.items():
        matches = []
        for i, lc in enumerate(lower_cols):
            for kw in keywords:
                if kw in lc:
                    matches.append(df_cols[i])
                    break
        if matches:
            results[category] = matches
    return results


def completeness(series: pd.Series) -> str:
    total = len(series)
    if total == 0:
        return "0/0"
    filled = series.notna().sum() - (series.astype(str).str.strip() == "").sum()
    pct = filled / total * 100
    return f"{filled:,}/{total:,} ({pct:.0f}%)"


def inspect_file(path: Path) -> pd.DataFrame | None:
    print(f"\n{SEP}")
    print(f"FILE: {path.name}")
    print(SEP)

    if not path.exists():
        print(f"  !! FILE NOT FOUND at: {path}")
        return None

    size_mb = path.stat().st_size / (1024 * 1024)
    file_hash = sha256_file(path)
    print(f"  Size     : {size_mb:.2f} MB")
    print(f"  SHA-256  : {file_hash}")

    # Try reading — detect header row automatically if needed
    try:
        df = pd.read_excel(path, dtype=str, engine="openpyxl")
    except Exception as e:
        print(f"  !! Could not read file: {e}")
        return None

    print(f"  Rows     : {len(df):,}")
    print(f"  Columns  : {len(df.columns)}")

    print("\n--- ALL COLUMNS (with completeness) ---")
    for col in df.columns:
        fill = completeness(df[col])
        print(f"    {col!r:<45}  filled: {fill}")

    interest = find_interest_cols(list(df.columns))
    print("\n--- KEY FIELD DETECTION ---")
    for category, cols in interest.items():
        for col in cols:
            fill = completeness(df[col])
            print(f"    [{category.upper():<10}]  {col!r:<40}  filled: {fill}")
    missing = [c for c in INTEREST_COLS if c not in interest]
    if missing:
        print(f"    !! NOT DETECTED : {', '.join(missing)}")

    print("\n--- FIRST 5 ROWS ---")
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 40)
    print(df.head(5).to_string(index=False))

    # Status value distribution (if detected)
    if "status" in interest:
        for col in interest["status"]:
            print(f"\n--- STATUS VALUES in {col!r} ---")
            vc = df[col].fillna("(empty)").value_counts().head(20)
            for val, cnt in vc.items():
                print(f"    {cnt:>6,}  {val}")

    return df


def compare_sirets(dfs: dict[str, pd.DataFrame]) -> None:
    print(f"\n{SEP}")
    print("CROSS-FILE SIRET OVERLAP ANALYSIS")
    print(SEP)

    siret_sets = {}
    for name, df in dfs.items():
        siret_col = None
        for col in df.columns:
            if "siret" in col.lower():
                siret_col = col
                break
        if siret_col is None:
            print(f"  {name!r}: no SIRET column found — skipping")
            continue
        sirets = (
            df[siret_col]
            .dropna()
            .astype(str)
            .str.strip()
            .str.replace(r"\s+", "", regex=True)
        )
        sirets = sirets[sirets.str.len().isin([9, 14])]  # valid SIREN or SIRET
        siret_sets[name] = set(sirets)
        print(f"  {name!r}: {len(siret_sets[name]):,} valid SIRETs")

    names = list(siret_sets.keys())
    if len(names) < 2:
        print("  Not enough files with SIRETs to compare.")
        return

    print()
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            overlap = siret_sets[a] & siret_sets[b]
            only_a = siret_sets[a] - siret_sets[b]
            only_b = siret_sets[b] - siret_sets[a]
            print(f"  {a!r}  vs  {b!r}")
            print(f"    Overlap (in both)    : {len(overlap):,}")
            print(f"    Only in first file   : {len(only_a):,}")
            print(f"    Only in second file  : {len(only_b):,}")

            if len(overlap) > 0:
                # What fraction of the smaller set is covered by overlap?
                smaller = min(len(siret_sets[a]), len(siret_sets[b]))
                pct = len(overlap) / smaller * 100
                print(f"    Overlap / smaller set: {pct:.1f}%")
                if pct > 80:
                    print(
                        "    >>> HIGH OVERLAP — likely pipeline stages of the same data"
                    )
                elif pct > 30:
                    print(
                        "    >>> PARTIAL OVERLAP — possibly same data with additions or losses"
                    )
                else:
                    print(
                        "    >>> LOW OVERLAP — possibly independent sources"
                    )
            print()


def print_conclusion_template(dfs: dict[str, pd.DataFrame]) -> None:
    print(f"\n{SEP}")
    print("NEXT STEP — Human Decision Required")
    print(SEP)
    print("""
After reviewing the output above, fill in these answers before proceeding to M1-S2:

  File A (agriculteurs total):
    pipeline_stage guess   : [ ] raw  [ ] siret_matched  [ ] verified  [ ] unknown
    collection_date approx : __________
    independent source?    : [ ] yes — independent  [ ] no — derived from another file above

  File B (Eleveurs_siret_results):
    pipeline_stage guess   : [ ] raw  [ ] siret_matched  [ ] verified  [ ] unknown
    collection_date approx : __________
    independent source?    : [ ] yes — independent  [ ] no — derived from another file above

  File C (Eleveurs_verified):
    pipeline_stage guess   : [ ] raw  [ ] siret_matched  [ ] verified  [ ] unknown
    collection_date approx : __________
    independent source?    : [ ] yes — independent  [ ] no — derived from another file above

  Overall verdict:
    [ ] All three are independent sources -> merge and deduplicate all three
    [ ] B and C are derived from A      -> only A is the raw input; treat B/C as enrichment reference
    [ ] Other: ___________________________________________
""")


def main():
    print(f"\n{'#'*70}")
    print("  M1-S1 — PILOT DATA FILE INSPECTION REPORT")
    print(f"  Data directory: {DATA_DIR}")
    print(f"{'#'*70}")

    if not DATA_DIR.exists():
        sys.exit(f"\nERROR: Data directory not found:\n  {DATA_DIR}\nCheck DATA_DIR path in the script.")

    loaded_dfs: dict[str, pd.DataFrame] = {}

    for fname in FILES:
        path = DATA_DIR / fname
        df = inspect_file(path)
        if df is not None:
            loaded_dfs[fname] = df

    compare_sirets(loaded_dfs)
    print_conclusion_template(loaded_dfs)

    print(f"\n{'#'*70}")
    print("  END OF REPORT — M1-S1 complete (read-only, no data was modified)")
    print(f"{'#'*70}\n")


if __name__ == "__main__":
    main()
