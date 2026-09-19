"""
M6-S2 — Transform raw registry payloads into the M6 (éleveurs) population CSV
============================================================================
Input : exports/eleveurs/checkpoints/api_raw_<dept>.jsonl   (m6_s1)
Output: exports/eleveurs/checkpoints/operateurs_<dept>.csv  (COMMA-delimited)

ONE ROW PER ACTIVE ÉTABLISSEMENT IN THE DÉPARTEMENT. `departement=` on the
API matches legal units with ANY établissement there — the siège can be
elsewhere (M2 measured it) — so the row unit is the établissement.

Column layout = the m3ag_s2 adapter (19 columns, same names, same order) so
the sector-neutral M3AG scripts (m3ag_s7 Pages Jaunes, m3ag_s3 search,
m3ag_s4 crawl, m3ag_s13 SIRENE) run on this file unchanged; M6's own
columns are APPENDED. Adapter semantics: raisonSociale = enseigne when the
registry has one, else the legal name (directories print the trade name);
gerant = "Prénom NOM" of the best operator role (m2_s2.pick_dirigeant:
gérant > président > exploitant > DG; auditors never); productions =
type_elevage; flag_hors_agri = flag_public OR flag_animaux_compagnie so the
inherited filters keep institutions and pet breeders out of the call sheet;
numeroBio stays empty (filled by the Agence Bio match, m6_s8);
telephone/email/siteWebs are owner-declared slots the registry cannot fill.

Filters, in order, all counted and reported:
  1. dedup by SIREN (page overlap between NAF queries is expected)
  2. établissement etat_administratif == 'A'
  3. code_postal starts with the dept
  4. activity guard: the établissement's own activite_principale must be in
     m6_lib.NAF_SCOPE (falls back to the legal unit's code when absent)

Flags, never drops:
  * type_elevage — from the NAF (bovins lait / bovins viande / équins /
    ovins-caprins / porcins / volailles / autres animaux / polyculture-élevage).
  * flag_animaux_compagnie — dog / cat / pet trade words in the name
    (NAF 01.49Z covers kennels; M1 measured 1,714 such rows nationally).
  * flag_public — nature juridique 7xxx or an institution name (commune,
    lycée agricole, INRAE, chambre d'agriculture, association).
  * procedure_collective — "Liquidation" when a Liquidateur sits among the
    officers while the état still reads 'A' (M2/M1 rule).

Usage:
    python scripts/m6_s2_transform.py --departements 03,63
"""

import argparse
import csv
import json
import logging
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts.m1_s4_sirene_enrich import LEGAL_FORM_CODES          # noqa: E402
from scripts.m1_s8_export import EFFECTIF_LABEL, ILLEGAL_XML      # noqa: E402
from m2_s2_transform import pick_dirigeant                        # noqa: E402
from m6_lib import (CHECK_DIR, DEPARTEMENTS, NAF_SCOPE, NAF_LABELS,  # noqa: E402
                    classify_type, is_public, is_pet_trade)
from france_lib import in_dept                                    # noqa: E402

# m3ag_s2.COLUMNS verbatim, then the M6 columns.
ADAPTER_COLUMNS = ["raisonSociale", "siret", "gerant", "telephone", "telephoneCommerciale",
                   "codeNAF", "siteWebs", "categories", "productions",
                   "adresse", "codePostal", "ville", "lat", "lon",
                   "email", "numeroBio", "activites", "flag_hors_agri", "flag_hors_dept"]
M6_COLUMNS = ["siren", "denomination_legale", "enseigne", "type_elevage",
              "forme_juridique", "nature_juridique", "flag_public", "flag_animaux_compagnie",
              "procedure_collective", "date_creation", "tranche_effectif", "est_siege",
              "prenom", "nom", "fonction", "source_population"]
COLUMNS = ADAPTER_COLUMNS + M6_COLUMNS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m6_s2")


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v)).strip() if v else ""


def run_dept(dept: str) -> None:
    raw = CHECK_DIR / f"api_raw_{dept}.jsonl"
    out = CHECK_DIR / f"operateurs_{dept}.csv"
    if not raw.exists():
        sys.exit(f"[{dept}] {raw} missing — run m6_s1_acquire.py --departements {dept} first.")

    units: dict = {}
    with raw.open(encoding="utf-8") as f:
        for line in f:
            u = json.loads(line)
            units[u.get("siren")] = u          # last occurrence wins (freshest fetch)

    rows, seen_siret = [], set()
    c = Counter(units=len(units))
    naf_rejected, type_split, forme_split = Counter(), Counter(), Counter()

    for siren, unit in units.items():
        unit_naf = unit.get("activite_principale")
        dirigeants = unit.get("dirigeants") or []
        in_liquidation = any("liquidateur" in (d.get("qualite") or "").lower() for d in dirigeants)
        prenom, nom, fonction, contact_pm = pick_dirigeant(dirigeants)
        nj = unit.get("nature_juridique") or ""
        forme = LEGAL_FORM_CODES.get(nj, nj)
        legal_name = clean(unit.get("nom_raison_sociale") or unit.get("nom_complet"))
        dc = unit.get("date_creation") or ""
        if dc == "1900-01-01":
            dc = ""

        for e in unit.get("matching_etablissements") or []:
            c["etabs_seen"] += 1
            if e.get("etat_administratif") != "A":
                c["dropped: closed etab"] += 1
                continue
            cp = e.get("code_postal") or ""
            if not in_dept(cp, dept):      # not startswith: 20190 is in 2A, not in "20"
                c["dropped: outside dept"] += 1
                continue
            naf = e.get("activite_principale") or unit_naf
            if naf not in NAF_SCOPE:
                c["dropped: activity guard"] += 1
                naf_rejected[naf] += 1
                continue
            siret = e.get("siret")
            if siret in seen_siret:
                c["dropped: duplicate siret"] += 1
                continue
            seen_siret.add(siret)

            enseignes = [x for x in (e.get("liste_enseignes") or []) if x]
            enseigne = clean(enseignes[0] if enseignes else (e.get("nom_commercial") or ""))
            display = enseigne or legal_name
            public = is_public(nj, legal_name, enseigne)
            pet = is_pet_trade(legal_name, enseigne, naf=naf)
            typ = classify_type(naf, legal_name, enseigne)
            effectif = e.get("tranche_effectif_salarie") or unit.get("tranche_effectif_salarie")
            adresse = clean(e.get("adresse"))
            # the registry prints "<street> <CP> <commune>"; keep the street part
            if cp and cp in adresse:
                adresse = adresse.split(cp)[0].strip()
            gerant = " ".join(x for x in (clean(prenom), clean(nom)) if x) or clean(contact_pm)

            type_split[typ] += 1
            forme_split[forme or "?"] += 1
            if public:
                c["flag_public"] += 1
            if pet:
                c["flag_animaux_compagnie"] += 1
            if in_liquidation:
                c["flag_liquidation"] += 1
            if gerant:
                c["with_gerant"] += 1

            rows.append({
                "raisonSociale": display, "siret": siret, "gerant": gerant,
                "telephone": "", "telephoneCommerciale": "", "codeNAF": naf,
                "siteWebs": "", "categories": NAF_LABELS.get(naf, naf), "productions": typ,
                "adresse": adresse, "codePostal": cp, "ville": clean(e.get("libelle_commune")),
                "lat": e.get("latitude") or "", "lon": e.get("longitude") or "",
                "email": "", "numeroBio": "", "activites": "",
                "flag_hors_agri": int(public or pet), "flag_hors_dept": 0,
                "siren": siren, "denomination_legale": legal_name, "enseigne": enseigne,
                "type_elevage": typ, "forme_juridique": clean(forme), "nature_juridique": nj,
                "flag_public": int(public), "flag_animaux_compagnie": int(pet),
                "procedure_collective": "Liquidation" if in_liquidation else "",
                "date_creation": dc, "tranche_effectif": EFFECTIF_LABEL.get(effectif or "NN", ""),
                "est_siege": "Oui" if e.get("est_siege") else "Non",
                "prenom": clean(prenom), "nom": clean(nom), "fonction": clean(fonction),
                "source_population": "registre",
            })

    rows.sort(key=lambda r: (r["codePostal"], r["raisonSociale"], r["siret"]))
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)      # comma: the m3ag_s2 contract
        w.writeheader()
        w.writerows(rows)

    kept = len(rows)
    guard_rate = c["dropped: activity guard"] / max(1, c["dropped: activity guard"] + kept)
    log.info("─" * 62)
    log.info(f"[{dept}] {c['units']} legal units, {c['etabs_seen']} établissements -> written={kept} -> {out.name}")
    for k in ("dropped: closed etab", "dropped: outside dept", "dropped: activity guard", "dropped: duplicate siret"):
        log.info(f"[{dept}]   {k:<32} {c[k]:>5}")
    log.info(f"[{dept}]   activity-guard rate {guard_rate:.1%}  top rejected NAF {naf_rejected.most_common(5)}")
    log.info(f"[{dept}]   by NAF   {dict(Counter(r['codeNAF'] for r in rows))}")
    log.info(f"[{dept}]   by type  {dict(type_split.most_common())}")
    log.info(f"[{dept}]   by forme {forme_split.most_common(8)}")
    log.info(f"[{dept}]   public {c['flag_public']}   animaux de compagnie {c['flag_animaux_compagnie']}"
             f"   liquidation flagged {c['flag_liquidation']}"
             f"   gérant named {c['with_gerant']} ({c['with_gerant']/max(1,kept):.0%})"
             f"   geocoded {sum(1 for r in rows if r['lat'])}")
    if guard_rate > 0.10:
        log.warning(f"[{dept}] activity-guard rejections exceed 10% — read the rejected NAF list before trusting the scope.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default=",".join(DEPARTEMENTS))
    args = ap.parse_args()
    for dept in [d.strip() for d in args.departements.split(",") if d.strip()]:
        run_dept(dept)


if __name__ == "__main__":
    main()
