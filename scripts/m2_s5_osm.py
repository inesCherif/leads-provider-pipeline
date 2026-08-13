"""
M2-S5 — OpenStreetMap harvest (Overpass API)
=============================================
The registry gives no website, no email, no phone — verified on live payloads.
This is the FIRST place to look for them, before any scraping:

  * free, no key, no anti-bot, no ToS grey zone (ODbL, attribution required)
  * already structured: `website`, `phone`, `contact:email`, `brand` are tags
  * one HTTP request for the whole département

Why first: the project's rule is drain cheap certainty before expensive
uncertainty. A Playwright scrape that takes hours should only run against
what OSM could not already answer.

Query: shop=bakery / shop=pastry / craft=bakery inside the dept-13 admin
boundary (Bouches-du-Rhône, INSEE ref 13 -> Overpass area 3600007379...
resolved dynamically from `ref:INSEE`, never hardcoded).

Output: exports/boulangerie/checkpoints/osm_listings.csv — one row per OSM
element, written in ONE flush (the whole response arrives at once, so there
is no partial-progress problem the way there is with paged scraping).

Nothing is matched to a SIRET here. Matching is m2_s7's job and requires
location agreement — an OSM name alone must never write an email onto a
company row.

Usage:
    python scripts/m2_s5_osm.py
    python scripts/m2_s5_osm.py --timeout 300
"""

import argparse
import csv
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
OUT_DIR  = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OUT_PATH = OUT_DIR / "osm_listings.csv"

# Mirrors, tried in order: the main instance rate-limits aggressively.
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
DEPT_INSEE = "13"
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 5.0

# nwr = nodes, ways and relations: a bakery can be mapped as a point OR as a
# building outline. Querying only nodes silently loses the mapped-as-building
# ones, which skew to the larger, better-documented businesses.
QUERY_TMPL = """
[out:json][timeout:{timeout}];
area["boundary"="administrative"]["ref:INSEE"="{dept}"]->.dept;
(
  nwr["shop"="bakery"](area.dept);
  nwr["shop"="pastry"](area.dept);
  nwr["craft"="bakery"](area.dept);
);
out center tags;
"""

FIELDNAMES = ["osm_type", "osm_id", "name", "brand", "shop", "phone", "email",
              "website", "facebook", "street", "housenumber", "postcode",
              "city", "siret", "opening_hours", "lat", "lon"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s5")


def run_query(query: str) -> dict:
    body = urllib.parse.urlencode({"data": query}).encode()
    last_err = None
    for endpoint in ENDPOINTS:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                req = urllib.request.Request(
                    endpoint, data=body,
                    headers={"User-Agent": "leads-provider-m2s5 (contact: contact@example.org)"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                # 429 (rate limit) and 504 (query timeout) are the two normal
                # Overpass failures; both are worth a slower retry.
                wait = RETRY_BACKOFF * attempt
                log.warning(f"{endpoint}: HTTP {e.code}, retry in {wait:.0f}s "
                            f"({attempt}/{RETRY_ATTEMPTS})")
                time.sleep(wait)
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning(f"{endpoint}: {last_err}")
                time.sleep(RETRY_BACKOFF)
        log.warning(f"{endpoint} exhausted, trying next mirror")
    sys.exit(f"All Overpass mirrors failed. Last error: {last_err}")


def tag(tags: dict, *names: str) -> str:
    """First non-empty value among alternative tag spellings. OSM records the
    same fact under several keys (phone / contact:phone), and picking only one
    silently drops the other half of the data."""
    for n in names:
        v = (tags.get(n) or "").strip()
        if v:
            return v
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest dept-13 bakeries from OpenStreetMap")
    ap.add_argument("--timeout", type=int, default=180, help="Overpass server-side timeout (s)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    query = QUERY_TMPL.format(timeout=args.timeout, dept=DEPT_INSEE)
    log.info(f"Querying Overpass for shop=bakery|pastry, craft=bakery in dept {DEPT_INSEE}…")
    t0 = time.time()
    data = run_query(query)
    elements = data.get("elements", [])
    log.info(f"{len(elements)} elements returned in {time.time()-t0:.1f}s")

    rows = []
    for el in elements:
        tags = el.get("tags") or {}
        website = tag(tags, "website", "contact:website", "url")
        facebook = tag(tags, "contact:facebook", "facebook")
        # A facebook URL parked in `website` is a social page, not a domain we
        # can mine for a corporate email. Separate them here, not downstream.
        if "facebook." in website.lower():
            facebook = facebook or website
            website = ""
        rows.append({
            "osm_type": el.get("type"),
            "osm_id": el.get("id"),
            "name": tag(tags, "name", "official_name", "alt_name"),
            "brand": tag(tags, "brand"),
            "shop": tag(tags, "shop") or tag(tags, "craft"),
            "phone": tag(tags, "phone", "contact:phone", "contact:mobile"),
            "email": tag(tags, "email", "contact:email"),
            "website": website,
            "facebook": facebook,
            "street": tag(tags, "addr:street"),
            "housenumber": tag(tags, "addr:housenumber"),
            "postcode": tag(tags, "addr:postcode"),
            "city": tag(tags, "addr:city"),
            "siret": tag(tags, "ref:FR:SIRET", "ref:FR:siret"),
            "opening_hours": tag(tags, "opening_hours"),
            "lat": el.get("lat") or (el.get("center") or {}).get("lat", ""),
            "lon": el.get("lon") or (el.get("center") or {}).get("lon", ""),
        })

    rows.sort(key=lambda r: (r["postcode"], r["name"], str(r["osm_id"])))
    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    named    = sum(1 for r in rows if r["name"])
    w_phone  = sum(1 for r in rows if r["phone"])
    w_email  = sum(1 for r in rows if r["email"])
    w_site   = sum(1 for r in rows if r["website"])
    w_fb     = sum(1 for r in rows if r["facebook"])
    w_siret  = sum(1 for r in rows if r["siret"])
    w_cp     = sum(1 for r in rows if r["postcode"].startswith("13"))
    log.info("─" * 62)
    log.info(f"written={n} rows -> {OUT_PATH}")
    log.info(f"  named                 {named:>5} ({named/max(1,n):.0%})")
    log.info(f"  with a phone          {w_phone:>5} ({w_phone/max(1,n):.0%})")
    log.info(f"  with an EMAIL         {w_email:>5} ({w_email/max(1,n):.0%})")
    log.info(f"  with a website        {w_site:>5} ({w_site/max(1,n):.0%})  <- domains to crawl in m2_s9")
    log.info(f"  facebook only         {w_fb:>5}")
    log.info(f"  with a SIRET tag      {w_siret:>5}  <- exact match, no ambiguity")
    log.info(f"  postcode in dept 13   {w_cp:>5}")
    log.info(f"  shop types: {dict(Counter(r['shop'] for r in rows))}")
    log.info("Attribution: © OpenStreetMap contributors (ODbL) — required if "
             "this data reaches the client file.")
    log.info("Next: python scripts/m2_s6_pagesjaunes.py (or m2_s7_match.py to "
             "match what we already have)")


if __name__ == "__main__":
    main()
