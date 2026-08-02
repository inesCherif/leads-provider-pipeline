r"""
M1-S9-J — Scrape public agricultural directories for contact details
=====================================================================
THE CORRECTION THIS SCRIPT EXISTS FOR (2026-08-02)

S9-4 measured four ways of answering "does this farm have its own website" and
all four came back near zero, so domain discovery was cancelled for agriculture.
That conclusion was right — and the wrong question.

    "Does this farm have a website?"      ~0%   (measured four ways)
    "Has someone PUBLISHED this farm's contact details?"   very different

A farmer who would never build a website will happily register on
bienvenue-a-la-ferme to sell their produce. Measured on the first six listing
pages opened: **6 of 6 carried a real email address.**

    la-ferme-de-la-croze          martin-claire@hotmail.fr
    durand-pierre                pierredurand@gmail.com
    ferme-escarain                lafermeescarain@gmail.com
    ferme-du-moulin-neuf          fermedumoulinneuf@gmail.com
    domaine-des-arnasseaux        domaine.des.arnasseaux@orange.fr
    chevrerie-la-petite-caulette  morelsophie@gmail.com

And the first one checked against our base is a business we already ship WITH NO
EMAIL — 'LA FERME DE LA CROZE', 07110, phone only. That is the whole thesis in
one row.

SCALE: 5,386 listings, 10 per page, deep pagination confirmed working to page
400+. Each listing also carries a CONTACT PERSON ('Hélène et René Coste'), which
is separately useful — contact-name coverage is 78.7%.

WHAT A LISTING GIVES US, AND WHAT IT DOES NOT
    name, contact person, department, commune, email, phone, website.
    NO postcode and NO SIRET. So matching is name-similarity within a
    department, which is a judgement call with a real false-positive cost:
    attaching a stranger's email to a company is exactly the class of error
    m1_s4's exact-SIREN guard exists to prevent.

    THEREFORE THIS SCRIPT ONLY SCRAPES. It writes to staging.directory_listings
    and never touches staging.companies or staging.emails. Matching is a
    separate, measurable step so its precision can be sampled before anything
    reaches the deliverable.

POLITENESS
    bienvenue-a-la-ferme.com serves no robots.txt (404) and declares no
    restrictions. This still throttles hard: one page at a time, a real
    User-Agent, and a delay between requests. We are a guest.

IDEMPOTENT
    UNIQUE (source, external_id) + ON CONFLICT DO NOTHING, so re-running only
    adds listings not already stored. Safe to interrupt and resume.

Usage:
    python scripts/m1_s9j_annuaires.py --pages 5  --dry-run
    python scripts/m1_s9j_annuaires.py --pages 40           # ~400 listings
    python scripts/m1_s9j_annuaires.py --pages 539          # everything
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
    from playwright.sync_api import sync_playwright
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\n"
             "pip install -r requirements.txt && python -m playwright install chromium")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m1_s9j")

SCRIPT_NAME = "m1_s9j_annuaires.py"
SOURCE = "bienvenue_a_la_ferme"
BASE = "https://www.bienvenue-a-la-ferme.com"
SEARCH = BASE + "/fr/recherche?what=&where=&page={page}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PAGE_DELAY = 1.2        # between search pages
LISTING_DELAY = 0.8     # between listing pages
NAV_TIMEOUT = 40_000

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TEL_RE = re.compile(r"tel:(\+?[0-9 .()-]{9,})")
# '/fr/<region>/<departement>/<commune>/ferme/<slug>/<id>'
LISTING_RE = re.compile(r"^/fr/([a-z0-9-]+)/([a-z0-9-]+)/([a-z0-9-]+)/[a-z-]+/([a-z0-9-]+)/(\d+)")
# The listing header renders as 'Ardèche / Saint-Joseph-Des-Bancs'
LOCATION_RE = re.compile(r"^\s*(.+?)\s*/\s*(.+?)\s*$")

# Junk that appears in every page's email harvest.
EMAIL_BLOCKLIST = ("exemple.com", "example.com", "sentry.io", "mediawix.com",
                   "wixpress.com", "@2x", "chambagri.fr")

INSERT_SQL = """
INSERT INTO staging.directory_listings
    (source, external_id, url, business_name, contact_name,
     department_name, commune, email, phone, website)
VALUES %s
ON CONFLICT (source, external_id) DO NOTHING
"""

AUDIT_SQL = """
INSERT INTO audit.audit_log
    (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
VALUES ('staging.directory_listings', NULL, 'scrape', NULL, %s, %s, %s)
"""


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


def clean_email(candidates: list[str]) -> str | None:
    for e in candidates:
        low = e.lower()
        if any(b in low for b in EMAIL_BLOCKLIST):
            continue
        if low.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif")):
            continue
        return low
    return None


def parse_listing(page, path: str) -> dict | None:
    """Fetch one listing page and pull out the contact block."""
    m = LISTING_RE.match(path)
    if not m:
        return None
    _region, dept_slug, commune_slug, slug, ext_id = m.groups()

    page.goto(BASE + path, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(700)
    html = page.content()
    text = page.inner_text("body")

    email = clean_email(sorted(set(EMAIL_RE.findall(html))))
    tel = TEL_RE.search(html)
    site = re.search(
        r'href="(https?://(?!.*bienvenue-a-la-ferme)(?!.*facebook)(?!.*instagram)'
        r'(?!.*chambres-agriculture)(?!.*google)[^"]+)"', html)

    # Department and commune come from the URL SLUGS, never the page text. The
    # text version was parsed by walking lines around a '<Dept> / <Commune>'
    # header, which silently picked up the cookie banner on some pages and
    # produced communes like 'CGU."'. The slug is structural and always right.
    dept = dept_slug.replace("-", " ").title()
    commune = commune_slug.replace("-", " ").title()

    # The consent dialog also renders an <h1>, so the first one is not the
    # business. Drop anything that looks like the privacy banner.
    def _texts(selector: str) -> list[str]:
        out = []
        for el in page.query_selector_all(selector):
            t = (el.inner_text() or "").strip().replace("\n", " ")
            if t and not re.search(r"vie priv|cookie|consent", t, re.I):
                out.append(t)
        return out

    heads = _texts("h1") or _texts("h2")
    name = heads[0] if heads else slug.replace("-", " ")
    contacts = _texts("[class*=contact]")
    contact = contacts[0] if contacts else None
    # The contact block can repeat the phone/site line; keep only a person-ish
    # first line and never the phone row.
    if contact and re.search(r"T[ée]l\.|Site internet|@", contact):
        contact = None

    return {
        "external_id": ext_id,
        "url": BASE + path,
        "business_name": name.strip()[:300],
        "contact_name": (contact or "").strip()[:200] or None,
        "department_name": dept[:100],
        "commune": commune[:150],
        "email": email,
        "phone": (tel.group(1).strip() if tel else None),
        "website": (site.group(1)[:400] if site else None),
    }


def run(args) -> None:
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("SELECT external_id FROM staging.directory_listings WHERE source=%s",
                (SOURCE,))
    known = {r[0] for r in cur.fetchall()}
    log.info("Already stored for %s: %d listings", SOURCE, len(known))

    collected, stats = [], {"pages": 0, "seen": 0, "skipped": 0,
                            "with_email": 0, "with_contact": 0, "errors": 0}
    started = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="fr-FR",
                                  timezone_id="Europe/Paris")
        page = ctx.new_page()

        for pno in range(1, args.pages + 1):
            try:
                page.goto(SEARCH.format(page=pno), timeout=NAV_TIMEOUT,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(900)
                hrefs = page.eval_on_selector_all(
                    "a[href]", "e=>e.map(x=>x.getAttribute('href'))")
            except Exception as exc:
                log.warning("  search page %d failed: %s", pno, exc)
                stats["errors"] += 1
                continue

            paths, seen_here = [], set()
            for h in hrefs or []:
                if not h:
                    continue
                clean = h.split("?")[0]
                if LISTING_RE.match(clean) and clean not in seen_here:
                    seen_here.add(clean)
                    paths.append(clean)
            stats["pages"] += 1

            for path in paths:
                ext_id = LISTING_RE.match(path).group(5)
                stats["seen"] += 1
                if ext_id in known:
                    stats["skipped"] += 1
                    continue
                try:
                    rec = parse_listing(page, path)
                except Exception as exc:
                    log.warning("    listing %s failed: %s", path[-30:], exc)
                    stats["errors"] += 1
                    continue
                if not rec:
                    continue
                known.add(ext_id)
                if rec["email"]:
                    stats["with_email"] += 1
                if rec["contact_name"]:
                    stats["with_contact"] += 1
                collected.append(rec)
                time.sleep(LISTING_DELAY)

            if pno % 5 == 0 or pno == args.pages:
                rate = stats["seen"] / max(time.time() - started, 0.001)
                log.info("  page %d/%d  listings=%d  new=%d  email=%d  %.1f/s",
                         pno, args.pages, stats["seen"], len(collected),
                         stats["with_email"], rate)
            time.sleep(PAGE_DELAY)

        browser.close()

    log.info("")
    log.info("  pages scanned      %5d", stats["pages"])
    log.info("  listings seen      %5d", stats["seen"])
    log.info("  already stored     %5d", stats["skipped"])
    log.info("  NEW collected      %5d", len(collected))
    log.info("  ...with an email   %5d  (%.0f%%)", stats["with_email"],
             100 * stats["with_email"] / max(len(collected), 1))
    log.info("  ...with a contact  %5d", stats["with_contact"])
    log.info("  errors             %5d", stats["errors"])
    for r in collected[:10]:
        log.info("    %-38s %-26s %s", r["business_name"][:38],
                 r["email"] or "-", r["commune"][:24])

    if args.dry_run:
        log.info("DRY-RUN — nothing written.")
        conn.rollback(); conn.close(); return

    if collected:
        rows = [(SOURCE, r["external_id"], r["url"], r["business_name"],
                 r["contact_name"], r["department_name"], r["commune"],
                 r["email"], r["phone"], r["website"]) for r in collected]
        written = 0
        CHUNK = 200
        for i in range(0, len(rows), CHUNK):
            psycopg2.extras.execute_values(
                cur, INSERT_SQL, rows[i:i + CHUNK], page_size=CHUNK)
            written += cur.rowcount
        log.info("  rows inserted %d", written)
        cur.execute(AUDIT_SQL, (
            json.dumps({"step": "S9-J", "script": SCRIPT_NAME, "source": SOURCE,
                        **stats, "inserted": written}, ensure_ascii=False),
            SCRIPT_NAME,
            "Scrape bienvenue-a-la-ferme listings into staging.directory_listings "
            "(scrape only; matching is a separate step)",
        ))
        conn.commit()

    cur.execute("""SELECT count(*), count(email), count(contact_name)
                   FROM staging.directory_listings WHERE source=%s""", (SOURCE,))
    tot, em, ct = cur.fetchone()
    log.info("Verified — %s: %d listings stored, %d with an email, %d with a contact",
             SOURCE, tot, em, ct)
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape agricultural directories")
    ap.add_argument("--pages", type=int, default=5,
                    help="search pages to walk (10 listings each; 539 = all)")
    ap.add_argument("--dry-run", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
