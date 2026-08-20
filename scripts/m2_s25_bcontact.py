"""
M2-S25 — Harvest boulangerie.contact (bakery directory, dept 13)
=================================================================
A bakery-only national directory, Google-Maps-derived, with NO anti-bot and
every fiche SERVER-RENDERED — verified live 2026-08-20 (53/53 Marseille
fiches fetched clean; robots.txt allows /, /f/, /ville/, /departement/).
The prize is `application/ld+json`: a LocalBusiness block per fiche carrying
`telephone` (53% of the Marseille sample), sometimes `email` (9%) and
`sameAs` social/site links (19%).

TWO TRAPS, both measured on the live site — the selftest encodes them:

  * THE VISIBLE `tel:` LINK IS NOT THE SHOP'S NUMBER. Every fiche shows the
    same `tel:0972200676` — the directory's own CALL-TRACKING line. Shipping
    it would put one number on hundreds of rows (the premium-rate dedup
    disaster in new clothes). ONLY the ld+json `telephone` field counts.
  * THE BANNER COUNTS LIE. The département page claims "1 164 boulangeries";
    enumerating the paginated city pages gives ~450-550 real fiches. Crawl
    what paginates, never what the banner promises.

Rate: sequential, one request every DELAY_S — 10 concurrent broke on the
live site, 6 was clean; sequential is slower and unimpeachable. The whole
département is ~600 requests, ~8 minutes.

Ranking: `bcontact` EARNED its PHONE_RANK slot on 2026-08-20, measured the
way pagesjaunes earned 83.4%: 84.0% agreement vs pagesjaunes (213 overlaps),
88.9% vs OSM (45). Its 95-96% vs serper_places is discounted — both are
Google Maps data, same upstream, not an independent witness.

Output : exports/boulangerie/checkpoints/bcontact_listings.csv (m2_s7 input)
Resume : exports/boulangerie/checkpoints/bcontact_done.txt (fiche URL/line)

Usage:
    python scripts/m2_s25_bcontact.py --selftest
    python scripts/m2_s25_bcontact.py --pilot 20
    python scripts/m2_s25_bcontact.py
"""

import argparse
import csv
import gzip
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OUT_PATH = CHECK_DIR / "bcontact_listings.csv"
DONE_PATH = CHECK_DIR / "bcontact_done.txt"

BASE = "https://boulangerie.contact"
DEPT_URL = f"{BASE}/departement/bouches-du-rhone/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DELAY_S = 0.5
MAX_PAGES_PER_CITY = 40        # hard cap; Marseille was 3 pages of ~24

CITY_RE = re.compile(r'href="(/ville/[a-z0-9-]+/)"')
FICHE_RE = re.compile(r'href="(/f/[a-z0-9-]+/)"')
LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE)

FIELDNAMES = ["listing_id", "name", "phone", "email", "website", "facebook",
              "instagram", "address", "postcode", "city", "lat", "lon"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s25")


def get(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html",
        "Accept-Encoding": "gzip"})
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except Exception as e:
            log.warning(f"{url}: {e.__class__.__name__}: {e} (attempt {attempt})")
            time.sleep(2.0 * attempt)
    return ""


def iter_ld(html: str):
    """Every parseable ld+json object on the page, @graph flattened."""
    for m in LDJSON_RE.finditer(html):
        try:
            obj = json.loads(m.group(1).strip())
        except ValueError:
            continue
        items = obj if isinstance(obj, list) else [obj]
        for it in items:
            if isinstance(it, dict):
                yield from (g for g in it.get("@graph", []) if isinstance(g, dict))
                yield it


def parse_fiche(html: str, listing_id: str) -> dict | None:
    """One row from a fiche's ld+json — or None when there is no business.

    ONLY ld+json. The page's `tel:` link is the directory's own
    call-tracking number, printed identically on every fiche.
    """
    biz = None
    for it in iter_ld(html):
        t = it.get("@type", "")
        types = {t} if isinstance(t, str) else set(t or ())
        if types & {"LocalBusiness", "Bakery", "Store", "FoodEstablishment"}:
            biz = it
            break
    if not biz:
        return None
    addr = biz.get("address") or {}
    if isinstance(addr, str):
        addr = {"streetAddress": addr}
    geo = biz.get("geo") or {}
    same = biz.get("sameAs") or []
    if isinstance(same, str):
        same = [same]
    website = facebook = instagram = ""
    for u in same:
        lu = (u or "").lower()
        if "facebook.com" in lu and not facebook:
            facebook = u
        elif "instagram.com" in lu and not instagram:
            instagram = u
        elif lu.startswith("http") and not website \
                and "boulangerie.contact" not in lu and "google." not in lu:
            website = u
    url_field = (biz.get("url") or "").strip()
    if url_field and not website and "boulangerie.contact" not in url_field.lower():
        website = url_field
    return {
        "listing_id": listing_id,
        "name": (biz.get("name") or "").strip(),
        "phone": str(biz.get("telephone") or "").strip(),
        "email": (biz.get("email") or "").replace("mailto:", "").strip(),
        "website": website, "facebook": facebook, "instagram": instagram,
        "address": (addr.get("streetAddress") or "").strip(),
        "postcode": str(addr.get("postalCode") or "").strip()[:5],
        "city": (addr.get("addressLocality") or "").strip(),
        "lat": str(geo.get("latitude") or ""), "lon": str(geo.get("longitude") or ""),
    }


def selftest() -> None:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}: {got!r}"
              + ("" if good else f" (wanted {want!r})"))

    fiche = """
    <a href="tel:0972200676" class="call">Appeler</a>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Bakery","name":"Bonne Miette",
     "telephone":"06 31 91 21 45","email":"mailto:contact@bonnemiette.fr",
     "address":{"@type":"PostalAddress","streetAddress":"3 rue du Four",
                "postalCode":"13001","addressLocality":"Marseille"},
     "geo":{"latitude":43.29,"longitude":5.37},
     "sameAs":["https://www.facebook.com/bonnemiette",
               "https://bonnemiette.fr"]}
    </script>"""
    row = parse_fiche(fiche, "bonne-miette-1")
    check("ld+json phone wins, tel: link ignored", row["phone"], "06 31 91 21 45")
    check("mailto: stripped", row["email"], "contact@bonnemiette.fr")
    check("facebook from sameAs", row["facebook"],
          "https://www.facebook.com/bonnemiette")
    check("website from sameAs", row["website"], "https://bonnemiette.fr")
    check("postcode", row["postcode"], "13001")

    tracking_only = '<a href="tel:0972200676">Appeler</a><p>Boulangerie X</p>'
    check("no ld+json -> no row (never the tracking number)",
          parse_fiche(tracking_only, "x"), None)

    graph = ('<script type="application/ld+json">{"@graph":[{"@type":"WebSite"},'
             '{"@type":"LocalBusiness","name":"Y","telephone":"0442000000"}]}'
             '</script>')
    check("@graph flattened", parse_fiche(graph, "y")["phone"], "0442000000")
    print("selftest:", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest boulangerie.contact dept 13")
    ap.add_argument("--pilot", type=int, default=0, help="stop after N fiches")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()

    dept = get(DEPT_URL)
    cities = sorted(set(CITY_RE.findall(dept)))
    if not cities:
        sys.exit("département page yielded no city links — layout changed?")
    log.info(f"{len(cities)} city pages")

    fiches: list[str] = []
    seen_f = set()
    for ci, c in enumerate(cities, 1):
        page = 1
        while page <= MAX_PAGES_PER_CITY:
            url = f"{BASE}{c}" + (f"?page={page}" if page > 1 else "")
            html = get(url)
            found = [f for f in FICHE_RE.findall(html) if f not in seen_f]
            if not found:
                break
            seen_f.update(found)
            fiches.extend(found)
            page += 1
            time.sleep(DELAY_S)
        if ci % 20 == 0:
            log.info(f"[{ci}/{len(cities)}] cities walked, {len(fiches)} fiches so far")
        time.sleep(DELAY_S)
    log.info(f"fiche inventory: {len(fiches)} (banner numbers are inflated — "
             f"this is what actually paginates)")

    done = set()
    if DONE_PATH.exists():
        done = {l.strip() for l in DONE_PATH.read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = [f for f in fiches if f not in done]
    if args.pilot:
        todo = todo[:args.pilot]
    log.info(f"already done: {len(done)} | to read: {len(todo)}")

    stats = Counter()
    new_file = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as out_fh, \
         DONE_PATH.open("a", encoding="utf-8") as done_fh:
        w = csv.DictWriter(out_fh, fieldnames=FIELDNAMES, delimiter=";",
                           quoting=csv.QUOTE_MINIMAL)
        if new_file:
            w.writeheader()
        for i, f in enumerate(todo, 1):
            html = get(BASE + f)
            if not html:
                stats["fetch failed (will retry)"] += 1
                continue
            row = parse_fiche(html, f.strip("/").rpartition("/")[2])
            if row is None:
                stats["no ld+json business"] += 1
            else:
                w.writerow(row)
                out_fh.flush()
                stats["rows written"] += 1
                if row["phone"]:
                    stats["with a phone"] += 1
                if row["email"]:
                    stats["with an email"] += 1
                if row["website"]:
                    stats["with a website"] += 1
            done_fh.write(f + "\n")
            done_fh.flush()
            if i % 50 == 0:
                log.info(f"[{i}/{len(todo)}] {dict(stats)}")
            time.sleep(DELAY_S)

    log.info("─" * 62)
    for k, v in stats.most_common():
        log.info(f"  {k:<32} {v:>5}")
    log.info(f"output -> {OUT_PATH}")
    log.info("Next: python scripts/m2_s7_match.py  (bcontact feeds the "
             "corroboration pool until it earns a rank)")


if __name__ == "__main__":
    main()
