"""
M5-S17 — DATAtourisme: the tourism offices' own records (websites, labels, liveness)
==================================================================================
DATAtourisme (ADN Tourisme, Licence Ouverte 2.0) republishes every regional
tourism database as open data; Auvergne-Rhône-Alpes feeds it from Apidae.
A record there is a lodging an office de tourisme maintains — with a website
URL, its labels (Gîtes de France épis, Clévacances clés, étoiles) and the
date the office last touched it. **Phones and e-mails were removed from the
open export on 2023-12-14**; what remains is exactly what we need for the
liveness score and for site discovery.

Input : the daily regional CSV `datatourisme-reg-ara.csv` (44 MB, comma,
        utf-8) — `--download` fetches the current one through the data.gouv
        API (the static URL changes every day), else the copy under
        checkpoints/opendata/ is read.
Output: exports/hebergement/checkpoints/dt_listings.csv  (';')
        one row per accommodation POI with a CP in 03 or 63.

Category URIs are `|`-separated; the most specific accommodation class
becomes `dt_type` (camping / gîte-meublé / chambres d'hôtes / hôtel /
résidence / village vacances / hébergement collectif / autre hébergement).
Contacts arrive as `label#url|…` — only URLs survive; the first non-social,
non-aggregator one is the listing's website.

Usage:
    python scripts/m5_s17_datatourisme.py --download
    python scripts/m5_s17_datatourisme.py
"""

import argparse
import csv
import json
import logging
import sys
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m5_lib import CHECK_DIR, DEPARTEMENTS, is_aggregator, is_social   # noqa: E402

DATASET_API = ("https://www.data.gouv.fr/api/1/datasets/"
               "datatourisme-la-base-nationale-des-donnees-publiques-dinformation-touristique-en-open-data/")
RESOURCE_TITLE = "datatourisme-reg-ara.csv"
SRC_PATH = CHECK_DIR / "opendata" / RESOURCE_TITLE
OUT_PATH = CHECK_DIR / "dt_listings.csv"

# most specific first; the first class found wins
TYPE_BY_CLASS = [
    ("CampingAndCaravanning", "camping"),
    ("BedAndBreakfast", "chambres d'hôtes"),
    ("SelfCateringAccommodation", "gîte-meublé"),
    ("RentalAccommodation", "gîte-meublé"),
    ("Hotel", "hôtel"),
    ("HolidayResort", "village vacances"),
    ("CollectiveAccommodation", "hébergement collectif"),
    ("Hostel", "hébergement collectif"),
    ("Accommodation", "autre hébergement"),
    ("LodgingBusiness", "autre hébergement"),
]
LABEL_KEYS = ("gîtes de france", "gites de france", "clévacances", "clevacances", "étoile",
              "epi", "épi", "clé", "cle", "accueil paysan", "bienvenue à la ferme", "qualité tourisme",
              "tourisme & handicap", "camping qualité", "aire naturelle")

FIELDS = ["dept", "dt_id", "name", "dt_type", "categories", "lat", "lon", "address", "postcode", "city",
          "website", "other_urls", "classements", "lastupdate", "creator", "sit", "uri"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m5_s17")


def download() -> None:
    SRC_PATH.parent.mkdir(parents=True, exist_ok=True)
    d = json.load(urllib.request.urlopen(DATASET_API, timeout=60))
    res = next((r for r in d["resources"] if r.get("title") == RESOURCE_TITLE), None)
    if res is None:
        sys.exit(f"resource {RESOURCE_TITLE} not found on data.gouv")
    log.info(f"downloading {res['url']} ({int(res.get('filesize') or 0)/1e6:.1f} MB)")
    urllib.request.urlretrieve(res["url"], SRC_PATH)


def classify(categories: str) -> tuple[str, str]:
    short = [c.rsplit("#", 1)[-1] for c in categories.split("|") if c]
    for cls, label in TYPE_BY_CLASS:
        if cls in short:
            return label, "|".join(s for s in short if s not in ("PointOfInterest", "PlaceOfInterest"))
    return "", ""


def urls_of(contacts: str) -> list[str]:
    out = []
    for piece in (contacts or "").split("|"):
        for part in piece.split("#"):
            part = part.strip()
            if part.startswith("http") and part not in out:
                out.append(part)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--download", action="store_true", help="fetch today's regional CSV first")
    args = ap.parse_args()
    if args.download or not SRC_PATH.exists():
        download()

    csv.field_size_limit(10 ** 8)
    rows, n_total, n_dept = [], 0, 0
    with SRC_PATH.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            n_total += 1
            pc, _, city = (r.get("Code_postal_et_commune") or "").partition("#")
            if pc[:2] not in DEPARTEMENTS:
                continue
            n_dept += 1
            dt_type, cats = classify(r.get("Categories_de_POI") or "")
            if not dt_type:
                continue
            urls = urls_of(r.get("Contacts_du_POI") or "")
            site = next((u for u in urls if not is_social(u) and not is_aggregator(u)), "")
            others = [u for u in urls if u != site]
            uri = r.get("URI_ID_du_POI") or ""
            rows.append({
                "dept": pc[:2], "dt_id": uri.rsplit("/", 1)[-1], "name": (r.get("Nom_du_POI") or "").strip(),
                "dt_type": dt_type, "categories": cats,
                "lat": r.get("Latitude") or "", "lon": r.get("Longitude") or "",
                "address": (r.get("Adresse_postale") or "").strip(), "postcode": pc, "city": city.strip(),
                "website": site, "other_urls": " ".join(others)[:500],
                "classements": (r.get("Classements_du_POI") or "").replace("|", " ; ")[:300],
                "lastupdate": r.get("Date_de_mise_a_jour") or "",
                "creator": (r.get("Createur_de_la_donnee") or "").strip(),
                "sit": (r.get("SIT_diffuseur") or "").split(" - ")[0].strip(), "uri": uri,
            })
    rows.sort(key=lambda r: (r["dept"], r["postcode"], r["name"]))
    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    log.info("─" * 62)
    log.info(f"{n_total} POIs in the regional file, {n_dept} in depts {'/'.join(DEPARTEMENTS)}, "
             f"written={len(rows)} accommodations -> {OUT_PATH.name}")
    for d in DEPARTEMENTS:
        sub = [r for r in rows if r["dept"] == d]
        log.info(f"[{d}] {len(sub)} rows  by type {dict(Counter(r['dt_type'] for r in sub).most_common())}")
        log.info(f"[{d}]   website {sum(1 for r in sub if r['website'])}  geocoded {sum(1 for r in sub if r['lat'])}  "
                 f"updated ≤ 12 months {sum(1 for r in sub if r['lastupdate'] >= '2025-09-09')}  "
                 f"labelled {sum(1 for r in sub if any(k in r['classements'].lower() for k in LABEL_KEYS))}")
    log.info("Attribution: DATAtourisme / ADN Tourisme — Licence Ouverte 2.0.")


if __name__ == "__main__":
    main()
