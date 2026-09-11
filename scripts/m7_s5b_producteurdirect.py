"""
M7-S5b — producteur.direct listings (Sam's pick), any département, DB-free
==========================================================================
Next.js site: the list pages are client-rendered (useless to `requests`)
but `sitemap/1..4.xml` list every fiche as `/producteur/<slug>-<dept>`
(5,000 per file), so the index costs 5 GETs and the département is read
off the URL suffix. Each fiche is server-rendered and carries a schema.org
`LocalBusiness` JSON-LD block: name, description, telephone, email,
sameAs (website), address (locality, postalCode), geo — plus a
BreadcrumbList whose 2nd item is the site's own category (Éleveur,
Fromager, Maraîcher…). robots.txt (2026-09-11) allows `/`.

Output : checkpoints/pd_listings.csv (';', m7_lib.LISTING_FIELDS) — source
         'producteur_direct'. Listings whose name / category hit the
         principle (viticulteur, brasseur, charcuterie) are skipped and
         counted. `postcode` from the JSON-LD gives the dept; the URL
         suffix is only the pre-filter.
Done   : pd_index.csv (every slug of every dept — the France index),
         pd_listings_done.txt

Usage:
    python scripts/m7_s5b_producteurdirect.py --departement 63 --pilot 10 --dump
    python scripts/m7_s5b_producteurdirect.py --departement 63,03
    python scripts/m7_s5b_producteurdirect.py --all-depts            # France, later
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone                          # noqa: E402
from m7_lib import (CHECK_DIR, LISTING_FIELDS, PAGE_DELAY, LISTING_DELAY,  # noqa: E402
                    make_session, get, strip, listing_excluded, dept_of_cp,
                    read_csv, append_rows, load_done, mark_done, is_social)

BASE = "https://producteur.direct"
SITEMAPS = [f"{BASE}/sitemap/{i}.xml" for i in range(0, 5)]
SOURCE = "producteur_direct"
SLUG_RE = re.compile(r"/producteur/([a-z0-9-]+)-(\d{2}|2a|2b)$")

INDEX_PATH = CHECK_DIR / "pd_index.csv"
LIST_DONE = CHECK_DIR / "pd_listings_done.txt"
OUT_PATH = CHECK_DIR / "pd_listings.csv"
INDEX_FIELDS = ["slug", "dept_url", "url"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("m7_s5b")


def walk_index(sess) -> list[dict]:
    if INDEX_PATH.exists():
        return read_csv(INDEX_PATH)
    rows = []
    for sm in SITEMAPS:
        xml = get(sess, sm, log)
        if not xml:
            log.warning(f"sitemap {sm}: empty")
            continue
        n = 0
        for loc in re.findall(r"<loc>(.*?)</loc>", xml):
            m = SLUG_RE.search(loc)
            if m:
                rows.append({"slug": m.group(0).split("/")[-1], "dept_url": m.group(2).upper(), "url": loc})
                n += 1
        log.info(f"sitemap {sm.split('/')[-1]}: {n} fiches")
        time.sleep(PAGE_DELAY)
    if not rows:
        sys.exit("no fiche URL found in the sitemaps — the site changed, stop.")
    append_rows(INDEX_PATH, INDEX_FIELDS, rows)
    return rows


def parse_fiche(html: str, idx: dict) -> dict | None:
    biz = None
    for blk in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(blk)
        except ValueError:
            continue
        if d.get("@type") == "LocalBusiness":
            biz = d
        elif d.get("@type") == "BreadcrumbList":
            items = d.get("itemListElement") or []
            cat = items[1].get("name", "") if len(items) > 1 else ""
            idx["_cat"] = "" if cat.lower() in ("rechercher", "producteurs", "accueil") else cat
    if not biz:
        return None
    addr = biz.get("address") or {}
    phone = normalize_fr_phone(biz.get("telephone") or "")
    tels = [normalize_fr_phone(t) for t in re.findall(r'href="tel:([^"]+)"', html)]
    mobile = next((t for t in tels if t and t != phone), "")
    site = ""
    for u in (biz.get("sameAs") or []):
        if u and not is_social(u):
            site = u[:300]
            break
    cp = addr.get("postalCode") or ""
    return {"listing_id": idx["slug"], "dept": dept_of_cp(cp) or idx["dept_url"], "name": strip(biz.get("name", ""))[:200],
            "alt_name": "", "phone": phone, "mobile": mobile, "email": (biz.get("email") or "").lower(),
            "website": site, "city": addr.get("addressLocality") or "", "postcode": cp,
            "address": addr.get("streetAddress") or "", "url": idx["url"],
            "description": strip(biz.get("description", ""))[:1500], "productions": "",
            "categorie": idx.get("_cat", ""), "siret": ""}


def main() -> None:
    ap = argparse.ArgumentParser(description="producteur.direct listings")
    ap.add_argument("--departement", default="63", help="comma-separated, e.g. 63,03")
    ap.add_argument("--all-depts", action="store_true")
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()
    depts = None if args.all_depts else {d.strip().upper() for d in args.departement.split(",") if d.strip()}
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    sess = make_session()

    index = walk_index(sess)
    todo = [r for r in index if depts is None or r["dept_url"] in depts]
    done = load_done(LIST_DONE)
    todo = [r for r in todo if r["slug"] not in done]
    if args.pilot:
        todo = todo[:args.pilot]
    log.info(f"index: {len(index)} fiches nationally, {len(todo)} to open for {depts or 'all depts'}")

    n = skipped = w_email = w_phone = w_site = 0
    for i, r in enumerate(todo, 1):
        html = get(sess, r["url"], log)
        if not html:
            continue
        if args.dump and n == 0:
            (CHECK_DIR / "pd_sample.html").write_text(html, encoding="utf-8")
        row = parse_fiche(html, r)
        if row is None:
            log.warning(f"[{i}/{len(todo)}] no LocalBusiness block: {r['url']}")
            continue
        if listing_excluded(row["name"], row["categorie"], row["website"]):
            skipped += 1
            mark_done(LIST_DONE, r["slug"])
            log.info(f"[{i}/{len(todo)}] EXCLU {row['name'][:40]} | {row['categorie']}")
            time.sleep(LISTING_DELAY)
            continue
        append_rows(OUT_PATH, LISTING_FIELDS, [row])
        mark_done(LIST_DONE, r["slug"])
        n += 1
        w_email += bool(row["email"]); w_phone += bool(row["phone"]); w_site += bool(row["website"])
        log.info(f"[{i}/{len(todo)}] {row['name'][:30]:30.30} {row['postcode']:5} {row['city'][:16]:16.16} "
                 f"{row['phone'] or '-':14} {row['email'] or '-':30.30} {row['categorie'][:14]:14} {row['website'][:30]}")
        time.sleep(LISTING_DELAY)

    log.info("-" * 62)
    log.info(f"written this run: {n} | skipped on principle {skipped} | phone {w_phone} | e-mail {w_email} | site {w_site}")
    log.info(f"file: {OUT_PATH} ({len(read_csv(OUT_PATH))} rows total)")


if __name__ == "__main__":
    main()
