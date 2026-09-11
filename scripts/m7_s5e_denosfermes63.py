"""
M7-S5e — De nos fermes 63 (Conseil départemental du Puy-de-Dôme), through YOUR Chrome
======================================================================================
The official directory of the département's producers (429 on 2026-09-11:
85 fromages, 31 miel, viandes, volailles, fruits-légumes…). Its category
LIST pages answer a plain `requests` GET with a JavaScript proof-of-work
challenge ("AI scrapers break the web…"); a real browser solves it by
itself, so — as for Pages Jaunes — the index is walked in a Chrome YOU
started with remote debugging, attached over CDP. The producer fiches
themselves answer `requests` normally (phone, mailto, site, address), so
only the LIST pages go through the browser.

    chrome --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile"
    (open https://denosfermes63.puy-de-dome.fr/producteurs.html once, leave the window open)

Index : /produits/<cat>/mode-vu/LIST.html?tx_solr[page]=N for every category
        EXCEPT `vins` (the principle) — cards link /tout-lannuaire/producteur/<slug>.html
Fiche : that URL (requests): itemprop telephone, mailto, itemprop url, address block.

Output : checkpoints/dnf_listings.csv (';', m7_lib.LISTING_FIELDS), source 'denosfermes63'
Done   : dnf_index.csv, dnf_pages_done.txt, dnf_listings_done.txt

Usage:
    python scripts/m7_s5e_denosfermes63.py --attach --pilot 2
    python scripts/m7_s5e_denosfermes63.py --attach
"""

import argparse
import html as htmlmod
import logging
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone, extract_phones                # noqa: E402
from m7_lib import (CHECK_DIR, LISTING_FIELDS, PAGE_DELAY, LISTING_DELAY,   # noqa: E402
                    make_session, get, strip, listing_excluded, dept_of_cp,
                    read_csv, append_rows, load_done, mark_done, is_social, EMAIL_RE)

BASE = "https://denosfermes63.puy-de-dome.fr"
CDP_URL = "http://localhost:9222"
SOURCE = "denosfermes63"
CATEGORIES = {                       # slug -> label written in `categorie`; `vins` deliberately absent
    "fromages-et-produits-laitiers": "Fromages et produits laitiers",
    "viandes-et-charcuteries": "Viandes",
    "miel-et-produits-de-la-ruche": "Miel et produits de la ruche",
    "volailles": "Volailles",
    "fruits-et-legumes": "Fruits et légumes",
    "autres": "Autres produits",
}
LIST = BASE + "/produits/{cat}/mode-vu/LIST.html?tx_solr%5Bpage%5D={page}"
SLUG_RE = re.compile(r'href="(/tout-lannuaire/producteur/([a-z0-9\-]+)\.html)')
INDEX_PATH = CHECK_DIR / "dnf_index.csv"
PAGES_DONE = CHECK_DIR / "dnf_pages_done.txt"
LIST_DONE = CHECK_DIR / "dnf_listings_done.txt"
OUT_PATH = CHECK_DIR / "dnf_listings.csv"
INDEX_FIELDS = ["slug", "path", "categorie"]
NAV_TIMEOUT = 45_000
EMAIL_BLOCK = ("puy-de-dome.fr", "sentry", "example")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("m7_s5e")


def walk_index(page, pilot: int) -> None:
    done = load_done(PAGES_DONE)
    for cat, label in CATEGORIES.items():
        empty_streak = 0
        for pno in range(1, 40):
            key = f"{cat}:{pno}"
            if key in done:
                continue
            if pilot and pno > pilot:
                break
            url = LIST.format(cat=cat, page=pno)
            slugs = []
            for attempt in range(4):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                except Exception as exc:
                    log.warning(f"{cat} p{pno}: {type(exc).__name__} — retry")
                    time.sleep(3)
                    continue
                # the proof-of-work page reloads itself once solved: wait for real content
                for _ in range(20):
                    content = page.content() or ""
                    if "I Challenge Thee" not in content and "haphash" not in content:
                        break
                    time.sleep(1.5)
                content = page.content() or ""
                seen = set()
                for path, slug in SLUG_RE.findall(content):
                    if slug not in seen:
                        seen.add(slug)
                        slugs.append({"slug": slug, "path": path, "categorie": label})
                if slugs or "Aucun résultat" in content or "0 résultat" in content:
                    break
                time.sleep(2 + attempt)
            if not slugs:
                empty_streak += 1
                log.info(f"{cat} p{pno}: no card — category done" if empty_streak == 1 else f"{cat} p{pno}: empty")
                mark_done(PAGES_DONE, key)
                if empty_streak >= 1:
                    break
                continue
            append_rows(INDEX_PATH, INDEX_FIELDS, slugs)
            mark_done(PAGES_DONE, key)
            log.info(f"{cat} p{pno}: {len(slugs)} producers")
            time.sleep(PAGE_DELAY)


def parse_fiche(html: str, idx: dict) -> dict:
    name = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    name = strip(name.group(1)) if name else idx["slug"].replace("-", " ").title()
    addr = re.search(r'annuaire--address-text">\s*<p>(.*?)</p>', html, re.S)
    lines = [strip(x) for x in re.split(r"<br\s*/?>", addr.group(1))] if addr else []
    lines = [x for x in lines if x]
    cp = city = street = ""
    for ln in lines:
        m = re.match(r"(\d{5})\s+(.*)", ln)
        if m:
            cp, city = m.group(1), strip(m.group(2)).title()
        else:
            street = (street + " " + ln).strip()
    tel = re.search(r'itemprop="telephone"[^>]*>(.*?)</', html, re.S)
    phones = sorted(p for p in extract_phones(strip(tel.group(1)) if tel else "") if p)
    emails = [e.lower() for e in re.findall(r'href="mailto:([^"?]+)"', html)
              if not any(b in e.lower() for b in EMAIL_BLOCK)]
    site = re.search(r'<a href="(https?://[^"]+)"[^>]*itemprop="url"', html)
    site = htmlmod.unescape(site.group(1)) if site and not is_social(site.group(1)) else ""
    desc = re.search(r'<div class="detail--container[^"]*">(.*?)</div>\s*</div>', html, re.S)
    desc = strip(desc.group(1))[:1500] if desc else ""
    return {"listing_id": idx["slug"], "dept": dept_of_cp(cp), "name": name[:200], "alt_name": "",
            "phone": phones[0] if phones else "", "mobile": phones[1] if len(phones) > 1 else "",
            "email": emails[0] if emails else "", "website": site[:300], "city": city, "postcode": cp,
            "address": street, "url": BASE + idx["path"], "description": desc, "productions": idx["categorie"],
            "categorie": idx["categorie"], "siret": ""}


def main() -> None:
    ap = argparse.ArgumentParser(description="De nos fermes 63 through your Chrome (CDP)")
    ap.add_argument("--attach", action="store_true", help=f"attach to YOUR running Chrome over CDP ({CDP_URL})")
    ap.add_argument("--pilot", type=int, default=0, help="N list pages per category, N fiches")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()
    CHECK_DIR.mkdir(parents=True, exist_ok=True)

    if not INDEX_PATH.exists() or args.attach:
        if not args.attach:
            sys.exit("--attach is required for the index: the LIST pages serve a JavaScript challenge to scripts. "
                     'Start Chrome with --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile" '
                     "and rerun with --attach.")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception as exc:
                sys.exit(f"could not attach to Chrome at {CDP_URL} ({exc}). Start Chrome first: "
                         'chrome --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile"')
            if not browser.contexts:
                sys.exit("attached, but Chrome has no browser context — open a window first")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            log.info(f"attached to Chrome ({len(ctx.pages)} tab(s)); current: {page.url[:80]}")
            walk_index(page, args.pilot)

    index = read_csv(INDEX_PATH)
    seen, todo = set(), []
    for r in index:                       # a producer sits in several categories: first label kept, others appended
        if r["slug"] not in seen:
            seen.add(r["slug"])
            todo.append(dict(r))
        else:
            for t in todo:
                if t["slug"] == r["slug"] and r["categorie"] not in t["categorie"]:
                    t["categorie"] += " - " + r["categorie"]
    done = load_done(LIST_DONE)
    todo = [r for r in todo if r["slug"] not in done]
    if args.pilot:
        todo = todo[:args.pilot]
    log.info(f"index: {len(seen)} producers, {len(todo)} fiches to open (requests)")
    sess = make_session()
    n = skipped = w_email = w_phone = w_site = 0
    for i, r in enumerate(todo, 1):
        html = get(sess, BASE + r["path"], log)
        if not html or "I Challenge Thee" in html:
            log.warning(f"[{i}/{len(todo)}] fiche not served to requests: {r['slug']}")
            continue
        if args.dump and n == 0:
            (CHECK_DIR / "dnf_sample.html").write_text(html, encoding="utf-8")
        row = parse_fiche(html, r)
        why = listing_excluded(row["name"], row["categorie"], row["website"], row["email"], row["description"])
        if why:
            skipped += 1
            mark_done(LIST_DONE, r["slug"])
            log.info(f"[{i}/{len(todo)}] EXCLU {row['name'][:40]} <- {why}")
            time.sleep(LISTING_DELAY)
            continue
        append_rows(OUT_PATH, LISTING_FIELDS, [row])
        mark_done(LIST_DONE, r["slug"])
        n += 1
        w_email += bool(row["email"]); w_phone += bool(row["phone"]); w_site += bool(row["website"])
        log.info(f"[{i}/{len(todo)}] {row['name'][:30]:30.30} {row['postcode']:5} {row['city'][:16]:16.16} "
                 f"{row['phone'] or '-':14} {row['mobile'] or '-':14} {row['email'] or '-':30.30} {row['website'][:28]}")
        time.sleep(LISTING_DELAY)
    log.info("-" * 62)
    log.info(f"written this run: {n} | skipped on principle {skipped} | phone {w_phone} | e-mail {w_email} | site {w_site}")
    log.info(f"file: {OUT_PATH} ({len(read_csv(OUT_PATH))} rows total)")


if __name__ == "__main__":
    main()
