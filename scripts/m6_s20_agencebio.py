"""
M6-S20 — Agence Bio operators as a listing source for the éleveurs population
=============================================================================
The M3AG population files (`exports/agriculteurs/checkpoints/operateurs_<dept>.csv`,
one row per certified-organic operator of dept 63 / 03) carry the phone,
e-mail and website the operator DECLARED to Agence Bio, its SIRET, its
numéro bio and its production list. For M6 they are a witness: an operator
whose SIRET is in the éleveurs population brings an owner-declared phone
(rank 1 in m6_s9, as in m3ag_s9), a `Bio` flag and the productions.

Read in place, never copied. Rows flagged `flag_hors_agri=1` (shops,
distributors, associations) are skipped: they are not farms.

Output: exports/eleveurs/checkpoints/agencebio_listings.csv  (';')

Usage:
    python scripts/m6_s20_agencebio.py
"""

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone, is_surtaxe   # noqa: E402
from m6_lib import CHECK_DIR, INHERITED_DIR, DEPARTEMENTS  # noqa: E402
from france_lib import in_dept, parse_departements         # noqa: E402

OUT_PATH = CHECK_DIR / "agencebio_listings.csv"
FIELDS = ["dept", "listing_id", "name", "siret", "dirigeant", "phone", "mobile", "email",
          "website", "address", "postcode", "city", "lat", "lon", "naf", "productions",
          "activites", "url"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m6_s20")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default=",".join(DEPARTEMENTS),
                    help="comma list, a région name, or 'all' for the 96 of métropole")
    args = ap.parse_args()
    depts = parse_departements(args.departements)
    verbose = len(depts) <= 6

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    rows, skipped, missing = [], Counter(), []
    for dept in depts:
        src = INHERITED_DIR / f"operateurs_{dept}.csv"
        if not src.exists():
            missing.append(dept)
            if verbose:
                log.warning(f"[{dept}] {src} missing — M3AG population not built for this dept, skipped")
            continue
        with src.open(encoding="utf-8-sig", newline="") as fh:
            ops = list(csv.DictReader(fh))
        for op in ops:
            if op.get("flag_hors_agri") == "1":
                skipped["hors_agri"] += 1
                continue
            if not in_dept(op.get("codePostal", ""), dept):
                skipped["outside dept"] += 1
                continue
            phones = [normalize_fr_phone(p) for p in (op.get("telephone"), op.get("telephoneCommerciale")) if p]
            phones = [p for p in dict.fromkeys(phones) if p and not is_surtaxe(p)]
            land = [p for p in phones if not p.startswith(("06", "07"))]
            mob = [p for p in phones if p.startswith(("06", "07"))]
            email = (op.get("email") or "").strip().lower()
            site = (op.get("siteWebs") or "").split(";")[0].strip()
            if not (phones or email or site):
                skipped["no contact data"] += 1
                continue
            rows.append({
                "dept": dept, "listing_id": f"NB{op.get('numeroBio', '')}",
                "name": (op.get("raisonSociale") or "").strip(),
                "siret": (op.get("siret") or "").strip(),
                "dirigeant": (op.get("gerant") or "").strip(),
                "phone": land[0] if land else (mob[0] if mob else ""),
                "mobile": (mob[0] if land and mob else (mob[1] if len(mob) > 1 else "")),
                "email": email, "website": site,
                "address": (op.get("adresse") or "").strip(),
                "postcode": op.get("codePostal", ""), "city": (op.get("ville") or "").strip(),
                "lat": op.get("lat", ""), "lon": op.get("lon", ""),
                "naf": op.get("codeNAF", ""), "productions": (op.get("productions") or "")[:400],
                "activites": op.get("activites", ""),
                "url": f"https://annuaire.agencebio.org/fiche/{op.get('numeroBio', '')}" if op.get("numeroBio") else "",
            })
    rows.sort(key=lambda x: (x["dept"], x["postcode"], x["name"]))
    # Written to a .tmp then replaced: during the France run the matchers read
    # this file while it is being refreshed, and a plain open("w") would let one
    # of them see a truncated file and silently lose its Agence Bio witnesses.
    tmp = OUT_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(OUT_PATH)
    log.info("─" * 62)
    log.info(f"written={len(rows)} Agence Bio listings -> {OUT_PATH.name}   skipped {dict(skipped)}")
    if missing:
        log.warning(f"{len(missing)} département(s) have no Agence Bio population "
                    f"(run m3ag_s1/m3ag_s2 for them): {', '.join(missing[:20])}"
                    + (" …" if len(missing) > 20 else ""))
    for d in depts if verbose else []:
        sub = [x for x in rows if x["dept"] == d]
        log.info(f"[{d}] {len(sub)}: phone {sum(1 for x in sub if x['phone'] or x['mobile'])}, "
                 f"e-mail {sum(1 for x in sub if x['email'])}, site {sum(1 for x in sub if x['website'])}, "
                 f"SIRET {sum(1 for x in sub if len(x['siret']) == 14)}")


if __name__ == "__main__":
    main()
