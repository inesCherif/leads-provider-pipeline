"""
M3AG-S6 — OpenStreetMap harvest (Overpass) of farms in a département
====================================================================
Adapted from m2_s5_osm.py (bakeries). Same reasons it runs first: free, no
key, no anti-bot, one HTTP call, and the tags are already structured
(`phone`, `contact:email`, `website`, `ref:FR:SIRET`). OSM carries lat/lon,
which is what unlocks m3ag_s8's geo rungs — Pages Jaunes never could.

Tags queried (nodes, ways AND relations — a farm is often a building
outline or a landuse polygon, not a point):
    shop=farm|dairy|honey|cheese|wine       craft=beekeeper|winery|cheese|
    distillery|brewery|cider|oil_mill|agricultural_engines(no)…
    landuse=farmyard[name]  place=farm[name]  organic=only|yes  produce=*

Output: exports/agriculteurs/checkpoints/osm_listings.csv  (';', one flush)
        appended per dept — rows carry `dept`; re-running a dept replaces
        its rows (the file is rebuilt from the other depts' rows + new).
Nothing is matched here; m3ag_s8 does that with location agreement.

Usage:
    python scripts/m3ag_s6_osm.py --departement 63
    python scripts/m3ag_s6_osm.py --departement 03 --timeout 300
"""

import argparse
import csv
import logging
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2_s5_osm import run_query, tag                    # noqa: E402
from m2lib_contact import normalize_fr_phone            # noqa: E402
from m3ag_lib import CHECK_DIR, read_csv                # noqa: E402

OUT_PATH = CHECK_DIR / "osm_listings.csv"

QUERY_TMPL = """
[out:json][timeout:{timeout}];
area["boundary"="administrative"]["admin_level"="6"]["ref:INSEE"="{dept}"]->.dept;
(
  nwr["shop"~"^(farm|dairy|honey|cheese|wine|greengrocer)$"](area.dept);
  nwr["craft"~"^(beekeeper|winery|cheese|distillery|brewery|cider|oil_mill|agricultural)$"](area.dept);
  nwr["landuse"="farmyard"]["name"](area.dept);
  nwr["place"="farm"]["name"](area.dept);
  nwr["organic"~"^(only|yes)$"](area.dept);
  nwr["produce"](area.dept);
  nwr["amenity"="marketplace"]["name"](area.dept);
);
out center tags;
"""

FIELDNAMES = ["dept", "osm_type", "osm_id", "name", "kind", "phone", "mobile", "email",
              "website", "facebook", "address", "postcode", "city", "siret",
              "lat", "lon", "organic", "produce"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s6")


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest farms from OpenStreetMap for one dept")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()
    dept = args.departement

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    query = QUERY_TMPL.format(timeout=args.timeout, dept=dept)
    log.info(f"Querying Overpass for farm tags in dept {dept}…")
    t0 = time.time()
    data = run_query(query)
    elements = data.get("elements", [])
    log.info(f"{len(elements)} elements returned in {time.time()-t0:.1f}s")

    rows = []
    for el in elements:
        tags = el.get("tags") or {}
        website = tag(tags, "website", "contact:website", "url")
        facebook = tag(tags, "contact:facebook", "facebook")
        if "facebook." in website.lower() or "instagram." in website.lower():
            facebook = facebook or website
            website = ""
        phone = normalize_fr_phone(tag(tags, "phone", "contact:phone"))
        mobile = normalize_fr_phone(tag(tags, "contact:mobile", "mobile"))
        if mobile and mobile == phone:
            mobile = ""
        street = tag(tags, "addr:street", "addr:place")
        house = tag(tags, "addr:housenumber")
        kind = (tag(tags, "shop") and f"shop={tag(tags, 'shop')}") or \
               (tag(tags, "craft") and f"craft={tag(tags, 'craft')}") or \
               (tag(tags, "landuse") and "landuse=farmyard") or \
               (tag(tags, "place") and "place=farm") or \
               (tag(tags, "amenity") and f"amenity={tag(tags, 'amenity')}") or "other"
        rows.append({
            "dept": dept,
            "osm_type": el.get("type"),
            "osm_id": el.get("id"),
            "name": tag(tags, "name", "official_name", "alt_name", "brand", "operator"),
            "kind": kind,
            "phone": phone,
            "mobile": mobile,
            "email": tag(tags, "email", "contact:email"),
            "website": website,
            "facebook": facebook,
            "address": f"{house} {street}".strip(),
            "postcode": tag(tags, "addr:postcode"),
            "city": tag(tags, "addr:city"),
            "siret": tag(tags, "ref:FR:SIRET", "ref:FR:siret"),
            "lat": el.get("lat") or (el.get("center") or {}).get("lat", ""),
            "lon": el.get("lon") or (el.get("center") or {}).get("lon", ""),
            "organic": tag(tags, "organic"),
            "produce": tag(tags, "produce"),
        })

    # keep the other depts' rows, replace this dept's
    others = [r for r in read_csv(OUT_PATH) if r.get("dept") != dept]
    all_rows = others + rows
    all_rows.sort(key=lambda r: (r["dept"], r["postcode"], r["name"], str(r["osm_id"])))
    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(all_rows)

    n = len(rows)
    named = sum(1 for r in rows if r["name"])
    w_phone = sum(1 for r in rows if r["phone"] or r["mobile"])
    w_email = sum(1 for r in rows if r["email"])
    w_site = sum(1 for r in rows if r["website"])
    w_siret = sum(1 for r in rows if r["siret"])
    w_any = sum(1 for r in rows if r["phone"] or r["mobile"] or r["email"] or r["website"])
    log.info("─" * 62)
    log.info(f"[{dept}] written={n} rows (file now {len(all_rows)}) -> {OUT_PATH}")
    log.info(f"  named                 {named:>5}")
    log.info(f"  with a phone          {w_phone:>5}")
    log.info(f"  with an EMAIL         {w_email:>5}")
    log.info(f"  with a website        {w_site:>5}")
    log.info(f"  with a SIRET tag      {w_siret:>5}")
    log.info(f"  with ANY contact      {w_any:>5}  <- the only rows m3ag_s8 can use")
    log.info(f"  kinds: {dict(Counter(r['kind'] for r in rows).most_common(8))}")
    log.info("Attribution: © OpenStreetMap contributors (ODbL).")


if __name__ == "__main__":
    main()
