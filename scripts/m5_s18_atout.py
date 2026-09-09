"""
M5-S18 — Atout France: classified campings (stars, capacity, website, a dated classement)
========================================================================================
The official classification register (Licence Ouverte, refreshed weekly):
every camping / village de vacances / résidence de tourisme / auberge
collective that holds a valid classement, with its stars, capacity, number
of pitches, address and website. No phone, no e-mail, no SIRET — but a
classement is valid five years and is renewed by the operator, so a recent
one is a proof of life, and capacity is the energy proxy the client cares
about. Meublés and chambres d'hôtes are classified by other bodies and are
NOT in this file; hotels are out of our scope.

Input : https://data.classement.atout-france.fr/static/exportHebergementsClasses/hebergements_classes.csv
        (';', downloaded to checkpoints/opendata/ with --download)
Output: exports/hebergement/checkpoints/atout_listings.csv  (';')
        rows with a CP in 03/63, geocoded through the BAN
        (api-adresse.data.gouv.fr, free, cached in checkpoints/ban_cache.csv)
        so the matcher's geo rungs can use them.

Usage:
    python scripts/m5_s18_atout.py --download
    python scripts/m5_s18_atout.py
"""

import argparse
import csv
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m5_lib import CHECK_DIR, DEPARTEMENTS, read_csv, append_rows   # noqa: E402

SRC_URL = "https://data.classement.atout-france.fr/static/exportHebergementsClasses/hebergements_classes.csv"
SRC_PATH = CHECK_DIR / "opendata" / "atout_hebergements_classes.csv"
OUT_PATH = CHECK_DIR / "atout_listings.csv"
BAN_CACHE = CHECK_DIR / "ban_cache.csv"
BAN = "https://api-adresse.data.gouv.fr/search/"
TYPOLOGIES = ("CAMPING", "VILLAGE DE VACANCES", "RÉSIDENCE DE TOURISME", "RESIDENCE DE TOURISME",
              "AUBERGE COLLECTIVE", "PARC RÉSIDENTIEL DE LOISIRS", "PARC RESIDENTIEL DE LOISIRS")
TYPE_FR = {"CAMPING": "camping", "VILLAGE DE VACANCES": "village vacances",
           "RÉSIDENCE DE TOURISME": "résidence", "RESIDENCE DE TOURISME": "résidence",
           "AUBERGE COLLECTIVE": "hébergement collectif",
           "PARC RÉSIDENTIEL DE LOISIRS": "camping", "PARC RESIDENTIEL DE LOISIRS": "camping"}

FIELDS = ["dept", "atout_id", "name", "atout_type", "typologie", "classement", "categorie", "mention",
          "date_classement", "proroge", "address", "postcode", "city", "website",
          "capacite", "emplacements", "chambres", "logements", "lat", "lon", "geo_score", "geo_label"]
BAN_FIELDS = ["key", "lat", "lon", "score", "label"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m5_s18")


def col(row: dict, *names: str) -> str:
    """Header names carry accents and vary in case — match loosely."""
    import unicodedata
    def n(s): return unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().upper().strip()
    keys = {n(k): k for k in row}
    for name in names:
        k = keys.get(n(name))
        if k is not None:
            return (row[k] or "").strip()
    return ""


def geocode(cache: dict, address: str, postcode: str, city: str) -> dict:
    key = f"{address}|{postcode}|{city}".lower()
    if key in cache:
        return cache[key]
    q = " ".join(x for x in (address, city) if x)
    params = {"q": q or city, "postcode": postcode, "limit": 1}
    hit = {"key": key, "lat": "", "lon": "", "score": "", "label": ""}
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(BAN + "?" + urllib.parse.urlencode(params), timeout=20) as resp:
                d = json.loads(resp.read())
            feats = d.get("features") or []
            if feats:
                f = feats[0]
                lon, lat = f["geometry"]["coordinates"]
                hit.update(lat=lat, lon=lon, score=f["properties"].get("score", ""),
                           label=f["properties"].get("label", ""))
            break
        except Exception:
            time.sleep(2 * attempt)
    cache[key] = hit
    append_rows(BAN_CACHE, BAN_FIELDS, [hit])
    return hit


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--download", action="store_true")
    args = ap.parse_args()
    if args.download or not SRC_PATH.exists():
        SRC_PATH.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"downloading {SRC_URL}")
        urllib.request.urlretrieve(SRC_URL, SRC_PATH)

    raw = SRC_PATH.read_bytes()
    text = raw.decode("utf-8-sig") if b"\xc3" in raw[:200000] or raw.startswith(b"\xef\xbb\xbf") else raw.decode("cp1252")
    src = list(csv.DictReader(text.splitlines(), delimiter=";"))
    cache = {r["key"]: r for r in read_csv(BAN_CACHE)}
    rows, seen = [], set()
    n_dept = 0
    for i, r in enumerate(src):
        cp = col(r, "CODE POSTAL")
        if cp[:2] not in DEPARTEMENTS:
            continue
        n_dept += 1
        typo = col(r, "TYPOLOGIE ÉTABLISSEMENT", "TYPOLOGIE ETABLISSEMENT").upper()
        if typo not in TYPOLOGIES:
            continue
        name = col(r, "NOM COMMERCIAL")
        addr, city = col(r, "ADRESSE"), col(r, "COMMUNE")
        key = (name.lower(), cp, addr.lower())
        if key in seen:
            continue
        seen.add(key)
        g = geocode(cache, addr, cp, city)
        rows.append({
            "dept": cp[:2], "atout_id": f"AF{i}", "name": name, "atout_type": TYPE_FR.get(typo, typo.lower()),
            "typologie": typo, "classement": col(r, "CLASSEMENT"), "categorie": col(r, "CATÉGORIE", "CATEGORIE"),
            "mention": col(r, "MENTION (villages de vacances)"),
            "date_classement": col(r, "DATE DE CLASSEMENT"), "proroge": col(r, "classement prorogé", "classement proroge"),
            "address": addr, "postcode": cp, "city": city, "website": col(r, "SITE INTERNET").replace("-", "") if col(r, "SITE INTERNET") == "-" else col(r, "SITE INTERNET"),
            "capacite": col(r, "CAPACITÉ D'ACCUEIL (PERSONNES)", "CAPACITE D'ACCUEIL (PERSONNES)"),
            "emplacements": col(r, "NOMBRE D'EMPLACEMENTS"), "chambres": col(r, "NOMBRE DE CHAMBRES"),
            "logements": col(r, "NOMBRE DE LOGEMENTS (villages de vacances)"),
            "lat": g["lat"], "lon": g["lon"], "geo_score": g["score"], "geo_label": g["label"],
        })
        time.sleep(0.05)
    for r in rows:
        if r["website"] in ("-", "N/A"):
            r["website"] = ""
    rows.sort(key=lambda r: (r["dept"], r["postcode"], r["name"]))
    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    log.info("─" * 62)
    log.info(f"{len(src)} classified establishments in France, {n_dept} in depts {'/'.join(DEPARTEMENTS)}, "
             f"written={len(rows)} in our typologies -> {OUT_PATH.name}")
    for d in DEPARTEMENTS:
        sub = [r for r in rows if r["dept"] == d]
        log.info(f"[{d}] {len(sub)} rows  by type {dict(Counter(r['atout_type'] for r in sub).most_common())}  "
                 f"classement {dict(Counter(r['classement'] for r in sub).most_common(6))}")
        log.info(f"[{d}]   website {sum(1 for r in sub if r['website'])}  geocoded {sum(1 for r in sub if r['lat'])} "
                 f"(BAN score ≥ 0.6: {sum(1 for r in sub if r['geo_score'] and float(r['geo_score']) >= 0.6)})  "
                 f"emplacements total {sum(int(r['emplacements']) for r in sub if r['emplacements'].isdigit())}")
    log.info("Attribution: Atout France — Licence Ouverte; géocodage BAN (Etalab).")


if __name__ == "__main__":
    main()
