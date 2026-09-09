"""
M5-S6 — OpenStreetMap harvest (Overpass) of campings and gîtes in a département
===============================================================================
Copy of `m3ag_s6_osm.py` with tourism tags. Same reasons it runs early: free,
no key, no anti-bot, one HTTP call, structured tags (`phone`, `contact:email`,
`website`, `ref:FR:SIRET`, `operator`, `operator:type`, `capacity`,
`check_date`), and lat/lon for the matcher's geo rungs. OSM phones agreed
95.7 % with Google Maps on bakeries — the top of PHONE_RANK, measured.

Tags queried (nodes, ways and relations — a camping is usually an area):
    tourism=camp_site | caravan_site | chalet | guest_house | apartment |
            hostel | alpine_hut | wilderness_hut | motel

Output: exports/hebergement/checkpoints/osm_listings.csv  (';')
        rows carry `dept`; re-running a dept replaces its rows.
`operator_type=public` / an operator named like a commune is how phase 2
will find the municipal campings the registry cannot list.

Usage:
    python scripts/m5_s6_osm.py --departement 63
    python scripts/m5_s6_osm.py --departement 03 --timeout 300
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
from m5_lib import CHECK_DIR, read_csv, PUBLIC_NAME_RE  # noqa: E402

OUT_PATH = CHECK_DIR / "osm_listings.csv"

QUERY_TMPL = """
[out:json][timeout:{timeout}];
area["boundary"="administrative"]["admin_level"="6"]["ref:INSEE"="{dept}"]->.dept;
(
  nwr["tourism"~"^(camp_site|caravan_site|chalet|guest_house|apartment|hostel|alpine_hut|wilderness_hut|motel)$"](area.dept);
);
out center tags;
"""

KIND_FR = {"camp_site": "camping", "caravan_site": "aire camping-car", "chalet": "gîte-meublé",
           "guest_house": "chambres d'hôtes", "apartment": "gîte-meublé", "hostel": "hébergement collectif",
           "alpine_hut": "refuge", "wilderness_hut": "refuge", "motel": "hôtel"}

FIELDNAMES = ["dept", "osm_type", "osm_id", "name", "kind", "tourism", "phone", "mobile", "email",
              "website", "facebook", "address", "postcode", "city", "siret",
              "lat", "lon", "operator", "operator_type", "capacity", "stars", "check_date", "backcountry"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m5_s6")


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest campings and gîtes from OpenStreetMap for one dept")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()
    dept = args.departement

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    query = QUERY_TMPL.format(timeout=args.timeout, dept=dept)
    log.info(f"Querying Overpass for tourism tags in dept {dept}…")
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
        tourism = tag(tags, "tourism")
        operator = tag(tags, "operator")
        op_type = tag(tags, "operator:type")
        if not op_type and (PUBLIC_NAME_RE.search(operator) or PUBLIC_NAME_RE.search(tag(tags, "name"))):
            op_type = "public?"
        rows.append({
            "dept": dept, "osm_type": el.get("type"), "osm_id": el.get("id"),
            "name": tag(tags, "name", "official_name", "alt_name", "brand", "operator"),
            "kind": KIND_FR.get(tourism, tourism), "tourism": tourism,
            "phone": phone, "mobile": mobile, "email": tag(tags, "email", "contact:email"),
            "website": website, "facebook": facebook,
            "address": f"{house} {street}".strip(), "postcode": tag(tags, "addr:postcode"),
            "city": tag(tags, "addr:city"), "siret": tag(tags, "ref:FR:SIRET", "ref:FR:siret"),
            "lat": el.get("lat") or (el.get("center") or {}).get("lat", ""),
            "lon": el.get("lon") or (el.get("center") or {}).get("lon", ""),
            "operator": operator, "operator_type": op_type, "capacity": tag(tags, "capacity"),
            "stars": tag(tags, "stars"), "check_date": tag(tags, "check_date", "survey:date"),
            "backcountry": tag(tags, "backcountry"),
        })

    others = [r for r in read_csv(OUT_PATH) if r.get("dept") != dept]
    all_rows = others + rows
    all_rows.sort(key=lambda r: (r["dept"], r["postcode"], r["name"], str(r["osm_id"])))
    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(all_rows)

    n = len(rows)
    log.info("─" * 62)
    log.info(f"[{dept}] written={n} rows (file now {len(all_rows)}) -> {OUT_PATH}")
    log.info(f"  named                 {sum(1 for r in rows if r['name']):>5}")
    log.info(f"  with a phone          {sum(1 for r in rows if r['phone'] or r['mobile']):>5}")
    log.info(f"  with an EMAIL         {sum(1 for r in rows if r['email']):>5}")
    log.info(f"  with a website        {sum(1 for r in rows if r['website']):>5}")
    log.info(f"  with a SIRET tag      {sum(1 for r in rows if r['siret']):>5}")
    log.info(f"  public operator       {sum(1 for r in rows if r['operator_type']):>5}  ({dict(Counter(r['operator_type'] for r in rows if r['operator_type']))})")
    log.info(f"  kinds: {dict(Counter(r['kind'] for r in rows).most_common(9))}")
    camp = [r for r in rows if r["tourism"] == "camp_site"]
    log.info(f"  camp_site: {len(camp)} — named {sum(1 for r in camp if r['name'])}, phone {sum(1 for r in camp if r['phone'] or r['mobile'])}, "
             f"website {sum(1 for r in camp if r['website'])}, backcountry {sum(1 for r in camp if r['backcountry'] == 'yes')}")
    log.info("Attribution: © OpenStreetMap contributors (ODbL).")


if __name__ == "__main__":
    main()
