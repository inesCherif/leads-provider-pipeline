"""
M7-S5g — « Où trouver des produits bio ? Alpes-Maritimes » (Agribio 06, GoGoCarto) — DB-free
==========================================================================================
Found by the PACA directory recon of 2026-09-20 (Sam: "l'intérêt des autres
annuaires est de récupérer directement l'e-mail, soit le site web"). The map
bio-provence.org embeds is a GoGoCarto instance whose public API returns every
element in ONE call, under ODbL (attribution: Agribio 06 / Bio de Provence):

    https://trouverdesproduitsbiopaca06.gogocarto.fr/api/elements.json

Measured: 96 producers, 88 with an e-mail, 91 with a phone, product categories
on every element (so the principle CAN read them — unlike bienvenue-à-la-ferme,
whose fiches carry no category). Only the 06 exists: the 04 / 05 / 13 / 83 / 84
sub-domains answer a generic page, not an API.

The element fields are a hand-filled form, so the same value lives under two
spellings (`Email` / `email`, `Telephone` / `telephone`, `Site_internet` /
`site_internet`, `NOM` / `Nom`); `field()` reads whichever is filled.

Output : checkpoints/bp06_listings.csv (';', m7_lib.LISTING_FIELDS + lat / lon),
         source 'biopaca06'. Whole-file rewrite: one call, idempotent, no done-file.

Usage:
    python scripts/m7_s5g_biopaca06.py
"""

import csv
import logging
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone                                # noqa: E402
from m7_lib import (CHECK_DIR, LISTING_FIELDS, make_session, listing_excluded,  # noqa: E402
                    dept_of_cp, is_social)

API = "https://trouverdesproduitsbiopaca06.gogocarto.fr/api/elements.json"
PAGE = "https://trouverdesproduitsbiopaca06.gogocarto.fr/map#/fiche/-/{id}/"
SOURCE = "biopaca06"
DEPT = "06"
OUT_PATH = CHECK_DIR / "bp06_listings.csv"
FIELDS = LISTING_FIELDS + ["lat", "lon"]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("m7_s5g")


def field(e: dict, *names: str) -> str:
    for n in names:
        v = e.get(n)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())          # a hand-filled form: line breaks inside a value
    return ""


def parse(e: dict) -> dict:
    addr = e.get("address") or {}
    free = field(e, "Adresse", "adresse")
    cp = (addr.get("postalCode") or "").strip()
    if not cp:
        m = re.search(r"\b(0[46]\d{3}|83\d{3})\b", free)      # the map spills a little over its border
        cp = m.group(1) if m else ""
    city = (addr.get("addressLocality") or "").strip()
    street = " ".join(x for x in (addr.get("streetNumber"), addr.get("streetAddress")) if x) or free
    m = EMAIL_RE.search(field(e, "Email", "email"))
    site = field(e, "Site_internet", "site_internet")
    if site and not site.lower().startswith("http"):
        site = "https://" + site
    if site and is_social(site):
        site = ""
    geo = e.get("geo") or {}
    cats = " - ".join(e.get("categories") or [])
    person = " ".join(x for x in (field(e, "Prenom", "prenom"), field(e, "NOM", "Nom", "nom")) if x)
    return {"listing_id": str(e.get("id", "")), "dept": dept_of_cp(cp) or DEPT, "name": " ".join((e.get("name") or "").split())[:200],
            "alt_name": person[:120], "phone": normalize_fr_phone(field(e, "Telephone", "telephone")) or "",
            "mobile": "", "email": m.group(0).lower() if m else "", "website": site[:300], "city": city,
            "postcode": cp, "address": street[:200], "url": PAGE.format(id=e.get("id", "")),
            "description": (field(e, "Produit", "produit") + " | " + field(e, "Vente", "vente")).strip(" |")[:1500],
            "productions": cats[:500], "categorie": cats[:300], "siret": "",
            "lat": geo.get("latitude", ""), "lon": geo.get("longitude", "")}


def main() -> None:
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    sess = make_session()
    resp = sess.get(API, timeout=90)
    resp.raise_for_status()
    payload = resp.json()
    elements = payload.get("data") or []
    if not elements:
        sys.exit("the API returned no element — nothing written, the previous file is kept")
    rows, skipped = [], 0
    for e in elements:
        row = parse(e)
        why = listing_excluded(row["name"], row["categorie"], row["website"], row["email"], row["description"])
        if why:
            skipped += 1
            log.info(f"EXCLU {row['name'][:40]} <- {why}")
            continue
        rows.append(row)
    tmp = OUT_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(OUT_PATH)
    log.info("-" * 62)
    log.info(f"licence: {payload.get('licence', '?')}")
    log.info(f"elements {len(elements)} | written {len(rows)} | skipped on principle {skipped} | "
             f"e-mail {sum(1 for r in rows if r['email'])} | phone {sum(1 for r in rows if r['phone'])} | "
             f"site {sum(1 for r in rows if r['website'])} | with a postcode {sum(1 for r in rows if r['postcode'])}")
    log.info(f"file: {OUT_PATH}")


if __name__ == "__main__":
    main()
