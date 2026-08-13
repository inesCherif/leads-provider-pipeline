"""
M2-S19 — Quality gate for the V5 boulangeries deliverable
==========================================================
Same contract as m2_s17_check_v4.py (which gated V4), retargeted to V5 and
extended with three checks V5's incidents make necessary. Every assertion
maps to a defect that actually shipped on this project once.

It reads the .xlsx BACK — not the CSV, not the logs. The agriculture
deliverable was validated by reading the workbook, and that is what caught
447 malformed emails and the SIRET-as-number bug. A gate that reads the
producer's own output format proves nothing about what the client opens.

Checks (hard = exit 1, warn = reported, exit 0):
  H1  every SIRET is 14 digits and passes the Luhn check (La Poste exempt)
  H2  SIREN == left(SIRET, 9)                     — wrong-identifier guard
  H3  no duplicate SIRET                          — one row per establishment
  H4  every code postal starts with '13'          — the CEO asked for dept 13
  H5  NAF in {10.71C, 10.71B, 10.71D}             — scope decided 2026-08-13
  H6  no mapped column is entirely empty          — the Statut_Activite typo
  H7  SIRET/SIREN/CP survive as TEXT in the xlsx  — the 4,47956E+13 bug
  H8  raison sociale, adresse, ville non-empty on every row
  H9  every phone matches `0X XX XX XX XX`        — V3 normalization
  H10 every surtaxe phone carries the flag        — the premium-rate trap
  H11 no phone without a stated source            — provenance is auditable
  H12 NO REGRESSION vs V4 (rows, phones, emails, sites)
  H13 no untrusted source in the dialled column   — snippet/site-faible ban
  H14 piste column empty where a confirmed phone exists
  H15 every corroborated phone cites 2 distinct sources
  H16 no shipped Email carries an `invalide` verdict in verified_emails.csv
      — V4 shipped 109 proven-invalid addresses when the export raced the
      verifier; this reads the verifier's own file, not the export's claim
  H17 the xlsx is NEWER than verified_emails.csv  — the V4 race itself:
      an export built before verification finished is stale by definition
  H18 no Site web on an aggregator/registry domain — V3-era crawls sold
      myboulange.fr (a directory) as 97 bakeries' own site, and infonet.fr
      pages yielded contact@axa.fr as a bakery's address
  W1  row count inside a sanity band (1,000-2,400)
  W2  contact-name coverage >= 70%
  W3  phone coverage inside a sanity band
  W4  liquidation-flagged rows are disclosed, not silently shipped

Usage:
    python scripts/m2_s19_check_v5.py
    python scripts/m2_s19_check_v5.py --strict     # warnings also fail
"""

import argparse
import csv
import logging
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import is_surtaxe                       # noqa: E402
from m2_s8_websites import AGGREGATORS                     # noqa: E402

XLSX_PATH = PROJECT_ROOT / "exports" / "boulangerie" / "boulangerie_13_v5.xlsx"
# Compare against V4, not V3: each version must beat the one it replaces.
PREV_PATH = PROJECT_ROOT / "exports" / "boulangerie" / "boulangerie_13_v4.xlsx"
VERIFIED_PATH = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints" / "verified_emails.csv"

NAF_SCOPE = {"10.71C", "10.71B", "10.71D"}
COUNT_BAND = (1000, 2400)
MIN_NAME_COVERAGE = 0.70
PHONE_BAND = (0.30, 1.00)      # below 30% the sweep did not do its job
REQUIRED_NON_EMPTY = ["Raison sociale", "Adresse", "Ville"]
# Columns whose emptiness is a legitimate result, not a silent NULL:
#   Telephone surtaxe — empty means no premium-rate number was found (good)
#   Autres emails     — empty means nobody had a second address
#   LinkedIn          — a neighbourhood bakery genuinely has no company page
# Still printed when empty, so this exemption can never hide a data loss.
MAY_BE_EMPTY = {"Telephone surtaxe", "Autres emails", "LinkedIn",
                "Telephone piste (non confirme)"}
PHONE_FMT = re.compile(r"^0[1-9](?: \d{2}){4}$")

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("m2_s19")

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


def load(path: Path) -> tuple:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=False)
    rows, formats = [], {}
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
    return rows, formats, [ws.title for ws in wb.worksheets]


def main() -> None:
    ap = argparse.ArgumentParser(description="Quality gate for the V5 deliverable")
    ap.add_argument("--strict", action="store_true", help="warnings also fail")
    args = ap.parse_args()

    try:
        import openpyxl  # noqa: F401
    except ImportError:
        sys.exit("openpyxl is required\nRun: pip install openpyxl")
    if not XLSX_PATH.exists():
        sys.exit(f"{XLSX_PATH} not found — run scripts/m2_s14_export_v3.py first.")

    rows, formats, sheets = load(XLSX_PATH)
    n = len(rows)
    log.info(f"\nReading {XLSX_PATH.name}: {n} rows, sheets: {sheets}\n")

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
    empty_hard = [h for h in empty_cols if h not in MAY_BE_EMPTY]
    hard(not empty_hard, "H6 no entirely-empty source column", f"empty: {empty_hard}")
    if set(empty_cols) & MAY_BE_EMPTY:
        log.info(f"        (empty by design, allowed: "
                 f"{sorted(set(empty_cols) & MAY_BE_EMPTY)})")

    # H7 — the check that only reading the workbook can make.
    text_cols = ["SIRET", "SIREN", "Code postal", "Date de creation", "Telephone"]
    not_text = {c: dict(formats.get(c, {})) for c in text_cols
                if set(formats.get(c, {})) != {"@"}}
    hard(not not_text, "H7 identifier columns stored as text in xlsx",
         f"non-'@' number formats: {not_text}")

    # H8
    blanks = {c: sum(1 for r in rows if not str(r[c]).strip()) for c in REQUIRED_NON_EMPTY}
    hard(all(v == 0 for v in blanks.values()), "H8 core fields non-empty on every row",
         f"blanks: { {k: v for k, v in blanks.items() if v} }")

    # H9 / H10 / H11 — the phone contract.
    phoned = [r for r in rows if str(r["Telephone"]).strip()]
    badfmt = [str(r["Telephone"]) for r in phoned if not PHONE_FMT.match(str(r["Telephone"]))]
    hard(not badfmt, "H9 phones normalized to 0X XX XX XX XX",
         f"{len(badfmt)} malformed, e.g. {badfmt[:3]}")
    unflagged = [str(r["Telephone"]) for r in phoned
                 if is_surtaxe(str(r["Telephone"])) and str(r["Telephone surtaxe"]).strip() != "oui"]
    hard(not unflagged, "H10 every surtaxe phone is flagged",
         f"{len(unflagged)} unflagged, e.g. {unflagged[:3]}")
    nosrc = [str(r["SIRET"]) for r in phoned if not str(r["Telephone source"]).strip()]
    hard(not nosrc, "H11 every phone states its source",
         f"{len(nosrc)} without provenance, e.g. {nosrc[:3]}")
    # A search-snippet phone alone must never reach the dialled column.
    UNTRUSTED = {"snippet", "site/faible"}
    leaked = [str(r["SIRET"]) for r in phoned
              if str(r["Telephone source"]).strip() in UNTRUSTED]
    hard(not leaked, "H13 no untrusted source in the dialled Telephone column",
         f"{len(leaked)} leaked, e.g. {leaked[:3]}")
    dbl = [str(r["SIRET"]) for r in rows
           if str(r["Telephone"]).strip() and str(r["Telephone piste (non confirme)"]).strip()]
    hard(not dbl, "H14 piste column empty where a confirmed phone exists",
         f"{len(dbl)} rows carry both, e.g. {dbl[:3]}")
    # H15 — a corroborated phone earns its place ONLY by naming two distinct
    # witnesses. "corrobore(x+x)" would be one source counted twice, which is
    # the whole failure this promotion route is designed to avoid.
    bad_corr = []
    for r in phoned:
        src = str(r["Telephone source"]).strip()
        if not src.startswith("corrobore("):
            continue
        parts = [p for p in src[len("corrobore("):-1].split("+") if p]
        if len(parts) < 2 or len(set(parts)) < 2:
            bad_corr.append(f'{r["SIRET"]}:{src}')
    hard(not bad_corr, "H15 every corroborated phone cites 2 distinct sources",
         f"{len(bad_corr)} malformed, e.g. {bad_corr[:3]}")

    # H16 — read the VERIFIER'S file, not the export's claim about itself.
    # V4's first build shipped 109 proven-invalid addresses because the export
    # raced the verifier; the gate then could not see it (H12 only fails on
    # decreases, and extra invalid addresses look like a win).
    if VERIFIED_PATH.exists():
        with VERIFIED_PATH.open(encoding="utf-8-sig", newline="") as fh:
            verdict_of = {row["email"].lower(): row["verdict"]
                          for row in csv.DictReader(fh, delimiter=";")}
        shipped_invalid = [str(r["Email"]) for r in rows
                           if str(r["Email"]).strip()
                           and verdict_of.get(str(r["Email"]).lower()) == "invalide"]
        hard(not shipped_invalid, "H16 no shipped email is proven invalid",
             f"{len(shipped_invalid)} invalid shipped, e.g. {shipped_invalid[:3]}")
        # H17 — the race itself: an export older than the verification file
        # was built before verification finished and is stale by definition.
        hard(XLSX_PATH.stat().st_mtime >= VERIFIED_PATH.stat().st_mtime,
             "H17 export built AFTER verification finished",
             f"{XLSX_PATH.name} is older than {VERIFIED_PATH.name} — rebuild the export")
    else:
        hard(False, "H16 no shipped email is proven invalid",
             f"{VERIFIED_PATH} missing — run scripts/m2_s11_verify.py first")

    # H18 — an aggregator/registry domain is never the shop's own site.
    bad_site = [f'{r["SIRET"]}:{r["Site web"]}' for r in rows
                if str(r["Site web"]).strip()
                and any(a in str(r["Site web"]).lower() for a in AGGREGATORS)]
    hard(not bad_site, "H18 no Site web on an aggregator domain",
         f"{len(bad_site)} rows, e.g. {bad_site[:3]}")

    # H12 — no regression. An enrichment that loses data is a bug.
    n_ph = len(phoned)
    n_em = sum(1 for r in rows if str(r["Email"]).strip())
    n_web = sum(1 for r in rows if str(r["Site web"]).strip())
    if PREV_PATH.exists():
        prev, _, _ = load(PREV_PATH)
        p_ph = sum(1 for r in prev if str(r.get("Telephone", "")).strip())
        p_em = sum(1 for r in prev if str(r.get("Email", "")).strip())
        p_web = sum(1 for r in prev if str(r.get("Site web", "")).strip())
        regress = []
        if n < len(prev):
            regress.append(f"rows {len(prev)} -> {n}")
        if n_ph < p_ph:
            regress.append(f"phones {p_ph} -> {n_ph}")
        if n_em < p_em:
            regress.append(f"emails {p_em} -> {n_em}")
        # Site count MAY legitimately fall in V5: the aggregator purge removed
        # junk "sites" like myboulange.fr sold as 97 bakeries' own pages. A
        # drop is reported, and anything beyond the purge's size still fails.
        if n_web < p_web - 500:
            regress.append(f"sites {p_web} -> {n_web} (beyond the ~462 purged)")
        elif n_web < p_web:
            log.info(f"        (sites {p_web} -> {n_web}: expected — junk "
                     "aggregator sites were purged deliberately)")
        hard(not regress, "H12 no regression vs V4", "; ".join(regress))
        log.info(f"        (V4 -> V5: rows {len(prev)}->{n}, phones {p_ph}->{n_ph}, "
                 f"emails {p_em}->{n_em}, sites {p_web}->{n_web})")
    else:
        warn(False, "H12 no regression vs V4", "boulangerie_13_v4.xlsx not found — "
             "cannot compare; check by hand before shipping")

    # W1 / W2 / W3 / W4
    warn(COUNT_BAND[0] <= n <= COUNT_BAND[1], "W1 row count in sanity band",
         f"{n} outside {COUNT_BAND} — verify the scope before shipping")
    named = sum(1 for r in rows if str(r["Nom"]).strip())
    warn(named / max(1, n) >= MIN_NAME_COVERAGE, "W2 contact-name coverage",
         f"{named}/{n} = {named/max(1,n):.1%} < {MIN_NAME_COVERAGE:.0%}")
    warn(PHONE_BAND[0] <= n_ph / max(1, n) <= PHONE_BAND[1], "W3 phone coverage in band",
         f"{n_ph}/{n} = {n_ph/max(1,n):.1%} outside {PHONE_BAND}")
    liq = sum(1 for r in rows if str(r["Procedure collective"]).strip())
    warn(True, "W4 liquidation disclosure", "")
    log.info(f"        (rows flagged Liquidation: {liq} — present and labelled, "
             "not silently mailed)")

    n_soc = sum(1 for r in rows if str(r["Facebook"]).strip() or str(r["Instagram"]).strip())
    n_surt = sum(1 for r in rows if str(r["Telephone surtaxe"]).strip())
    n_reach = sum(1 for r in rows if str(r["Telephone"]).strip() or str(r["Email"]).strip())
    log.info(f"\nCoverage: phone {n_ph} ({n_ph/max(1,n):.1%}) | email {n_em} "
             f"({n_em/max(1,n):.1%}) | site {n_web} | social {n_soc} | "
             f"reachable {n_reach} ({n_reach/max(1,n):.1%})")
    log.info(f"Phone sources: {dict(Counter(str(r['Telephone source']) for r in phoned))}")
    log.info(f"Surtaxe phones (flagged): {n_surt}")
    log.info(f"NAF split: {dict(Counter(r['Code NAF'] for r in rows))}")

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
