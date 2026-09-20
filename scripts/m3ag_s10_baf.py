"""
M3AG-S10 — bienvenue-a-la-ferme listings for one département (DB-free)
======================================================================
The S9-J thesis, re-used: a farmer who never builds a website still
registers on bienvenue-a-la-ferme to sell produce — 6/6 sampled listings
carried a real e-mail. S9-J stored 5,326 listings in Supabase, but the
project is paused, so this walks the site again with plain `requests`
(the search results are server-rendered; no browser needed) and keeps
ONLY the listings whose URL slug is in the requested département:

    /fr/<region>/<dept-slug>/<commune-slug>/ferme/<slug>/<id>

The site's `where=` filter is ignored server-side (measured 2026-09-03:
5,266 results whatever the value), so the index walk is national
(~527 pages, ~1.2 s each) but is done ONCE and cached in
`baf_index.csv`; listing pages are then opened only for our departements.

Output : checkpoints/baf_listings.csv (';') — wired into m3ag_s8 SOURCES as
         source 'bienvenue_ferme'. Columns: listing_id, dept, name,
         alt_name (contact person), phone, mobile, email, website, city,
         postcode (empty: the site prints none), address (empty).
Done   : baf_pages_done.txt (index pages), baf_index.csv (all paths seen),
         baf_listings_done.txt (listing ids parsed)

Usage:
    python scripts/m3ag_s10_baf.py --departement 63 --pilot 3      # 3 index pages
    python scripts/m3ag_s10_baf.py --departement 63
    python scripts/m3ag_s10_baf.py --departement 03                # index cached
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
import requests                                                    # noqa: E402
from m2lib_contact import normalize_fr_phone                       # noqa: E402
from m3ag_lib import CHECK_DIR, EMAIL_RE, read_csv, append_rows, load_done, mark_done  # noqa: E402

BASE = "https://www.bienvenue-a-la-ferme.com"
SEARCH = BASE + "/fr/recherche?what=&where=&page={page}"
LISTING_RE = re.compile(r"^/fr/([a-z0-9-]+)/([a-z0-9-]+)/([a-z0-9-]+)/[a-z-]+/([a-z0-9-]+)/(\d+)")
DEPT_SLUGS = {"63": ("puy-de-dome",), "03": ("allier",), "43": ("haute-loire",),
              "15": ("cantal",), "42": ("loire",), "23": ("creuse",),
              # PACA (Sam's priority, 2026-09-20) — slugs READ from the cached index, not guessed:
              # 68 / 57 / 20 / 37 / 45 / 132 listings
              "04": ("alpes-de-haute-provence",), "05": ("hautes-alpes",), "06": ("alpes-maritimes",),
              "13": ("bouches-du-rhone",), "83": ("var",), "84": ("vaucluse",)}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PAGE_DELAY = 1.2
LISTING_DELAY = 0.8
MAX_PAGES = 600

INDEX_PATH = CHECK_DIR / "baf_index.csv"
PAGES_DONE = CHECK_DIR / "baf_pages_done.txt"
LIST_DONE = CHECK_DIR / "baf_listings_done.txt"
OUT_PATH = CHECK_DIR / "baf_listings.csv"

INDEX_FIELDS = ["path", "region", "dept_slug", "commune_slug", "slug", "listing_id"]
FIELDNAMES = ["listing_id", "dept", "name", "alt_name", "phone", "mobile", "email",
              "website", "city", "postcode", "address", "url"]

EMAIL_BLOCKLIST = ("exemple.com", "example.com", "sentry.io", "wix", "@2x",
                   "chambagri.fr", "bienvenue-a-la-ferme")
SITE_SKIP = ("bienvenue-a-la-ferme", "facebook", "instagram", "chambres-agriculture",
             "google", "matomo", "unpkg", "orejime", "twitter", "youtube", "linkedin",
             "apple.com", "dnsi.io", "w3.org", "schema.org")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s10")


def get(sess, url: str) -> str:
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=40)
            if r.status_code == 200:
                return r.text
            log.warning(f"HTTP {r.status_code} {url}")
        except Exception as exc:
            log.warning(f"{type(exc).__name__} {url}")
        time.sleep(3 * (attempt + 1))
    return ""


def strip(s: str) -> str:
    return " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def walk_index(sess, pilot: int) -> None:
    done = load_done(PAGES_DONE)
    last = MAX_PAGES
    empty_streak = 0
    for pno in range(1, MAX_PAGES + 1):
        if pilot and pno > pilot:
            break
        if str(pno) in done:
            continue
        if pno > last:
            break
        # The site intermittently serves the empty app shell (HTTP 200, no
        # cards, no pagination) — measured 2026-09-03: pages 4 and 5 empty,
        # page 50 full. An empty page is retried, never marked done.
        paths = []
        for attempt in range(4):
            html = get(sess, SEARCH.format(page=pno))
            if not html:
                time.sleep(2)
                continue
            m = re.search(r"Page\s+\d+\s+sur\s+(\d+)", html)
            if m:
                last = int(m.group(1))
            seen = set()
            for h in re.findall(r'href="(/fr/[^"]+/ferme/[^"]+)"', html):
                p = htmlmod.unescape(h).split("?")[0]
                mm = LISTING_RE.match(p)
                if mm and p not in seen:
                    seen.add(p)
                    paths.append({"path": p, "region": mm.group(1), "dept_slug": mm.group(2),
                                  "commune_slug": mm.group(3), "slug": mm.group(4),
                                  "listing_id": mm.group(5)})
            if paths or (m and pno >= last):
                break
            time.sleep(2 + attempt)
        if not paths:
            empty_streak += 1
            log.warning(f"index page {pno}: empty after retries ({empty_streak} in a row)")
            if empty_streak >= 5:
                log.info("five empty pages in a row — stopping (re-run resumes here)")
                break
            time.sleep(PAGE_DELAY)
            continue
        empty_streak = 0
        append_rows(INDEX_PATH, INDEX_FIELDS, paths)
        mark_done(PAGES_DONE, str(pno))
        if pno % 25 == 0 or pilot:
            log.info(f"index page {pno}/{last}: {len(paths)} listings")
        time.sleep(PAGE_DELAY)


def parse_listing(html: str, path: str, dept: str) -> dict:
    mm = LISTING_RE.match(path)
    _region, _dept_slug, commune_slug, slug, ext_id = mm.groups()
    emails = [e.lower() for e in sorted(set(EMAIL_RE.findall(html)))
              if not any(b in e.lower() for b in EMAIL_BLOCKLIST)
              and not e.lower().endswith((".png", ".jpg", ".svg"))]
    tels = [normalize_fr_phone(t) for t in re.findall(r'href="tel:([^"]+)"', html)]
    tels = [t for t in tels if t]
    phone = tels[0] if tels else ""
    mobile = next((t for t in tels[1:] if t != phone), "")
    site = ""
    for u in re.findall(r'href="(https?://[^"]+)"', html):
        if not any(s in u.lower() for s in SITE_SKIP):
            site = u[:300]
            break
    heads = [strip(h) for h in re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S)]
    heads = [h for h in heads if h and not re.search(r"vie priv|cookie|consent", h, re.I)]
    name = heads[0] if heads else slug.replace("-", " ").title()
    # contact person: the block whose class mentions 'contact' and whose text
    # is a person-ish line (no phone/site/@) — same heuristic as m1_s9j.
    contact = ""
    for blk in re.findall(r'class="[^"]*[Cc]ontact[^"]*"[^>]*>(.*?)</', html, re.S):
        t = strip(blk)
        if t and not re.search(r"T[ée]l|Site|@|\d{2} \d{2}|contact", t, re.I) and len(t) < 80:
            contact = t
            break
    return {"listing_id": ext_id, "dept": dept, "name": name[:200], "alt_name": contact,
            "phone": phone, "mobile": mobile, "email": emails[0] if emails else "",
            "website": site, "city": commune_slug.replace("-", " ").title(),
            "postcode": "", "address": "", "url": BASE + path}


def main() -> None:
    ap = argparse.ArgumentParser(description="bienvenue-a-la-ferme listings for one dept")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--pilot", type=int, default=0, help="only N index pages")
    ap.add_argument("--dump", action="store_true", help="save the first listing's HTML")
    args = ap.parse_args()
    dept = args.departement
    slugs = DEPT_SLUGS.get(dept)
    if not slugs:
        sys.exit(f"no slug known for dept {dept} — add it to DEPT_SLUGS")

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})

    walk_index(sess, args.pilot)
    index = read_csv(INDEX_PATH)
    mine = [r for r in index if r["dept_slug"] in slugs]
    seen_ids, todo = set(), []
    for r in mine:
        if r["listing_id"] not in seen_ids:
            seen_ids.add(r["listing_id"])
            todo.append(r)
    done = load_done(LIST_DONE)
    todo = [r for r in todo if r["listing_id"] not in done]
    log.info(f"index: {len(index)} paths, {len(seen_ids)} in dept {dept}, {len(todo)} listings to open")

    n = w_email = w_phone = w_site = 0
    for i, r in enumerate(todo, 1):
        html = get(sess, BASE + r["path"])
        if not html:
            continue
        if args.dump and i == 1:
            (CHECK_DIR / "baf_sample.html").write_text(html, encoding="utf-8")
        row = parse_listing(html, r["path"], dept)
        append_rows(OUT_PATH, FIELDNAMES, [row])
        mark_done(LIST_DONE, r["listing_id"])
        n += 1
        w_email += bool(row["email"])
        w_phone += bool(row["phone"])
        w_site += bool(row["website"])
        log.info(f"[{i}/{len(todo)}] {row['name'][:30]:30.30} {row['city'][:18]:18.18} "
                 f"{row['phone'] or '-':14} {row['email'] or '-'} {row['alt_name'][:24]}")
        time.sleep(LISTING_DELAY)

    log.info("─" * 62)
    log.info(f"[{dept}] listings written this run: {n} | e-mail {w_email} | phone {w_phone} | site {w_site}")
    log.info(f"file: {OUT_PATH} ({len(read_csv(OUT_PATH))} rows total)")
    log.info("Next: python scripts/m3ag_s8_match.py --departement " + dept)


if __name__ == "__main__":
    main()
