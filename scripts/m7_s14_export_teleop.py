"""
M7-S14 — Maha's call sheet for producteurs: few columns, phones first, nothing she already has
=============================================================================================
Copy of `m6_s14_export_teleop.py`. The full file (m7_s9) is Sam's audit
trail; a phone operator needs one row per business she can dial, every
column meaningful, no row she already received in another file.

Two tabs (same layout as the éleveurs sheet, `Sous-segment` instead of
`Type d'élevage`):
  Sûr       a dialled phone from a measured or owner-declared source (Agence
            Bio, OpenStreetMap, Pages Jaunes, bienvenue-à-la-ferme, the
            direct-sales directories the producer registered on, the farm's
            own site, two independent sources agreeing) and the établissement
            active in SIRENE today.
  Probable  the `Sans SIRET` rows: a directory listing with a phone that the
            registry could not be matched to (identity to confirm on the call).
Dropped, counted: no phone; already sent to Maha (SIRET or phone in any file
she holds); public bodies; liquidation named in the registry; a second row
carrying a phone already on the sheet; éleveurs rows (Maha has them from M6).

Gate T1–T18 as in m6_s14 (T14 = Sous-segment filled; T10 = no public body).

Usage:
    python scripts/m7_s14_export_teleop.py --departement 63 --version v1 --out-version v1
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
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from m1_s8_export import ILLEGAL_XML                                   # noqa: E402
from m2lib_contact import is_surtaxe, plausible_fr_number, FREE_MAIL   # noqa: E402
from m3ag_s12_check import PHONE_RE, EMAIL_RE, MAIRIE_RE, EXTRA_FREE_MAIL  # noqa: E402
from m7_lib import (CHECK_DIR, OUT_DIR, INHERITED_AGRI, INHERITED_ELEVEURS, read_csv, is_aggregator,  # noqa: E402
                    PHONE_SOURCE_FR, load_sent, phone_digits, EXCLUDED_LOOSE_RE)

COLUMNS = ["Entreprise", "Sous-segment", "Bio", "Contact", "Téléphone", "Origine du téléphone",
           "E-mail", "Statut e-mail", "Adresse", "Code postal", "Ville",
           "SIRET", "Statut SIRENE", "Niveau"]

LISEZ_MOI = [
    ("Fichier", "Producteurs agricoles (hors élevage) — liste d'appel, département {dept}, générée le {today} (version {out})."),
    ("Onglet Sûr", "Une ligne par exploitation avec un téléphone issu d'une source identifiée (déclaré par "
                   "l'exploitant à l'Agence Bio ou sur un annuaire de vente directe, OpenStreetMap, Pages Jaunes, "
                   "Bienvenue à la ferme, le site de l'exploitation, ou deux sources indépendantes concordantes) et un "
                   "établissement actif au registre SIRENE à la date du fichier. C'est la liste à appeler en premier."),
    ("Onglet Probable", "Producteurs inscrits sur un annuaire de vente directe avec un téléphone, mais que le registre "
                        "SIRENE n'a pas permis d'identifier (pas de SIRET). Vérifier l'identité en début d'appel."),
    ("Pas de doublon", "Aucune ligne de ce fichier ne figure dans les fichiers déjà envoyés : les SIRET et les numéros "
                       "déjà transmis ont été retirés. Les éleveurs ne sont pas dans ce fichier (fichiers éleveurs)."),
    ("Exclus", "Sans téléphone ; déjà envoyés ; organismes publics (lycées agricoles, communes, associations) ; "
               "liquidateur nommé au registre ; viticulture, cidre, brasserie, charcuterie et élevage porcin (hors périmètre)."),
    ("Entreprise", "Enseigne quand le registre en a une, sinon raison sociale (GAEC, EARL, nom de l'exploitant)."),
    ("Sous-segment", "D'après le code d'activité du registre et les annuaires : grandes cultures, maraîcher, arboriculteur, "
                     "plantes aromatiques, pépiniériste, fromager, laiterie, transformation, travaux agricoles, aquaculture… "
                     "Plusieurs valeurs possibles, séparées par |."),
    ("Bio", "oui = exploitation certifiée en agriculture biologique (Agence Bio)."),
    ("Contact", "Gérant / exploitant inscrit au registre, sinon la personne nommée par l'annuaire. Vide = inconnu."),
    ("Téléphone", "Numéro à composer, format 0X XX XX XX XX. Jamais de numéro surtaxé. Un numéro n'apparaît qu'une fois."),
    ("Origine du téléphone", "La source du numéro ; « confirmé par … » = une seconde source donne le même numéro."),
    ("E-mail / Statut", "vérifié = le serveur de messagerie a confirmé la boîte ; déclaré par l'exploitant = donné "
                        "par l'exploitation (Agence Bio, annuaire) ; non vérifié = trouvé sur le web, non confirmé. "
                        "Les adresses prouvées fausses ne sont pas dans le fichier."),
    ("SIRET / Statut SIRENE", "Identifiant de l'établissement (texte). actif = établissement actif au registre à la "
                              "date du fichier. non retrouvé = onglet Probable, pas de SIRET."),
    ("Niveau", "Sûr ou Probable, la règle ci-dessus."),
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m7_s14")


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v if v is not None else "")).strip()


def phone_source_fr(src: str) -> str:
    base = src.split("(")[0]
    if base == "corrobore":
        inside = src[src.index("(") + 1:].rstrip(")") if "(" in src else ""
        return "2 sources indépendantes concordantes" + (f" ({inside})" if inside else "")
    return PHONE_SOURCE_FR.get(base, src)


def email_status_fr(statut: str) -> str:
    return {"valide": "vérifié", "risque": "risqué (le domaine accepte tout)", "declare": "déclaré par l'exploitant",
            "non verifie": "non vérifié"}.get(statut, "non vérifié")


SENT_SIRETS, SENT_PHONES = load_sent()


def also_exclude(paths: list[str]) -> None:
    """Sheets built today but not yet registered (the éleveurs V3 sheets) count
    as sent: a phone must never sit on two sheets Maha receives together."""
    for p in paths:
        p = Path(p)
        if not p.exists():
            sys.exit(f"--also-exclude: {p} not found")
        from openpyxl import load_workbook
        wb = load_workbook(p, read_only=True)
        n = 0
        for ws in wb.worksheets:
            it = ws.iter_rows(values_only=True)
            header = [str(h or "").lower() for h in next(it, [])]
            if "téléphone" not in header:
                continue
            ti, si = header.index("téléphone"), (header.index("siret") if "siret" in header else None)
            for vals in it:
                d = phone_digits(str(vals[ti] or ""))
                if d:
                    SENT_PHONES.add(d)
                    n += 1
                if si is not None and vals[si]:
                    SENT_SIRETS.add(str(vals[si]))
        log.info(f"--also-exclude {p.name}: {n} phones added to the never-twice list")


def build(dept: str, version: str) -> tuple[list[dict], Counter]:
    src = OUT_DIR / f"producteurs_{dept}_{version}.csv"
    if not src.exists():
        sys.exit(f"{src} missing — run m7_s9_export.py --version {version} first.")
    rows_in = read_csv(src, delim=",")
    sans_in = read_csv(OUT_DIR / f"producteurs_{dept}_{version}_sans_siret.csv", delim=",")
    stats = Counter(rows_in=len(rows_in), sans_in=len(sans_in))
    rows_in.sort(key=lambda r: (0 if r["telephone_final"] else 1, 0 if r.get("est_siege") == "Oui" else 1,
                                r["codePostal"], r["raisonSociale"]))
    kept: dict[str, dict] = {}
    phones_used: set = set()
    for r in rows_in:
        if r.get("population_source", "registre") != "registre":
            stats["dropped: éleveurs row (Maha has the éleveurs files)"] += 1
            continue
        provider_only = False
        tel, tel_src = r["telephone_final"], r["source_telephone"]
        if not tel and r.get("provider_phone"):
            # the client's own file, 73 % measured: dialled, but on the Probable tab (M6 rule, T20)
            tel, tel_src, provider_only = r["provider_phone"], "provider", True
        if not tel:
            stats["dropped: no phone"] += 1
            continue
        if r["deja_envoye"] or (len(r["siret"]) == 14 and r["siret"] in SENT_SIRETS):
            stats["dropped: already sent to Maha (siret)"] += 1
            continue
        if phone_digits(tel) in SENT_PHONES:
            stats["dropped: already sent to Maha (telephone)"] += 1
            continue
        if r["flag_public"] == "1":
            stats["dropped: public body"] += 1
            continue
        if r["procedure_collective"]:
            stats["dropped: liquidation named in the registry"] += 1
            continue
        if not clean(r["raisonSociale"]):
            if clean(r["gerant"]):
                r["raisonSociale"] = r["gerant"]
                stats["entreprise = contact name (no company name in the source)"] += 1
            else:
                stats["dropped: no company name and no contact name"] += 1
                continue
        if is_surtaxe(tel) or not plausible_fr_number(tel):
            stats["dropped: surtaxé / implausible number"] += 1
            continue
        d = phone_digits(tel)
        if d in phones_used:
            stats["merged: phone already on the sheet (one farm = one call)"] += 1
            continue
        key = r["siret"] if len(r["siret"]) == 14 else (f"X{r['siren']}" if r["siren"] else f"P{d}")
        if key in kept:
            stats["merged: duplicate SIRET"] += 1
            continue
        niveau = "Sûr" if (r["statut_sirene"].startswith("actif") and not provider_only) else "Probable"
        contact = clean(r["gerant"]) if (r.get("nom") or "").strip() else (
            clean(r.get("contact_directory", "")) or clean(r.get("provider_nom", ""))
            or (f"(gérée par {clean(r['gerant'])})" if r["gerant"] else ""))
        origin = phone_source_fr(tel_src)
        if r["telephone_confirme_par"] and not provider_only:
            origin += f" — confirmé par {r['telephone_confirme_par']}"
        stats["kept: provider-only phone (Probable)"] += provider_only
        phones_used.add(d)
        kept[key] = {
            "Entreprise": clean(r["raisonSociale"]), "Sous-segment": r["sous_segment"] or "non précisé",
            "Bio": "oui" if r["bio"] == "1" else "",
            "Contact": contact, "Téléphone": tel, "Origine du téléphone": origin,
            "E-mail": clean(r["email_final"]),
            "Statut e-mail": email_status_fr(r["email_statut"]) if r["email_final"] else "",
            "Adresse": clean(r["adresse"]), "Code postal": clean(r["codePostal"]), "Ville": clean(r["ville"]),
            "SIRET": r["siret"], "Statut SIRENE": r["statut_sirene"], "Niveau": niveau,
        }
        stats[f"kept: {niveau}"] += 1
    # the Sans SIRET rows -> Probable
    for s in sans_in:
        tel = s["phone"] or s["mobile"]
        if not tel:
            stats["sans siret dropped: no phone"] += 1
            continue
        if EXCLUDED_LOOSE_RE.search(f"{s['name']} {s['sous_segment']} {s['description']}"):
            stats["sans siret dropped: principle"] += 1
            continue
        if is_surtaxe(tel) or not plausible_fr_number(tel):
            stats["sans siret dropped: surtaxé / implausible"] += 1
            continue
        d = phone_digits(tel)
        if d in SENT_PHONES:
            stats["sans siret dropped: phone already sent to Maha"] += 1
            continue
        if d in phones_used:
            stats["sans siret merged: phone already on the sheet"] += 1
            continue
        phones_used.add(d)
        kept[f"P{d}"] = {
            "Entreprise": clean(s["name"]), "Sous-segment": s["sous_segment"] or "producteur (annuaire)",
            "Bio": "", "Contact": clean(s["contact"]), "Téléphone": tel,
            "Origine du téléphone": PHONE_SOURCE_FR.get(s["source"], s["source"]),
            "E-mail": clean(s["email"]), "Statut e-mail": email_status_fr(s["email_statut"]) if s["email"] else "",
            "Adresse": clean(s["address"]), "Code postal": clean(s["postcode"]), "Ville": clean(s["city"]),
            "SIRET": "", "Statut SIRENE": "non retrouvé", "Niveau": "Probable",
        }
        stats["kept: Probable (sans SIRET)"] += 1
    out = sorted(kept.values(), key=lambda x: (0 if x["Niveau"] == "Sûr" else 1, x["Code postal"], x["Entreprise"]))
    stats["rows_out"] = len(out)
    return out, stats


def write(rows: list[dict], dept: str, out_version: str) -> Path:
    csv_path = OUT_DIR / f"producteurs_{dept}_teleop_{out_version}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    xlsx_path = OUT_DIR / f"producteurs_{dept}_teleop_{out_version}.xlsx"
    wb = Workbook()
    widths = {"Entreprise": 34, "Sous-segment": 26, "Bio": 5, "Contact": 24, "Téléphone": 16,
              "Origine du téléphone": 44, "E-mail": 32, "Statut e-mail": 24, "Adresse": 30,
              "Code postal": 11, "Ville": 22, "SIRET": 16, "Statut SIRENE": 16, "Niveau": 9}
    first = True
    for niveau in ("Sûr", "Probable"):
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = niveau
        ws.append(COLUMNS)
        for r in rows:
            if r["Niveau"] == niveau:
                ws.append([str(r[c]) if c in ("SIRET", "Code postal") else r[c] for c in COLUMNS])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for i, c in enumerate(COLUMNS, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths[c]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    lm = wb.create_sheet("Lisez-moi")
    lm.column_dimensions["A"].width = 26
    lm.column_dimensions["B"].width = 115
    for k, v in LISEZ_MOI:
        lm.append([k, v.format(dept=dept, today=date.today().strftime("%d/%m/%Y"), out=out_version)])
        lm.cell(row=lm.max_row, column=1).font = Font(bold=True)
        lm.cell(row=lm.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(xlsx_path)
    return xlsx_path


def gate(xlsx_path: Path, dept: str, version: str, expected: int) -> int:
    from openpyxl import load_workbook
    wb = load_workbook(xlsx_path, read_only=True)
    rows = []
    for name in ("Sûr", "Probable"):
        ws = wb[name]
        it = ws.iter_rows(values_only=True)
        header = next(it)
        for vals in it:
            rows.append(dict(zip(header, vals)))
    verified = {}
    for d in (INHERITED_AGRI, INHERITED_ELEVEURS, CHECK_DIR):
        for r in read_csv(d / "verified_emails.csv"):
            verified[r["email"].lower()] = r["verdict"]
    full_rows = read_csv(OUT_DIR / f"producteurs_{dept}_{version}.csv", delim=",")
    full = {r["siret"]: r for r in full_rows if len(r["siret"]) == 14}
    fails = []

    def check(cond, label):
        print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
        if not cond:
            fails.append(label)

    g = lambda r, k: "" if r.get(k) is None else str(r[k])
    print(f"{xlsx_path.name}: {len(rows)} rows ({Counter(g(r,'Niveau') for r in rows)}), {len(COLUMNS)} columns")
    check(len(rows) == expected, f"T1 row count matches the build ({len(rows)} vs {expected})")
    check(all(g(r, "Téléphone") and PHONE_RE.match(g(r, "Téléphone")) for r in rows), "T2 every row has a phone in 0X XX XX XX XX shape")
    check(not any(is_surtaxe(g(r, "Téléphone")) or not plausible_fr_number(g(r, "Téléphone")) for r in rows), "T2 no surtaxé / implausible number")
    check(all(g(r, "Origine du téléphone") for r in rows), "T3 phone provenance always filled")
    sirets = [g(r, "SIRET") for r in rows if g(r, "SIRET")]
    check(len(sirets) == len(set(sirets)), f"T4 SIRET unique ({len(sirets) - len(set(sirets))} repeats)")
    dead = [s for s in sirets if full.get(s, {}).get("procedure_collective") or full.get(s, {}).get("statut_sirene", "").startswith("ferm")]
    check(not dead, f"T5 no liquidation / closed SIRET ({len(dead)})")
    check(all(isinstance(r.get("Code postal"), str) and len(g(r, "Code postal")) == 5 for r in rows)
          and all(isinstance(r.get("SIRET"), str) for r in rows if r.get("SIRET") not in (None, "")),
          "T6 SIRET and CP stored as text, CP 5 chars")
    mails = [g(r, "E-mail").lower() for r in rows if g(r, "E-mail")]
    check(all(EMAIL_RE.match(m) for m in mails), "T7 e-mail shape")
    bad_inv = [m for m in mails if verified.get(m) == "invalide"]
    check(not bad_inv, f"T7 no e-mail verified invalide ({len(bad_inv)})")
    bad_dom = [m for m in mails if (is_aggregator(m.rpartition("@")[2]) and m.rpartition("@")[2] != "gmail.com") or MAIRIE_RE.search(m.rpartition("@")[2])]
    check(not bad_dom, f"T7 no aggregator / mairie mailbox ({len(bad_dom)}) {bad_dom[:3]}")
    check(all(bool(g(r, "E-mail")) == bool(g(r, "Statut e-mail")) for r in rows), "T8 e-mail statut filled iff e-mail filled")
    dom = Counter(m.rpartition("@")[2] for m in mails if m.rpartition("@")[2] not in FREE_MAIL | EXTRA_FREE_MAIL)
    shared = [d for d, n in dom.items() if n > 2]
    check(not shared, f"T9 no corporate domain on > 2 rows ({shared[:3]})")
    bad_flag = [s for s in sirets if full.get(s, {}).get("flag_public") == "1"]
    check(not bad_flag, f"T10 no public body row ({len(bad_flag)})")
    check(all(g(r, "Statut SIRENE") for r in rows), "T11 Statut SIRENE always filled")
    check(all(g(r, "Sous-segment") for r in rows), "T14 Sous-segment filled")
    check(all(g(r, "Entreprise").strip() for r in rows), f"T15 Entreprise never empty ({sum(1 for r in rows if not g(r, 'Entreprise').strip())})")
    dup_sent_s = [s for s in sirets if s in SENT_SIRETS]
    dup_sent_p = [g(r, "Téléphone") for r in rows if phone_digits(g(r, "Téléphone")) in SENT_PHONES]
    check(not dup_sent_s and not dup_sent_p,
          f"T16 no SIRET / phone already in a file sent to Maha ({len(dup_sent_s)} SIRET, {len(dup_sent_p)} phones)")
    check(not [r for r in rows if g(r, "Niveau") == "Sûr" and not g(r, "SIRET")], "T17 every Sûr row has a SIRET")
    ph = Counter(phone_digits(g(r, "Téléphone")) for r in rows)
    check(not [p for p, n in ph.items() if n > 1], f"T18 no phone twice on the sheet ({sum(1 for n in ph.values() if n > 1)})")
    bad_pr = [r for r in rows if EXCLUDED_LOOSE_RE.search(g(r, "Sous-segment")) or
              re.search(r"vigneron|viticult|vignoble|brasserie|cidre|charcuterie|porcin", g(r, "Entreprise"), re.I)]
    check(not bad_pr, f"T19 principle: no wine / beer / pork row ({len(bad_pr)})")
    prov_sur = [r for r in rows if g(r, "Niveau") == "Sûr" and g(r, "Origine du téléphone").startswith("fichier fournisseur")]
    check(not prov_sur, f"T20 no provider-only phone in the Sûr tab ({len(prov_sur)})")
    print(f"  info  Contact filled: {sum(1 for r in rows if g(r, 'Contact'))}/{len(rows)}; e-mail: {len(mails)} "
          f"({Counter(g(r, 'Statut e-mail') for r in rows if g(r, 'E-mail')).most_common()}); bio: {sum(1 for r in rows if g(r, 'Bio'))}; "
          f"sous-segments: {Counter(t for r in rows for t in g(r, 'Sous-segment').split('|')).most_common(8)}")
    print(f"{len(fails)} failed")
    return 1 if fails else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departement", default="63")
    ap.add_argument("--version", default="v1", help="m7_s9 version to read")
    ap.add_argument("--out-version", default="v1")
    ap.add_argument("--also-exclude", nargs="*", default=[],
                    help="xlsx call sheets built today, not yet registered, whose phones / SIRETs must not reappear")
    args = ap.parse_args()

    also_exclude(args.also_exclude)
    rows, stats = build(args.departement, args.version)
    xlsx_path = write(rows, args.departement, args.out_version)
    log.info("-" * 62)
    log.info(f"[{args.departement}] {stats['rows_in']} rows + {stats['sans_in']} sans SIRET in -> {stats['rows_out']} rows out -> {xlsx_path.name} + .csv")
    for k, v in stats.most_common():
        if k not in ("rows_in", "rows_out", "sans_in"):
            log.info(f"  {k:<64} {v:>5}")
    log.info(f"  phone origin: {dict(Counter(r['Origine du téléphone'].split(' —')[0] for r in rows))}")
    log.info("reading the xlsx back:")
    sys.exit(gate(xlsx_path, args.departement, args.version, len(rows)))


if __name__ == "__main__":
    main()
