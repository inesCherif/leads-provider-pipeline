"""
M2-S13 — Harvest Google Maps listings (Serper /maps) for dept-13 bakeries
=========================================================================
The V3 phone engine. Pages Jaunes is DataDome-blocked; Google Maps holds the
same phones and Serper's /maps endpoint serves it as a free-tier API — no
anti-bot, no CAPTCHA, no proxy. MEASURED before this was written (2026-08-13):

    /places  1 credit   NO phone field — useless for the goal
    /maps    3 credits  ~20 listings/query, phone on ~16/20, website on ~5/20
             NO pagination (page=2 returns empty) -> dense areas need many
             ANCHORS, not many pages

The sweep is GREEDY and TARGET-ANCHORED, because credits are one-time (2,500
for the whole account) and a per-business query would cost 1,599 x 3 = 4,797:

    * anchor a query at one of OUR uncovered bakeries (`ll=@lat,lon,15z`,
      generic q "boulangerie patisserie") — Google returns the ~20 nearest;
    * every returned listing is written (flush per query, dedup by cid);
    * every OUR-target within COVER_M of any written listing is marked
      covered and never becomes an anchor itself;
    * anchors are visited densest-neighbourhood-first, so one query in
      central Marseille can cover a dozen targets while a village anchor
      covers its whole commune.

Worst case is one query per never-covered target; the default credit ceiling
(--max-pool-used) stops the run long before the account cap, keeping a
reserve for the targeted second pass and m2_s8.

This script DOES NOT match. Listings go to places_listings.csv and
m2_s7_match.py decides which SIRET, if any, each one belongs to — geo rules,
ambiguity = rejection, same as OSM and Pages Jaunes. A name alone never
writes a phone onto a row.

--named MODE (V4). The geo sweep returns the ~20 nearest shops per anchor, so
a business in a dense street can be crowded out of every anchor that covered
it. `--named` asks for it BY NAME ("{enseigne} {commune}") instead. Measured
2026-08-13: 13 of 15 such queries returned a phone — but one asked for
`COMPAGNIE BOULANGERE` and got "Boulanger Aubagne", an ELECTRONICS retailer.
That is why named results are written as listings like every other source and
handed to `m2_s7_match.py`, which requires geo + name agreement and rejects
ambiguity. A name query never writes a phone onto a row by itself.

Usage:
    python scripts/m2_s13_places.py --pilot 5      # ~15 credits, then STOP
    python scripts/m2_s13_places.py                # full sweep
    python scripts/m2_s13_places.py --max-pool-used 2200
    python scripts/m2_s13_places.py --named --pilot 20   # by-name, phoneless rows
"""

import argparse
import csv
import logging
import math
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m2lib_search import (search, quota_state, QuotaExceeded,   # noqa: E402
                          SearchAuthError)

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
OUT_PATH = CHECK_DIR / "places_listings.csv"
DONE_PATH = CHECK_DIR / "places_anchors_done.txt"
MATCHED_PATH = CHECK_DIR / "matched.csv"
NAMED_DONE_PATH = CHECK_DIR / "places_named_done.txt"

QUERY = "boulangerie patisserie"
ZOOM = "15z"
COVER_M = 300.0        # a listing this close to a target = the target had its
                       # chance to appear; it will not be anchored later
DENSITY_M = 500.0      # neighbourhood radius for the anchor ordering
MAX_POOL_USED = 2200   # default serper-credit ceiling: keeps ~300 in reserve

FIELDNAMES = ["listing_id", "name", "phone", "website", "address", "postcode",
              "city", "lat", "lon", "rating", "category", "anchor_siret",
              "email", "facebook", "siret"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s13")


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# Google Maps lets a business register its Facebook or Instagram page as its
# "website". Left alone, `facebook.com` ends up in the deliverable's Site web
# column for a dozen bakeries and m2_s9 wastes a crawl on it. OSM had the same
# habit and m2_s5 already splits them — this does the same, one layer earlier.
SOCIAL_HOSTS = ("facebook.com", "fb.com", "fb.me", "instagram.com",
                "linkedin.com", "tiktok.com", "twitter.com", "x.com")


def split_social(url: str) -> tuple:
    """(website, facebook) — a social URL is not a website."""
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if any(h in host for h in SOCIAL_HOSTS):
        return "", url
    return url, ""


def parse_cp_city(address: str) -> tuple:
    """'45 Rue Davso, 13001 Marseille, France' -> ('13001', 'Marseille').

    Some maps listings carry no CP or no address at all — then geo matching
    carries the row (lat/lon is always present on a maps listing).
    """
    m = re.search(r"\b(\d{5})\s+([^,]+)", address or "")
    return (m.group(1), m.group(2).strip()) if m else ("", "")


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def flush(rows: list) -> None:
    new = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";",
                           quoting=csv.QUOTE_MINIMAL)
        if new:
            w.writeheader()
        w.writerows(rows)
        fh.flush()


class Grid:
    """~100 m buckets so coverage checks aren't O(targets x listings)."""

    def __init__(self):
        self.cells = {}

    def _key(self, lat, lon):
        return (round(lat / 0.0009), round(lon / 0.0012))

    def add(self, lat, lon):
        self.cells.setdefault(self._key(lat, lon), []).append((lat, lon))

    def any_within(self, lat, lon, radius_m) -> bool:
        k0, k1 = self._key(lat, lon)
        span = int(radius_m / 100) + 1
        for dk0 in range(-span, span + 1):
            for dk1 in range(-span, span + 1):
                for (la, lo) in self.cells.get((k0 + dk0, k1 + dk1), ()):
                    if haversine_m(lat, lon, la, lo) <= radius_m:
                        return True
        return False


def run_named(args) -> None:
    """Ask for phoneless businesses BY NAME; write listings, decide nothing."""
    with OURS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        ours = list(csv.DictReader(fh, delimiter=";"))
    have = set()
    if MATCHED_PATH.exists():
        with MATCHED_PATH.open(encoding="utf-8-sig", newline="") as fh:
            have = {r["siret"] for r in csv.DictReader(fh, delimiter=";") if r["phone"]}
    done = load_named_done()
    seen_cids = set()
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            seen_cids = {r["listing_id"] for r in csv.DictReader(fh, delimiter=";")}

    todo = [r for r in ours if r["siret"] not in have and r["siret"] not in done]
    EFF = {"": 0, "0 salarie": 1, "1 a 2 salaries": 2, "3 a 5 salaries": 3,
           "6 a 9 salaries": 4, "10 a 19 salaries": 5, "20 a 49 salaries": 6,
           "50 a 99 salaries": 7, "100 a 199 salaries": 8}
    todo.sort(key=lambda r: (-EFF.get(r["tranche_effectif"], 9),
                             0 if r["enseigne"] else 1))
    if args.pilot:
        todo = todo[:args.pilot]
    log.info(f"{len(ours)} businesses | {len(have)} already have a phone | "
             f"{len(done)} already name-queried -> {len(todo)} to do")

    queries = written = with_phone = 0
    try:
        for i, r in enumerate(todo, 1):
            pool = quota_state()["serper"]["used"]
            if pool + 3 > args.max_pool_used:
                log.info(f"credit ceiling reached ({pool}/{args.max_pool_used}) — stopping cleanly")
                break
            label = r["enseigne"] or r["raison_sociale"]
            rows = search(f'{label} {r["commune"]}', "serper_maps", max_results=4)
            queries += 1
            out = []
            for L in rows:
                cid = L["source_id"]
                if not cid or cid in seen_cids:
                    continue
                seen_cids.add(cid)
                cp, city = parse_cp_city(L["address"])
                site, fb = split_social(L["url"])
                out.append({
                    "listing_id": cid, "name": L["title"], "phone": L["phone"],
                    "website": site, "address": L["address"],
                    "postcode": cp, "city": city,
                    "lat": L["lat"], "lon": L["lon"], "rating": L["rating"],
                    "category": L["snippet"], "anchor_siret": r["siret"],
                    "email": "", "facebook": fb, "siret": "",
                })
            if out:
                flush(out)
                written += len(out)
                with_phone += sum(1 for o in out if o["phone"])
            with NAMED_DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(r["siret"] + "\n")
            if i % 20 == 0 or out:
                log.info(f"[{i}/{len(todo)}] {label[:24]:24.24} -> {len(out)} new "
                         f"(phones {with_phone}) pool={quota_state()['serper']['used']}")
    except QuotaExceeded as exc:
        log.warning(f"STOP: {exc}")
    except SearchAuthError as exc:
        sys.exit(f"auth: {exc}")

    log.info("─" * 62)
    log.info(f"named queries: {queries} ({queries * 3} credits) | new listings "
             f"{written}, of which {with_phone} carry a phone")
    log.info(f"serper pool: {quota_state()['serper']['used']}/2500")
    log.info("Next: python scripts/m2_s7_match.py --report-rejects   "
             "(the matcher decides — a name query proves nothing on its own)")


def load_named_done() -> set:
    return (set(NAMED_DONE_PATH.read_text(encoding="utf-8").split())
            if NAMED_DONE_PATH.exists() else set())


def main() -> None:
    ap = argparse.ArgumentParser(description="Serper /maps sweep over dept-13 bakeries")
    ap.add_argument("--pilot", type=int, default=0, help="stop after N queries")
    ap.add_argument("--max-pool-used", type=int, default=MAX_POOL_USED,
                    help="stop when the serper pool counter reaches this")
    ap.add_argument("--named", action="store_true",
                    help="query phoneless businesses BY NAME instead of by anchor")
    args = ap.parse_args()

    if args.named:
        if not OURS_PATH.exists():
            sys.exit(f"{OURS_PATH} not found — run scripts/m2_s2_transform.py first.")
        return run_named(args)

    if not OURS_PATH.exists():
        sys.exit(f"{OURS_PATH} not found — run scripts/m2_s2_transform.py first.")
    with OURS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        ours = list(csv.DictReader(fh, delimiter=";"))
    targets = []
    for r in ours:
        try:
            targets.append({"siret": r["siret"],
                            "lat": float(r["latitude"]),
                            "lon": float(r["longitude"])})
        except (ValueError, KeyError):
            pass    # 4 rows have no geocode; m2_s8's name search is their path
    log.info(f"{len(ours)} businesses, {len(targets)} geocoded targets")

    # Resume state: listings already on disk cover targets; anchors already
    # attempted are never re-queried (even if they yielded nothing).
    listing_grid = Grid()
    seen_cids = set()
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                seen_cids.add(r["listing_id"])
                try:
                    listing_grid.add(float(r["lat"]), float(r["lon"]))
                except ValueError:
                    pass
    done = load_done()
    log.info(f"resume: {len(seen_cids)} listings on disk, {len(done)} anchors done")

    # Densest neighbourhood first: one central-Marseille query can cover a
    # dozen targets; the sparse tail is cut by the credit ceiling, not luck.
    tgrid = Grid()
    for t in targets:
        tgrid.add(t["lat"], t["lon"])
    for t in targets:
        t["density"] = sum(
            1 for u in targets
            if abs(u["lat"] - t["lat"]) < 0.006 and abs(u["lon"] - t["lon"]) < 0.008
            and haversine_m(t["lat"], t["lon"], u["lat"], u["lon"]) <= DENSITY_M)
    targets.sort(key=lambda t: -t["density"])

    queries = written = 0
    covered_skips = 0
    try:
        for t in targets:
            if t["siret"] in done:
                continue
            if listing_grid.any_within(t["lat"], t["lon"], COVER_M):
                covered_skips += 1
                continue
            pool_used = quota_state()["serper"]["used"]
            if pool_used + 3 > args.max_pool_used:
                log.info(f"credit ceiling reached ({pool_used}/{args.max_pool_used}) — stopping cleanly")
                break
            if args.pilot and queries >= args.pilot:
                log.info(f"pilot cap ({args.pilot} queries) reached — stopping")
                break

            rows = search(QUERY, "serper_maps", max_results=20,
                          ll=f"@{t['lat']:.6f},{t['lon']:.6f},{ZOOM}")
            queries += 1
            out = []
            for L in rows:
                cid = L["source_id"]
                if not cid or cid in seen_cids:
                    continue
                seen_cids.add(cid)
                cp, city = parse_cp_city(L["address"])
                site, fb = split_social(L["url"])
                out.append({
                    "listing_id": cid, "name": L["title"], "phone": L["phone"],
                    "website": site, "address": L["address"],
                    "postcode": cp, "city": city,
                    "lat": L["lat"], "lon": L["lon"], "rating": L["rating"],
                    "category": L["snippet"], "anchor_siret": t["siret"],
                    "email": "", "facebook": fb, "siret": "",
                })
                try:
                    listing_grid.add(float(L["lat"]), float(L["lon"]))
                except (TypeError, ValueError):
                    pass
            if out:
                flush(out)
                written += len(out)
            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(t["siret"] + "\n")
            log.info(f"[q{queries}] anchor {t['siret']} density={t['density']:>2} "
                     f"-> {len(rows)} listings, {len(out)} new "
                     f"(phones {sum(1 for o in out if o['phone'])}) "
                     f"written={written} pool={quota_state()['serper']['used']}")
    except QuotaExceeded as exc:
        log.warning(f"STOP: {exc}")
    except SearchAuthError as exc:
        sys.exit(f"auth: {exc}")

    log.info("─" * 62)
    log.info(f"queries this run: {queries} ({queries * 3} credits) | "
             f"new listings written: {written}")
    log.info(f"targets skipped as already covered: {covered_skips}")
    log.info(f"serper pool: {quota_state()['serper']['used']}/2500")
    log.info("Next: python scripts/m2_s7_match.py --report-rejects")


if __name__ == "__main__":
    main()
