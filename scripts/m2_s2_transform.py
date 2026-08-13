"""
M2-S2 — Transform raw registry payloads into the establishment-grained CSV
==========================================================================
Reads exports/boulangerie/checkpoints/api_raw.jsonl (written by m2_s1) and
emits ONE ROW PER ACTIVE ETABLISSEMENT IN DEPT 13:

    exports/boulangerie/checkpoints/etablissements.csv   (utf-8-sig, ';')

Why établissement-grained: `departement=13` on the API matches legal units
with ANY établissement in dept 13 — the siège can be in another département
entirely (observed: 62, 83). The CEO's list is places with ovens in the
Bouches-du-Rhône, so the row unit is the établissement, never the siège.

Filters, in order, all counted and reported:
  1. dedup by SIREN (page overlap between runs is expected and harmless)
  2. établissement etat_administratif == 'A'
  3. code_postal starts with '13' (dept 13; CPs 13001-13990)
  4. activity guard: the établissement's own activite_principale must be in
     {10.71C, 10.71B} when present (falls back to the legal unit's code when
     absent). The API filters at the legal-unit level, so an in-scope
     company's dept-13 outlet can be e.g. a 47.24Z shop — out of scope.

Dirigeant selection (Sam's step 3, from the same payload):
  personnes physiques only, ranked by operator role — gérant > président >
  exploitant > directeur général > associé > anything else. Commissaires aux
  comptes (auditors) are never selected: they are the company's accountant's
  counterparty, not the person who buys ovens. Personne-morale-only units get
  the PM's denomination in the Contact column instead.

No DB. Pure file → file. Deterministic: sorted by (CP, name, SIRET).

Usage:
    python scripts/m2_s2_transform.py
"""

import csv
import json
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.m1_s4_sirene_enrich import LEGAL_FORM_CODES          # noqa: E402
from scripts.m1_s8_export import EFFECTIF_LABEL, ILLEGAL_XML      # noqa: E402

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
RAW_PATH  = CHECK_DIR / "api_raw.jsonl"
OUT_PATH  = CHECK_DIR / "etablissements.csv"

NAF_SCOPE = {"10.71C", "10.71B", "10.71D"}
NAF_LABELS = {
    "10.71C": "Boulangerie et boulangerie-patisserie",
    "10.71B": "Cuisson de produits de boulangerie (terminal)",
    "10.71D": "Patisserie",
}

# Operator roles, most decision-making first. Matched as lowercase substrings
# of `qualite`. Auditors are excluded before ranking ever happens.
ROLE_RANK = ["gérant", "gerant", "président", "president", "exploitant",
             "directeur général", "directeur general", "directeur", "associé", "associe"]
ROLE_EXCLUDE = ("commissaire aux comptes",)

FIELDNAMES = [
    "siret", "siren", "raison_sociale", "enseigne", "est_siege",
    "procedure_collective",
    "forme_juridique", "date_creation", "anciennete_ans", "tranche_effectif",
    "naf_code", "activite", "adresse", "code_postal", "commune",
    "prenom", "nom", "fonction", "contact",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s2")


def pick_dirigeant(dirigeants: list) -> tuple[str, str, str, str]:
    """Return (prenom, nom, fonction, contact_fallback)."""
    pps = [d for d in dirigeants or []
           if d.get("type_dirigeant") == "personne physique"
           and not any(x in (d.get("qualite") or "").lower() for x in ROLE_EXCLUDE)]

    def rank(d):
        q = (d.get("qualite") or "").lower()
        for i, role in enumerate(ROLE_RANK):
            if role in q:
                return i
        return len(ROLE_RANK)

    if pps:
        best = min(pps, key=rank)
        prenoms = (best.get("prenoms") or "").strip()
        prenom = prenoms.split(" ")[0].title() if prenoms else ""
        nom = (best.get("nom") or "").strip().upper()
        fonction = (best.get("qualite") or "").strip()
        return prenom, nom, fonction, ""

    pms = [d for d in dirigeants or []
           if d.get("type_dirigeant") != "personne physique"
           and not any(x in (d.get("qualite") or "").lower() for x in ROLE_EXCLUDE)]
    if pms:
        return "", "", "", (pms[0].get("denomination") or "").strip()
    return "", "", "", ""


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v)).strip() if v else ""


def main() -> None:
    if not RAW_PATH.exists():
        sys.exit(f"{RAW_PATH} not found — run scripts/m2_s1_acquire.py first.")

    seen_siren, seen_siret = set(), set()
    rows = []
    n_units = n_dup_siren = n_etabs_total = 0
    n_closed = n_not_13 = n_naf_rejected = n_dup_siret = 0
    naf_rejected = Counter()
    naf_split = Counter()
    n_hidden_addr = 0

    with RAW_PATH.open(encoding="utf-8") as f:
        for line in f:
            unit = json.loads(line)
            siren = unit.get("siren")
            if siren in seen_siren:
                n_dup_siren += 1
                continue
            seen_siren.add(siren)
            n_units += 1

            unit_naf = unit.get("activite_principale")
            dirigeants = unit.get("dirigeants") or []
            # A "Liquidateur" among the officers is the registry saying the
            # company is being wound up — independent of etat_administratif,
            # which can still read 'A' throughout a liquidation. Agriculture
            # found the same signal (migration 017) with ZERO overlap with the
            # provider's own closed flag. Flag it, never silently drop it:
            # the decision to mail is the client's, the disclosure is ours.
            in_liquidation = any("liquidateur" in (d.get("qualite") or "").lower()
                                 for d in dirigeants)
            prenom, nom, fonction, contact = pick_dirigeant(dirigeants)
            forme = LEGAL_FORM_CODES.get(unit.get("nature_juridique"),
                                         unit.get("nature_juridique") or "")
            dc = unit.get("date_creation")
            anciennete = ""
            if dc and dc != "1900-01-01":
                try:
                    anciennete = str(date.today().year - int(dc[:4]))
                except ValueError:
                    dc = None

            for e in unit.get("matching_etablissements") or []:
                n_etabs_total += 1
                if e.get("etat_administratif") != "A":
                    n_closed += 1
                    continue
                cp = (e.get("code_postal") or "")
                if not cp.startswith("13"):
                    n_not_13 += 1
                    continue
                # Activity guard: the unit passed the API filter, the outlet
                # itself may not be a bakery (e.g. a 47.24Z shop of a 10.71C
                # company). The établissement's own code wins when present.
                naf = e.get("activite_principale") or unit_naf
                if naf not in NAF_SCOPE:
                    n_naf_rejected += 1
                    naf_rejected[naf] += 1
                    continue
                siret = e.get("siret")
                if siret in seen_siret:
                    n_dup_siret += 1
                    continue
                seen_siret.add(siret)
                naf_split[naf] += 1

                enseignes = [x for x in (e.get("liste_enseignes") or []) if x]
                enseigne = enseignes[0] if enseignes else (e.get("nom_commercial") or "")
                adresse = clean(e.get("adresse"))
                if not adresse or "[ND]" in adresse:
                    n_hidden_addr += 1
                effectif = e.get("tranche_effectif_salarie") or unit.get("tranche_effectif_salarie")

                rows.append({
                    "siret": siret,
                    "siren": siren,
                    "raison_sociale": clean(unit.get("nom_raison_sociale") or unit.get("nom_complet")),
                    "enseigne": clean(enseigne),
                    "est_siege": "Oui" if e.get("est_siege") else "Non",
                    "procedure_collective": "Liquidation" if in_liquidation else "",
                    "forme_juridique": clean(forme),
                    "date_creation": (f"{dc[8:10]}/{dc[5:7]}/{dc[:4]}" if dc and dc != "1900-01-01" else ""),
                    "anciennete_ans": anciennete,
                    "tranche_effectif": EFFECTIF_LABEL.get(effectif or "NN", ""),
                    "naf_code": naf,
                    "activite": NAF_LABELS[naf],
                    "adresse": adresse,
                    "code_postal": cp,
                    "commune": clean(e.get("libelle_commune")),
                    "prenom": clean(prenom),
                    "nom": clean(nom),
                    "fonction": clean(fonction),
                    "contact": clean(contact),
                })

    rows.sort(key=lambda r: (r["code_postal"], r["raison_sociale"], r["siret"]))

    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    kept = len(rows)
    with_name = sum(1 for r in rows if r["nom"])
    with_contact_any = sum(1 for r in rows if r["nom"] or r["contact"])
    with_effectif = sum(1 for r in rows if r["tranche_effectif"])
    log.info(f"legal units read: {n_units} (+{n_dup_siren} duplicate SIRENs skipped)")
    log.info(f"etablissements seen: {n_etabs_total} -> kept {kept}")
    log.info(f"  filtered: {n_closed} closed, {n_not_13} outside dept 13, "
             f"{n_naf_rejected} activity-guard rejections, {n_dup_siret} duplicate SIRETs")
    guard_rate = n_naf_rejected / max(1, n_naf_rejected + kept)
    log.info(f"  activity-guard rejection rate: {guard_rate:.1%} "
             f"(top out-of-scope NAF: {naf_rejected.most_common(5)})")
    log.info(f"NAF split of kept rows: {dict(naf_split)}")
    log.info(f"dirigeant (personne) coverage: {with_name}/{kept} ({with_name/max(1,kept):.1%}); "
             f"any contact incl. personne morale: {with_contact_any}")
    n_liq = sum(1 for r in rows if r["procedure_collective"])
    log.info(f"effectif coverage: {with_effectif}; hidden/empty addresses: {n_hidden_addr}")
    log.info(f"flagged in liquidation (Liquidateur among officers): {n_liq} "
             "— shipped WITH the flag, not excluded; mailing them is the client's call")
    log.info(f"written={kept} rows -> {OUT_PATH}")
    if guard_rate > 0.10:
        log.warning("Activity-guard rejections exceed 10% — revisit scope with "
                    "StockEtablissement CSV as per plan before shipping.")


if __name__ == "__main__":
    main()
