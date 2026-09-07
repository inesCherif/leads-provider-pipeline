"""
M3AG-S14 — Call sheet for the téléopératrice: few columns, only what we stand behind
=================================================================================
The full deliverable (m3ag_s9) is a 45-column audit trail. A phone operator
needs the opposite: one row per business she can actually dial, every
column meaningful, no "piste" numbers, no closed companies, no e-mail that
is a known bounce.

Filters, in this order (each one counted in the log):
  1. a dialled phone exists            (telephone_final; pistes never ship)
  2. farmers only                      (flag_hors_agri == 0)
  3. SIRENE says still active          (m3ag_s13: unité 'C' or établissement 'F'
                                        → dropped; not found → kept, labelled)
  4. one row per SIRET                 (two certifications → productions merged)

E-mail: whatever m3ag_s9 shipped as email_final — `invalide` is already
withheld there — labelled `vérifié` / `déclaré par l'exploitant` / `non vérifié`.

The xlsx is read BACK at the end and gated (m3ag_s12 pattern): phone shape,
no surtaxé, no closed SIRET, SIRET unique, CP as text, no invalide e-mail,
no aggregator/mairie mailbox. Non-zero exit on any failure.

Input : exports/agriculteurs/agriculteurs_<dept>_<version>.csv     (m3ag_s9)
        exports/agriculteurs/checkpoints/sirene_etat_<dept>.csv     (m3ag_s13)
        exports/agriculteurs/checkpoints/verified_emails.csv         (m3ag_s11)
Output: exports/agriculteurs/agriculteurs_<dept>_teleop_<out>.xlsx  (+ .csv)

Usage:
    python scripts/m3ag_s14_export_teleop.py --departement 63 --version v3 --out-version v1
"""

import argparse
import csv
import logging
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m1_s8_export import ILLEGAL_XML                                   # noqa: E402
from m2lib_contact import is_surtaxe, plausible_fr_number, FREE_MAIL   # noqa: E402
from m3ag_lib import CHECK_DIR, OUT_DIR, read_csv, is_aggregator        # noqa: E402
from m3ag_s12_check import PHONE_RE, EMAIL_RE, MAIRIE_RE, EXTRA_FREE_MAIL, load_xlsx  # noqa: E402

COLUMNS = ["Entreprise", "Contact", "Téléphone", "Origine du téléphone",
           "E-mail", "Statut e-mail", "Adresse", "Code postal", "Ville",
           "Productions", "SIRET", "Statut SIRENE"]

PHONE_SOURCE_FR = {
    "agencebio": "déclaré par l'exploitant (Agence Bio)",
    "pagesjaunes": "Pages Jaunes",
    "osm": "OpenStreetMap",
    "bienvenue_ferme": "Bienvenue à la ferme",
    "places": "Google Maps",
    "site/confirme": "site web de l'exploitation",
    "corrobore": "2 sites indépendants concordants",
}
MAX_PRODUCTIONS = 6          # a 1,281-character cell is not a talking point

LISEZ_MOI = [
    ("Fichier", "Agriculteurs bio — liste d'appel, département {dept}, générée le {today}."),
    ("Sélection", "Une ligne par entreprise agricole ayant un numéro de téléphone que nous "
                  "tenons d'une source identifiée. Les magasins, distributeurs et associations "
                  "sont exclus ; les entreprises fermées au registre SIRENE aussi."),
    ("Entreprise", "Raison sociale de l'exploitation (registre Agence Bio)."),
    ("Contact", "Nom du gérant / exploitant. Vide = inconnu, demander le responsable."),
    ("Téléphone", "Numéro à composer, format 0X XX XX XX XX. Jamais de numéro surtaxé."),
    ("Origine du téléphone", "D'où vient le numéro : déclaré par l'exploitant à l'Agence Bio, "
                             "Pages Jaunes, OpenStreetMap, Bienvenue à la ferme, le site web "
                             "de l'exploitation, ou deux sites indépendants qui donnent le même numéro."),
    ("E-mail", "Adresse de l'exploitation quand nous en avons une. Vide = aucune adresse fiable."),
    ("Statut e-mail", "vérifié = le serveur de messagerie a confirmé que la boîte existe. "
                      "déclaré par l'exploitant = donnée par l'exploitant lui-même, le serveur "
                      "(Orange, Wanadoo, Hotmail…) refuse la vérification. non vérifié = trouvée "
                      "sur le web, non confirmée. Les adresses prouvées fausses ne sont pas dans le fichier."),
    ("Adresse / Code postal / Ville", "Adresse active de l'exploitation dans le département."),
    ("Productions", "Productions certifiées bio (6 premières), pour personnaliser l'appel."),
    ("SIRET", "Identifiant de l'établissement, en texte."),
    ("Statut SIRENE", "actif = établissement actif au registre à la date du fichier. "
                      "SIRET inconnu / non retrouvé = pas de vérification possible, ligne conservée."),
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s14")


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v if v is not None else "")).strip()


def phone_source_fr(src: str) -> str:
    return PHONE_SOURCE_FR.get(src.split("(")[0], src)


def email_status_fr(src: str, statut: str) -> str:
    if statut == "valide":
        return "vérifié"
    if statut == "risque":
        return "risqué (le domaine accepte tout)"
    if src == "agencebio":
        return "déclaré par l'exploitant"
    return "non vérifié"


def short_productions(p: str) -> str:
    items = [x.strip() for x in re.split(r"\s*;\s*", p) if x.strip()]
    if len(items) <= MAX_PRODUCTIONS:
        return "; ".join(items)
    return "; ".join(items[:MAX_PRODUCTIONS]) + " ; …"


def build(dept: str, version: str) -> tuple[list[dict], Counter, dict]:
    src = OUT_DIR / f"agriculteurs_{dept}_{version}.csv"
    if not src.exists():
        sys.exit(f"{src} missing — run m3ag_s9_export.py --version {version} first.")
    rows_in = read_csv(src, delim=",")
    etat = {r["siret"]: r for r in read_csv(CHECK_DIR / f"sirene_etat_{dept}.csv")}
    if not etat:
        sys.exit(f"sirene_etat_{dept}.csv missing — run m3ag_s13_sirene_etat.py first.")

    stats = Counter(rows_in=len(rows_in))
    kept: dict[str, dict] = {}       # key -> row (dedupe on SIRET)
    for r in rows_in:
        if not r["telephone_final"]:
            stats["dropped: no dialled phone"] += 1
            continue
        if r["flag_hors_agri"] == "1":
            stats["dropped: hors agri (shop, distributor, association)"] += 1
            continue
        siret = clean(r["siret"])
        e = etat.get(siret)
        if len(siret) != 14:
            sirene = "SIRET inconnu"
            stats["kept: no 14-digit SIRET (unverifiable)"] += 1
        elif e is None or not e["etat_ul"]:
            sirene = "non retrouvé au registre"
            stats["kept: SIRET not found in the registry"] += 1
        elif e["etat_ul"] == "C" or e["etat_etab"] == "F":
            stats["dropped: closed in SIRENE"] += 1
            continue
        else:
            sirene = "actif"
            if "liquidateur" in e["note"]:
                sirene = "actif (liquidateur nommé)"
                stats["kept: active but a liquidator is named"] += 1
        key = siret if len(siret) == 14 else f"NB{r['numeroBio']}"
        if key in kept:
            stats["merged: second certification on the same SIRET"] += 1
            prev = kept[key]
            extra = [x for x in re.split(r"\s*;\s*", r["productions"]) if x and x not in prev["_prod"]]
            prev["_prod"] += extra
            prev["Contact"] = prev["Contact"] or clean(r["gerant"])
            prev["E-mail"] = prev["E-mail"] or clean(r["email_final"])
            if not prev["_email_src"] and r["email_final"]:
                prev["_email_src"], prev["_email_statut"] = r["source_email"], r["email_statut"]
            continue
        kept[key] = {
            "Entreprise": clean(r["raisonSociale"]), "Contact": clean(r["gerant"]),
            "Téléphone": r["telephone_final"], "Origine du téléphone": phone_source_fr(r["source_telephone"]),
            "E-mail": clean(r["email_final"]),
            "_email_src": r["source_email"], "_email_statut": r["email_statut"],
            "Adresse": clean(r["adresse"]), "Code postal": clean(r["codePostal"]), "Ville": clean(r["ville"]),
            "_prod": [x for x in re.split(r"\s*;\s*", r["productions"]) if x],
            "SIRET": siret, "Statut SIRENE": sirene,
        }
    out = []
    for row in kept.values():
        row["Statut e-mail"] = email_status_fr(row["_email_src"], row["_email_statut"]) if row["E-mail"] else ""
        row["Productions"] = short_productions("; ".join(row["_prod"]))
        out.append({c: row[c] for c in COLUMNS})
    out.sort(key=lambda r: (r["Code postal"], r["Entreprise"]))
    stats["rows_out"] = len(out)
    return out, stats, etat


def write(rows: list[dict], dept: str, out_version: str) -> Path:
    csv_path = OUT_DIR / f"agriculteurs_{dept}_teleop_{out_version}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    xlsx_path = OUT_DIR / f"agriculteurs_{dept}_teleop_{out_version}.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Liste d'appel"
    ws.append(COLUMNS)
    for r in rows:
        ws.append([str(r[c]) if c in ("SIRET", "Code postal") else r[c] for c in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    widths = {"Entreprise": 36, "Contact": 28, "Téléphone": 16, "Origine du téléphone": 34,
              "E-mail": 34, "Statut e-mail": 24, "Adresse": 34, "Code postal": 11,
              "Ville": 24, "Productions": 50, "SIRET": 16, "Statut SIRENE": 18}
    for i, c in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths[c]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    lm = wb.create_sheet("Lisez-moi")
    lm.column_dimensions["A"].width = 28
    lm.column_dimensions["B"].width = 110
    for k, v in LISEZ_MOI:
        lm.append([k, v.format(dept=dept, today=date.today().strftime("%d/%m/%Y"))])
        lm.cell(row=lm.max_row, column=1).font = Font(bold=True)
        lm.cell(row=lm.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(xlsx_path)
    return xlsx_path


def gate(xlsx_path: Path, dept: str, version: str, etat: dict, expected: int) -> int:
    rows = load_xlsx(xlsx_path)
    verified = {r["email"].lower(): r["verdict"] for r in read_csv(CHECK_DIR / "verified_emails.csv")}
    src = {r["siret"]: r for r in read_csv(OUT_DIR / f"agriculteurs_{dept}_{version}.csv", delim=",")}
    fails = []

    def check(cond, label):
        print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
        if not cond:
            fails.append(label)

    print(f"{xlsx_path.name}: {len(rows)} rows, {len(rows[0]) if rows else 0} columns")
    check(len(rows) == expected, f"T1 row count matches the build ({len(rows)} vs {expected})")
    check(all(r["Téléphone"] and PHONE_RE.match(str(r["Téléphone"])) for r in rows), "T2 every row has a phone in 0X XX XX XX XX shape")
    check(not any(is_surtaxe(str(r["Téléphone"])) or not plausible_fr_number(str(r["Téléphone"])) for r in rows), "T2 no surtaxé / implausible number")
    check(all(r["Origine du téléphone"] for r in rows), "T3 phone provenance always filled")
    sirets = [str(r["SIRET"]) for r in rows if r["SIRET"]]
    check(len(sirets) == len(set(sirets)), f"T4 SIRET unique ({len(sirets) - len(set(sirets))} repeats)")
    closed = [s for s in sirets if s in etat and (etat[s]["etat_ul"] == "C" or etat[s]["etat_etab"] == "F")]
    check(not closed, f"T5 no SIRET closed in SIRENE ({len(closed)})")
    check(all(isinstance(r["SIRET"], str) and isinstance(r["Code postal"], str) and len(r["Code postal"]) == 5 for r in rows), "T6 SIRET and CP stored as text, CP 5 chars")
    mails = [str(r["E-mail"]).lower() for r in rows if r["E-mail"]]
    check(all(EMAIL_RE.match(m) for m in mails), "T7 e-mail shape")
    check(not any(verified.get(m) == "invalide" for m in mails), "T7 no e-mail verified invalide")
    bad_dom = [m for m in mails if (is_aggregator(m.rpartition("@")[2]) and m.rpartition("@")[2] != "gmail.com") or MAIRIE_RE.search(m.rpartition("@")[2])]
    check(not bad_dom, f"T7 no aggregator / mairie mailbox ({len(bad_dom)})")
    check(all(bool(r["E-mail"]) == bool(r["Statut e-mail"]) for r in rows), "T8 e-mail statut filled iff e-mail filled")
    dom = Counter(m.rpartition("@")[2] for m in mails if m.rpartition("@")[2] not in FREE_MAIL | EXTRA_FREE_MAIL)
    shared = [d for d, n in dom.items() if n > 2]
    check(not shared, f"T9 no corporate domain on > 2 rows (franchise HQ) ({shared[:3]})")
    check(all(not src.get(str(r["SIRET"]), {}).get("flag_hors_agri") == "1" for r in rows), "T10 no hors-agri row")
    check(all(r["Statut SIRENE"] for r in rows), "T11 Statut SIRENE always filled")
    print(f"  info  Contact filled: {sum(1 for r in rows if r['Contact'])}/{len(rows)}; "
          f"e-mail: {len(mails)} ({Counter(r['Statut e-mail'] for r in rows if r['E-mail']).most_common()})")
    print(f"{len(fails)} failed")
    return 1 if fails else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departement", default="63")
    ap.add_argument("--version", default="v3", help="m3ag_s9 version to read")
    ap.add_argument("--out-version", default="v1")
    args = ap.parse_args()

    rows, stats, etat = build(args.departement, args.version)
    xlsx_path = write(rows, args.departement, args.out_version)
    log.info("─" * 62)
    log.info(f"[{args.departement}] {stats['rows_in']} rows in -> {stats['rows_out']} rows out -> {xlsx_path.name} + .csv")
    for k, v in stats.most_common():
        if k not in ("rows_in", "rows_out"):
            log.info(f"  {k:<58} {v:>5}")
    log.info(f"  phone origin: {dict(Counter(r['Origine du téléphone'] for r in rows))}")
    log.info(f"  e-mails: {sum(1 for r in rows if r['E-mail'])}  {dict(Counter(r['Statut e-mail'] for r in rows if r['E-mail']))}")
    log.info("reading the xlsx back:")
    sys.exit(gate(xlsx_path, args.departement, args.version, etat, len(rows)))


if __name__ == "__main__":
    main()
