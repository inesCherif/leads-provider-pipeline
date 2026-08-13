"""
M2-S12 — Build the enriched deliverable (V2)
=============================================
Merges every V2 checkpoint onto the V1 rows, by SIRET, and writes

    exports/boulangerie/boulangerie_13_v2.xlsx    <- the enriched deliverable
    exports/boulangerie/boulangerie_13_v2.csv

Inputs (each optional — the export degrades, it never crashes on a missing
harvest):
    etablissements.csv   the V1 population, one row per établissement
    matched.csv          phone / website / facebook / email from OSM & PJ
    site_emails.csv      e-mails read off the businesses' own websites
    verified_emails.csv  SMTP verdict per address

Two rules carried over from agriculture, both bought with incidents:

  * AN ADDRESS VERIFIED `invalide` NEVER SHIPS. Before migration 015, marking
    an address invalid did not stop it being exported, so verification
    silently accomplished nothing. Here the filter is explicit.
  * A `faible`-confidence address ships only with its confidence stated. An
    address found on a franchise site or an unconfirmed domain is a lead, not
    a fact, and the column says so.

Emails are ranked: confirmed+valide first, then confirmed+non verifie, then
risque, then faible. The best one lands in `Email`; the rest are counted in
`Autres emails` so nothing is lost silently.

Usage:
    python scripts/m2_s12_export_v2.py
    python scripts/m2_s12_export_v2.py --only-reachable   # rows with a contact
"""

import argparse
import csv
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.m1_s8_export import ILLEGAL_XML            # noqa: E402
from scripts.m2_s2_transform import NAF_LABELS          # noqa: E402

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OUT_DIR   = PROJECT_ROOT / "exports" / "boulangerie"
BASENAME  = "boulangerie_13_v2"

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
    ("telephone",            "Telephone"),
    ("email",                "Email"),
    ("email_statut",         "Email verifie"),
    ("email_confiance",      "Email confiance"),
    ("autres_emails",        "Autres emails"),
    ("site_web",             "Site web"),
    ("facebook",             "Facebook"),
    ("source_contact",       "Source contact"),
    ("contact",              "Contact (personne morale)"),
    ("forme_juridique",      "Forme juridique"),
    ("date_creation",        "Date de creation"),
    ("anciennete_ans",       "Anciennete (ans)"),
    ("tranche_effectif",     "Tranche effectif"),
    ("est_siege",            "Siege social"),
    ("procedure_collective", "Procedure collective"),
]
TEXT_COLUMNS = {"siret", "siren", "code_postal", "naf_code", "date_creation", "telephone"}

# Best-first. A confirmed domain with an SMTP accept is a fact; a franchise
# mailbox on an unconfirmed site is a guess. They must not sort equally.
RANK = {("confirme", "valide"): 0, ("confirme", "non verifie"): 1,
        ("confirme", "risque"): 2, ("faible", "valide"): 3,
        ("faible", "non verifie"): 4, ("faible", "risque"): 5}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s12")


def read(name: str) -> list[dict]:
    p = CHECK_DIR / name
    if not p.exists():
        log.info(f"(skip) {name} not present — that enrichment did not run")
        return []
    with p.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def cell(row: dict, col: str) -> str:
    return ILLEGAL_XML.sub("", str(row.get(col) or "")).strip()


def write_xlsx(path: Path, rows: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="Boulangeries 13")
    ws.freeze_panes = "A2"
    head = []
    for _, h in COLUMNS:
        c = WriteOnlyCell(ws, value=h)
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the enriched boulangerie deliverable")
    ap.add_argument("--only-reachable", action="store_true",
                    help="keep only rows with a phone or an email")
    args = ap.parse_args()

    base = read("etablissements.csv")
    if not base:
        sys.exit("etablissements.csv missing — run scripts/m2_s2_transform.py first.")
    matched = read("matched.csv")
    site_emails = read("site_emails.csv")
    verified = {r["email"].lower(): r["verdict"] for r in read("verified_emails.csv")}

    # Contact channels harvested per SIRET.
    phone, website, facebook, srcs = {}, {}, {}, defaultdict(set)
    cand: dict[str, list[tuple]] = defaultdict(list)   # siret -> [(rank, email, verdict, conf, src)]
    for m in matched:
        s = m["siret"]
        if m.get("phone") and s not in phone:
            phone[s] = m["phone"]
            srcs[s].add(m["source"])
        if m.get("website") and s not in website:
            website[s] = m["website"]
            srcs[s].add(m["source"])
        if m.get("facebook") and s not in facebook:
            facebook[s] = m["facebook"]
        if m.get("email"):
            e = m["email"].lower()
            v = verified.get(e, "non verifie")
            cand[s].append((RANK.get(("confirme", v), 9), e, v, "confirme", m["source"]))
            srcs[s].add(m["source"])
    for r in site_emails:
        s, e = r["siret"], r["email"].lower()
        v = verified.get(e, "non verifie")
        conf = r.get("confiance") or "faible"
        cand[s].append((RANK.get((conf, v), 9), e, v, conf, "site"))
        srcs[s].add("site")
        if r.get("domain") and s not in website:
            website[s] = "https://" + r["domain"]

    n_blocked = 0
    rows = []
    for b in base:
        s = b["siret"]
        # An address proven undeliverable must not ship. Before agriculture's
        # migration 015 this filter did not exist and verification bought
        # nothing at all.
        usable = sorted([c for c in cand.get(s, []) if c[2] != "invalide"])
        n_blocked += len(cand.get(s, [])) - len(usable)
        best = usable[0] if usable else None
        r = dict(b)
        r.update({
            "telephone": phone.get(s, ""),
            "email": best[1] if best else "",
            "email_statut": best[2] if best else "",
            "email_confiance": best[3] if best else "",
            "autres_emails": str(len(usable) - 1) if len(usable) > 1 else "",
            "site_web": website.get(s, ""),
            "facebook": facebook.get(s, ""),
            "source_contact": ", ".join(sorted(srcs.get(s, ()))),
        })
        rows.append(r)

    if args.only_reachable:
        before = len(rows)
        rows = [r for r in rows if r["telephone"] or r["email"]]
        log.info(f"--only-reachable: {before - len(rows)} rows without any contact dropped")

    xlsx = OUT_DIR / f"{BASENAME}.xlsx"
    write_xlsx(xlsx, rows)
    csv_path = OUT_DIR / f"{BASENAME}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow([h for _, h in COLUMNS])
        for r in rows:
            w.writerow([cell(r, c) for c, _ in COLUMNS])

    n = len(rows)
    n_ph = sum(1 for r in rows if r["telephone"])
    n_em = sum(1 for r in rows if r["email"])
    n_web = sum(1 for r in rows if r["site_web"])
    n_reach = sum(1 for r in rows if r["telephone"] or r["email"])
    log.info("─" * 62)
    log.info(f"Rows                       {n:>6}")
    log.info(f"  with a phone             {n_ph:>6} ({n_ph/max(1,n):.1%})")
    log.info(f"  with an EMAIL            {n_em:>6} ({n_em/max(1,n):.1%})")
    log.info(f"  with a website           {n_web:>6}")
    log.info(f"  reachable (phone|email)  {n_reach:>6} ({n_reach/max(1,n):.1%})")
    log.info(f"  email verdicts: {dict(Counter(r['email_statut'] for r in rows if r['email']))}")
    log.info(f"  email confiance: {dict(Counter(r['email_confiance'] for r in rows if r['email']))}")
    log.info(f"  addresses withheld as proven-invalid: {n_blocked}")
    log.info(f"written -> {xlsx}")
    log.info(f"written -> {csv_path}")


if __name__ == "__main__":
    main()
