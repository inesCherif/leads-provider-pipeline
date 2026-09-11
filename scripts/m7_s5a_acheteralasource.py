"""
M7-S5a — acheteralasource.com listings for one département (DB-free)
=====================================================================
The biggest direct-sales directory measured for 03/63 (2026-09-11: 404
producers in 63, 371 in 03). Server-rendered React markup with schema.org
microdata on the fiche: `itemProp="telephone"`, `streetAddress`,
`addressLocality`, `postalCode`, `description`; the website sits in the
`<a class="c015" target="_blank">` right after the phone; the contact
PERSON and a second number are only ever inside the description text
("contacter Jean-Luc Lafont au 06 63 70 05 04"). No e-mail on most fiches.

robots.txt (read 2026-09-11) disallows `/recherche*` and `/trouver/` only;
the département listing pages and the fiches are allowed and are the only
URLs opened here. One request every 0.8–1.2 s.

Index : /producteurs-en-france/all/departement/<dd>/page/N  (50 cards/page,
        card = href /producteur/<id>, name, categories, city, dept)
Fiche : /producteur/<id>

Output : checkpoints/aas_listings.csv (';', m7_lib.LISTING_FIELDS) — source
         'acheteralasource' in m7_s8 SOURCES. Listings whose name or whole
         category hits the principle (wine, beer, charcuterie) are skipped
         and counted, never written.
Done   : aas_pages_done.txt (<dept>:<page>), aas_index.csv, aas_listings_done.txt

Usage:
    python scripts/m7_s5a_acheteralasource.py --departement 63 --pilot 10 --dump
    python scripts/m7_s5a_acheteralasource.py --departement 63
    python scripts/m7_s5a_acheteralasource.py --departement 03
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
from m2lib_contact import normalize_fr_phone, extract_phones                 # noqa: E402
from m7_lib import (CHECK_DIR, LISTING_FIELDS, EMAIL_RE, PAGE_DELAY, LISTING_DELAY,  # noqa: E402
                    make_session, get, strip, listing_excluded, dept_of_cp,
                    read_csv, append_rows, load_done, mark_done, is_social)

BASE = "https://www.acheteralasource.com"
LIST = BASE + "/producteurs-en-france/all/departement/{dept}/page/{page}"
SOURCE = "acheteralasource"

INDEX_PATH = CHECK_DIR / "aas_index.csv"
PAGES_DONE = CHECK_DIR / "aas_pages_done.txt"
LIST_DONE = CHECK_DIR / "aas_listings_done.txt"
OUT_PATH = CHECK_DIR / "aas_listings.csv"
INDEX_FIELDS = ["listing_id", "dept", "name", "categorie", "city", "snippet"]

CARD_RE = re.compile(r'<a class="c0169" href="/producteur/(\d+)">(.*?)</a>', re.S)
NAME_RE = re.compile(r'class="c0149 c0181">(.*?)</p>', re.S)
SNIP_RE = re.compile(r'class="c0182 c0172">(.*?)</p>', re.S)
CAT_RE = re.compile(r'gories:(.*?)</p>', re.S)
CITY_RE = re.compile(r'class="c0183 c0185 c0170"[^>]*>(.*?)</p>', re.S)
PAGE_RE = re.compile(r'/departement/\d+/page/(\d+)"')
CONTACT_RE = re.compile(
    r"(?:contacter|contactez|appeler|appelez|joindre|renseignements?\s*(?::|aupr[eè]s de)?)\s+"
    r"(?:M\.|Mme|Mr|Madame|Monsieur)?\s*([A-ZÉÈ][\w'’-]+(?:\s+[A-ZÉÈ][\w'’-]+){1,2})", re.U)
EMAIL_BLOCK = ("acheteralasource", "sentry", "wix", "example", "@2x")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("m7_s5a")


def walk_index(sess, dept: str, pilot: int) -> None:
    done = load_done(PAGES_DONE)
    last = 60
    pno = 1
    while pno <= last:
        key = f"{dept}:{pno}"
        if key in done:
            pno += 1
            continue
        html = get(sess, LIST.format(dept=dept, page=pno), log)
        if not html:
            log.warning(f"[{dept}] index page {pno}: empty — stopping (re-run resumes here)")
            return
        pages = [int(p) for p in PAGE_RE.findall(html)]
        if pages:
            last = max(pages)
        m = re.search(r"(\d+)\s+r[ée]sultats", html)
        total = int(m.group(1)) if m else None
        rows = []
        for lid, card in CARD_RE.findall(html):
            nm = NAME_RE.search(card)
            cat = CAT_RE.search(card)
            city = CITY_RE.search(card)
            snip = SNIP_RE.search(card)
            rows.append({"listing_id": lid, "dept": dept,
                         "name": strip(nm.group(1)) if nm else "",
                         "categorie": strip(cat.group(1)) if cat else "",
                         "city": strip(city.group(1)).rstrip(" -") if city else "",
                         "snippet": strip(snip.group(1))[:300] if snip else ""})
        if not rows:
            log.warning(f"[{dept}] index page {pno}: no card — stopping")
            return
        append_rows(INDEX_PATH, INDEX_FIELDS, rows)
        mark_done(PAGES_DONE, key)
        log.info(f"[{dept}] index page {pno}/{last}: {len(rows)} cards (site says {total} results)")
        if pilot and len(rows) >= pilot:
            return
        pno += 1
        time.sleep(PAGE_DELAY)


def parse_fiche(html: str, idx: dict) -> dict:
    def prop(name):
        m = re.search(r'itemProp="%s"[^>]*>(.*?)</' % name, html, re.S)
        return strip(m.group(1)) if m else ""
    name = strip((re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S) or [None, idx["name"]])[1]) or idx["name"]
    desc = prop("description")
    phone = normalize_fr_phone(prop("telephone"))
    extra = sorted(p for p in extract_phones(htmlmod.unescape(desc)) if p and p != phone)
    if not phone and extra:
        phone, extra = extra[0], extra[1:]
    mobile = extra[0] if extra else ""
    site = ""
    for u in re.findall(r'<a href="([^"]+)" target="_blank" class="c015"', html):
        u = htmlmod.unescape(u)
        if "@" in u or is_social(u) or "acheteralasource" in u:
            continue
        site = u[:300]
        break
    if not site:
        for u in re.findall(r"https?://[^\s\"'<>()]+", htmlmod.unescape(desc)):
            if not is_social(u) and "acheteralasource" not in u:
                site = u.rstrip(".,;)")[:300]
                break
    emails = [e.lower() for e in EMAIL_RE.findall(htmlmod.unescape(html))
              if not any(b in e.lower() for b in EMAIL_BLOCK) and not e.lower().endswith((".png", ".jpg", ".svg"))]
    m = CONTACT_RE.search(htmlmod.unescape(desc))
    contact = m.group(1).strip() if m else ""
    cp = prop("postalCode")
    return {"listing_id": idx["listing_id"], "dept": dept_of_cp(cp) or idx["dept"], "name": name[:200], "alt_name": contact[:80],
            "phone": phone, "mobile": mobile, "email": emails[0] if emails else "",
            "website": site, "city": prop("addressLocality") or idx["city"], "postcode": cp,
            "address": prop("streetAddress"), "url": f"{BASE}/producteur/{idx['listing_id']}",
            "description": desc[:1500], "productions": "", "categorie": idx["categorie"], "siret": ""}


def main() -> None:
    ap = argparse.ArgumentParser(description="acheteralasource.com listings for one dept")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--pilot", type=int, default=0, help="stop after N fiches")
    ap.add_argument("--dump", action="store_true", help="save the first fiche's HTML")
    args = ap.parse_args()
    dept = args.departement
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    sess = make_session()

    walk_index(sess, dept, args.pilot)
    index = [r for r in read_csv(INDEX_PATH) if r["dept"] == dept]
    seen, todo = set(), []
    for r in index:
        if r["listing_id"] not in seen:
            seen.add(r["listing_id"])
            todo.append(r)
    done = load_done(LIST_DONE)
    todo = [r for r in todo if r["listing_id"] not in done]
    if args.pilot:
        todo = todo[:args.pilot]
    log.info(f"[{dept}] index: {len(seen)} listings, {len(todo)} fiches to open")

    n = skipped = w_email = w_phone = w_site = w_contact = 0
    for i, r in enumerate(todo, 1):
        if listing_excluded(r["name"], r["categorie"]):
            skipped += 1
            mark_done(LIST_DONE, r["listing_id"])
            log.info(f"[{i}/{len(todo)}] EXCLU {r['name'][:40]} | {r['categorie'][:50]}")
            continue
        html = get(sess, f"{BASE}/producteur/{r['listing_id']}", log)
        if not html:
            continue
        if args.dump and n == 0:
            (CHECK_DIR / "aas_sample.html").write_text(html, encoding="utf-8")
        row = parse_fiche(html, r)
        if listing_excluded(row["name"], row["categorie"], row["website"]):
            skipped += 1
            mark_done(LIST_DONE, r["listing_id"])
            log.info(f"[{i}/{len(todo)}] EXCLU {row['name'][:40]} | {row['website'][:40]}")
            time.sleep(LISTING_DELAY)
            continue
        append_rows(OUT_PATH, LISTING_FIELDS, [row])
        mark_done(LIST_DONE, r["listing_id"])
        n += 1
        w_email += bool(row["email"]); w_phone += bool(row["phone"]); w_site += bool(row["website"]); w_contact += bool(row["alt_name"])
        log.info(f"[{i}/{len(todo)}] {row['name'][:30]:30.30} {row['postcode']:5} {row['city'][:16]:16.16} "
                 f"{row['phone'] or '-':14} {row['mobile'] or '-':14} {row['email'] or '-':28.28} {row['alt_name'][:22]:22.22} {row['website'][:30]}")
        time.sleep(LISTING_DELAY)

    log.info("-" * 62)
    log.info(f"[{dept}] written this run: {n} | skipped on principle {skipped} | phone {w_phone} "
             f"| e-mail {w_email} | site {w_site} | contact person {w_contact}")
    log.info(f"file: {OUT_PATH} ({len(read_csv(OUT_PATH))} rows total)")


if __name__ == "__main__":
    main()
