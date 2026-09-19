"""
M7-S2 — Transform raw registry payloads into the M7 (producteurs) population CSV
===============================================================================
Input : exports/producteurs/checkpoints/api_raw_<dept>.jsonl   (m7_s1)
Output: exports/producteurs/checkpoints/operateurs_<dept>.csv  (COMMA-delimited)

Copy of `m6_s2_transform.py`. ONE ROW PER ACTIVE ÉTABLISSEMENT IN THE
DÉPARTEMENT (the API's `departement=` matches legal units with ANY
établissement there, so the row unit is the établissement).

Column layout = the m3ag_s2 adapter (19 columns, same names, same order)
so the sector-neutral M3AG scripts (m3ag_s7 Pages Jaunes, m3ag_s3 search,
m3ag_s4 crawl, m3ag_s13 SIRENE) run on this file unchanged, then the M6
columns (same names so m6_s8 / m6_s9 logic can be copied), with
`sous_segment` in the slot M6 used for `type_elevage`. Adapter semantics:
raisonSociale = enseigne when the registry has one, else the legal name;
gerant = "Prénom NOM" of the best operator role (m2_s2.pick_dirigeant);
productions = the NAF's official label (the activity CONTEXT Sam asked
for, before any directory adds its own description); flag_hors_agri =
flag_public (institutions never reach a call sheet).

Filters, in order, all counted and reported:
  1. dedup by SIREN (page overlap between NAF queries is expected)
  2. établissement etat_administratif == 'A'
  3. code_postal starts with the dept
  4. activity guard: the établissement's own activite_principale must be in
     m7_lib.NAF_SCOPE (falls back to the legal unit's code when absent)
  5. --drop-naf: codes Ines removed after reading the m7_s1 report

Flags, never drops: sous_segment (from the NAF), flag_public,
procedure_collective ("Liquidation" when a Liquidateur sits among the
officers while the état still reads 'A').

Usage:
    python scripts/m7_s2_transform.py --departements 03,63
    python scripts/m7_s2_transform.py --departements 03,63 --drop-naf 01.11Z
"""

import argparse
import csv
import gzip
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
from m7_lib import (CHECK_DIR, DEPARTEMENTS, NAF_SCOPE, NAF_LABELS, EXCLUDED_NAF,  # noqa: E402
                    tag_from_naf, is_public)
from france_lib import in_dept                                    # noqa: E402

# m3ag_s2.COLUMNS verbatim, then the M6 column names (sous_segment where M6
# had type_elevage) so the M6 scripts can be copied with a rename.
ADAPTER_COLUMNS = ["raisonSociale", "siret", "gerant", "telephone", "telephoneCommerciale",
                   "codeNAF", "siteWebs", "categories", "productions",
                   "adresse", "codePostal", "ville", "lat", "lon",
                   "email", "numeroBio", "activites", "flag_hors_agri", "flag_hors_dept"]
M7_COLUMNS = ["siren", "denomination_legale", "enseigne", "sous_segment",
              "forme_juridique", "nature_juridique", "flag_public",
              "procedure_collective", "date_creation", "tranche_effectif", "est_siege",
              "prenom", "nom", "fonction", "source_population"]
COLUMNS = ADAPTER_COLUMNS + M7_COLUMNS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m7_s2")


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v)).strip() if v else ""


def run_dept(dept: str, drop_naf: set) -> None:
    raw = CHECK_DIR / f"api_raw_{dept}.jsonl"
    out = CHECK_DIR / f"operateurs_{dept}.csv"
    # The France driver gzips the raw once its transform has run (~8x on 96
    # départements), so a re-run must be able to read the .gz — otherwise the
    # pipeline stops being re-runnable the moment it has been tidied up, which
    # is exactly how dept 2A failed its repair pass on 2026-09-19.
    gz = CHECK_DIR / f"api_raw_{dept}.jsonl.gz"
    # A ZERO-BYTE .jsonl must never win over a real .gz. Dept 2A ended up
    # with an empty one beside its gzipped data and the transform happily
    # read it, reporting "0 legal units" instead of failing — the
    # silent-emptiness family again. Size, not existence, decides.
    have_raw = raw.exists() and raw.stat().st_size > 0
    have_gz = gz.exists() and gz.stat().st_size > 0
    if not have_raw and not have_gz:
        sys.exit(f"[{dept}] {raw} missing — run m7_s1_acquire.py --departements {dept} first.")

    units: dict = {}
    if have_raw:
        opener = lambda: raw.open(encoding="utf-8")              # noqa: E731
    else:
        opener = lambda: gzip.open(gz, "rt", encoding="utf-8")   # noqa: E731
    with opener() as f:
        for line in f:
            u = json.loads(line)
            units[u.get("siren")] = u          # last occurrence wins (freshest fetch)

    rows, seen_siret = [], set()
    c = Counter(units=len(units))
    naf_rejected, seg_split, forme_split = Counter(), Counter(), Counter()

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
            if naf in EXCLUDED_NAF:
                c["dropped: excluded on principle"] += 1
                continue
            if naf not in NAF_SCOPE:
                c["dropped: activity guard"] += 1
                naf_rejected[naf] += 1
                continue
            if naf in drop_naf:
                c["dropped: --drop-naf"] += 1
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
            seg = tag_from_naf(naf)
            effectif = e.get("tranche_effectif_salarie") or unit.get("tranche_effectif_salarie")
            adresse = clean(e.get("adresse"))
            if cp and cp in adresse:
                adresse = adresse.split(cp)[0].strip()
            gerant = " ".join(x for x in (clean(prenom), clean(nom)) if x) or clean(contact_pm)

            seg_split[seg] += 1
            forme_split[forme or "?"] += 1
            if public:
                c["flag_public"] += 1
            if in_liquidation:
                c["flag_liquidation"] += 1
            if gerant:
                c["with_gerant"] += 1

            rows.append({
                "raisonSociale": display, "siret": siret, "gerant": gerant,
                "telephone": "", "telephoneCommerciale": "", "codeNAF": naf,
                "siteWebs": "", "categories": NAF_LABELS.get(naf, naf), "productions": NAF_LABELS.get(naf, naf),
                "adresse": adresse, "codePostal": cp, "ville": clean(e.get("libelle_commune")),
                "lat": e.get("latitude") or "", "lon": e.get("longitude") or "",
                "email": "", "numeroBio": "", "activites": "",
                "flag_hors_agri": int(public), "flag_hors_dept": 0,
                "siren": siren, "denomination_legale": legal_name, "enseigne": enseigne,
                "sous_segment": seg, "forme_juridique": clean(forme), "nature_juridique": nj,
                "flag_public": int(public),
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
    for k in ("dropped: closed etab", "dropped: outside dept", "dropped: excluded on principle",
              "dropped: activity guard", "dropped: --drop-naf", "dropped: duplicate siret"):
        log.info(f"[{dept}]   {k:<32} {c[k]:>5}")
    log.info(f"[{dept}]   activity-guard rate {guard_rate:.1%}  top rejected NAF {naf_rejected.most_common(5)}")
    log.info(f"[{dept}]   by NAF   {dict(Counter(r['codeNAF'] for r in rows).most_common())}")
    log.info(f"[{dept}]   by sous-segment {dict(seg_split.most_common())}")
    log.info(f"[{dept}]   by forme {forme_split.most_common(8)}")
    log.info(f"[{dept}]   public {c['flag_public']}   liquidation flagged {c['flag_liquidation']}"
             f"   gérant named {c['with_gerant']} ({c['with_gerant']/max(1,kept):.0%})"
             f"   geocoded {sum(1 for r in rows if r['lat'])}")
    if guard_rate > 0.10:
        log.warning(f"[{dept}] activity-guard rejections exceed 10% — read the rejected NAF list before trusting the scope.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default=",".join(DEPARTEMENTS))
    ap.add_argument("--drop-naf", default="", help="comma-separated NAF codes to leave out (Ines's call)")
    args = ap.parse_args()
    drop = {n.strip() for n in args.drop_naf.split(",") if n.strip()}
    for dept in [d.strip() for d in args.departements.split(",") if d.strip()]:
        run_dept(dept, drop)


if __name__ == "__main__":
    main()
