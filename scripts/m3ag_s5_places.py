"""
M3AG-S5 — Google Places (New) Text Search, one query per phoneless operator
==========================================================================
Sam's sample was Agence Bio + Google Places, and carried the wrong-match
bugs M2 solved (a farmer matched to a lake, another to a midwife). So this
script NEVER trusts the top result: every place returned is written as a
LISTING with lat/lon, and m3ag_s8's geo rungs decide (≤150 m + name
agreement, or ≤40 m unique) — the same test OSM and PJ listings pass.

Cost model (changed March 2025 — no more $200 credit): a Text Search whose
field mask includes phone/website is the ENTERPRISE SKU, 1,000 free
requests per month, then paid. The counter lives in the shared quota file
(pool "places", monthly) and hard-stops at 1,000 like the other pools.
Targets are phoneless operators only, biggest gaps first.

Prerequisites: GOOGLE_PLACES_API_KEY in .env, "Places API (New)" ENABLED on
the key's Google Cloud project, key restricted to that API, budget cap set.
(2026-09-03: the key answers 403 PERMISSION_DENIED — the API is not enabled.)

Output : checkpoints/places_listings.csv  (';', wired into m3ag_s8 SOURCES)
Done   : checkpoints/places_done.txt  keys "<dept>:<row_id>"

Usage:
    python scripts/m3ag_s5_places.py --ping                       # 1 request
    python scripts/m3ag_s5_places.py --departement 63 --pilot 20
    python scripts/m3ag_s5_places.py --departement 63
"""

import argparse
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from dotenv import load_dotenv                                          # noqa: E402
from m2lib_search import charge, quota_state, QuotaExceeded             # noqa: E402
from m2lib_contact import normalize_fr_phone                            # noqa: E402
from m3ag_lib import (CHECK_DIR, QUOTA_PATH, load_operators, load_matches,  # noqa: E402
                      append_rows, load_done, mark_done, is_social)

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ("places.id,places.displayName,places.formattedAddress,places.location,"
              "places.nationalPhoneNumber,places.websiteUri,places.types,places.businessStatus")
RADIUS_M = 5000.0
MAX_RESULTS = 5
DELAY = 0.3

OUT_PATH = CHECK_DIR / "places_listings.csv"
DONE_PATH = CHECK_DIR / "places_done.txt"
FIELDNAMES = ["listing_id", "query_row_id", "query_name", "name", "phone", "mobile",
              "email", "website", "address", "postcode", "city", "lat", "lon",
              "types", "status", "distance_m"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s5")


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def text_search(key: str, query: str, lat: float | None, lon: float | None) -> list:
    body = {"textQuery": query, "languageCode": "fr", "regionCode": "FR",
            "maxResultCount": MAX_RESULTS}
    if lat is not None and lon is not None:
        body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lon},
                                           "radius": RADIUS_M}}
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": FIELD_MASK})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("places", [])


def main() -> None:
    ap = argparse.ArgumentParser(description="Google Places (New) text search per phoneless operator")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ping", action="store_true", help="one request, prints the answer")
    args = ap.parse_args()
    dept = args.departement

    load_dotenv(PROJECT_ROOT / ".env")
    key = os.getenv("GOOGLE_PLACES_API_KEY", "")
    if not key:
        sys.exit("GOOGLE_PLACES_API_KEY missing from .env")

    if args.ping:
        try:
            charge("places", 1, QUOTA_PATH)
            places = text_search(key, "ferme bio Ambert", 45.55, 3.74)
            for p in places:
                print(p.get("displayName", {}).get("text"), "|", p.get("formattedAddress"),
                      "|", p.get("nationalPhoneNumber"), "|", p.get("websiteUri"))
            print(f"OK — {len(places)} places. quota: {quota_state(QUOTA_PATH).get('places')}")
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.read()[:400].decode(errors='replace')}")
            if e.code == 403:
                print("-> enable 'Places API (New)' on the key's Google Cloud project, "
                      "and check the key's API restriction.")
        return

    ops = load_operators(dept)
    matches = load_matches(dept)
    done = load_done(DONE_PATH)

    def has_phone(op):
        return op["_has_phone"] or any(m.get("phone") or m.get("mobile")
                                       for m in matches.get(op["_id"], []))

    todo = [op for op in ops if not has_phone(op) and f"{dept}:{op['_id']}" not in done]
    todo.sort(key=lambda op: (0 if op.get("flag_hors_agri") in ("", "0") else 1,
                              0 if op.get("gerant") else 1, op["codePostal"]))
    if args.pilot:
        todo = todo[:args.pilot]
    elif args.limit:
        todo = todo[:args.limit]
    log.info(f"[{dept}] {len(ops)} operators | {len(todo)} phoneless to query | "
             f"quota {quota_state(QUOTA_PATH).get('places')}")

    stats = Counter()
    try:
        for i, op in enumerate(todo, 1):
            q = f"{op['raisonSociale']} {op['ville']}"
            lat = float(op["lat"]) if op.get("lat") else None
            lon = float(op["lon"]) if op.get("lon") else None
            charge("places", 1, QUOTA_PATH)
            try:
                places = text_search(key, q, lat, lon)
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode(errors="replace")
                if e.code in (403, 401):
                    sys.exit(f"HTTP {e.code}: {body}\n-> enable Places API (New) / check key restriction")
                log.warning(f"[{i}] HTTP {e.code}: {body}")
                time.sleep(2)
                continue
            except Exception as exc:
                log.warning(f"[{i}] {type(exc).__name__}: {exc}")
                time.sleep(2)
                continue
            rows = []
            for p in places:
                loc = p.get("location") or {}
                plat, plon = loc.get("latitude"), loc.get("longitude")
                addr = p.get("formattedAddress") or ""
                mcp = re.search(r"\b(\d{5})\b\s*([^,]*)", addr)
                site = p.get("websiteUri") or ""
                if is_social(site):
                    site = ""
                dist = ""
                if lat is not None and plat is not None:
                    dist = f"{haversine_m(lat, lon, plat, plon):.0f}"
                rows.append({
                    "listing_id": p.get("id", ""), "query_row_id": op["_id"],
                    "query_name": op["raisonSociale"],
                    "name": (p.get("displayName") or {}).get("text", ""),
                    "phone": normalize_fr_phone(p.get("nationalPhoneNumber") or ""),
                    "mobile": "", "email": "", "website": site, "address": addr,
                    "postcode": mcp.group(1) if mcp else "",
                    "city": mcp.group(2).strip() if mcp else "",
                    "lat": plat if plat is not None else "", "lon": plon if plon is not None else "",
                    "types": "|".join(p.get("types") or []), "status": p.get("businessStatus", ""),
                    "distance_m": dist,
                })
            append_rows(OUT_PATH, FIELDNAMES, rows)
            mark_done(DONE_PATH, f"{dept}:{op['_id']}")
            stats["places returned"] += len(rows)
            stats["with phone"] += sum(1 for r in rows if r["phone"])
            if rows and rows[0]["distance_m"] and float(rows[0]["distance_m"]) <= 150:
                stats["top result within 150 m"] += 1
            if args.pilot or i % 25 == 0:
                top = rows[0] if rows else {}
                log.info(f"[{i}/{len(todo)}] {op['raisonSociale'][:26]:26.26} -> {len(rows)} | "
                         f"{top.get('name', '-')[:28]:28.28} {top.get('distance_m', ''):>6} m "
                         f"{top.get('phone', '')}")
            time.sleep(DELAY)
    except QuotaExceeded as exc:
        log.warning(f"STOP (monthly cap): {exc}")

    log.info("─" * 62)
    for k, v in stats.most_common():
        log.info(f"  {k:<28} {v}")
    log.info(f"quota after: {quota_state(QUOTA_PATH).get('places')}")
    log.info("Next: python scripts/m3ag_s8_match.py --departement " + dept +
             "  (geo rungs decide; never the top result by itself)")


if __name__ == "__main__":
    main()
