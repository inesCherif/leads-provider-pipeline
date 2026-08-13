"""
M2-S6 — Pages Jaunes harvest (approved by Sam for this sector)
===============================================================
Pages Jaunes is where French small businesses that have no website still
publish a phone, and often a site link. It returns 403 to plain HTTP requests,
so this uses Playwright with a real browser profile — the same shape that
worked for bienvenue-a-la-ferme in m1_s9j_annuaires.py.

THE RULE THAT COST A NIGHT, obeyed here: never hold state across slow work.
m1_s9j held one DB connection through 3.5 h of scraping and lost 3,402
listings when the pooler killed it. There is no DB here, and the file
equivalent is enforced the same way: rows are APPENDED AND FLUSHED after every
single page, and the log prints `written=` (rows on disk), never `collected=`
(rows in memory). A crash costs one page.

Resume: `pj_done.txt` records every completed search URL. Re-running skips
them. Kill it any time.

Politeness: PAGE_DELAY between pages, one browser, one context, headless off
on the first run if a challenge appears (--headful). This is a slow crawl over
~140 communes, not a hammering.

Output: exports/boulangerie/checkpoints/pj_listings.csv
        (name, phone, website, address, postcode, city, listing_id)
Matching to SIRET rows is m2_s7's job, and requires location agreement.

Usage:
    python scripts/m2_s6_pagesjaunes.py --pilot 3      # 3 communes, eyeball it
    python scripts/m2_s6_pagesjaunes.py                # full run (resumes)
    python scripts/m2_s6_pagesjaunes.py --headful      # watch it / clear a challenge
"""

import argparse
import csv
import logging
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
OUT_PATH  = CHECK_DIR / "pj_listings.csv"
DONE_PATH = CHECK_DIR / "pj_done.txt"
STATE_PATH = CHECK_DIR / "pj_storage_state.json"

BASE = "https://www.pagesjaunes.fr"
WHAT = "boulangerie-patisserie"
PAGE_DELAY = (3.0, 6.0)      # seconds between pages, randomised
MAX_PAGES_PER_COMMUNE = 8    # PJ paginates ~20/page; 8 covers Marseille arrondissements
NAV_TIMEOUT = 30_000

FIELDNAMES = ["listing_id", "name", "phone", "website", "address",
              "postcode", "city", "search_url"]

PHONE_RE = re.compile(r"0[1-9](?:[\s.\-]?\d{2}){4}")
AGGREGATORS = ("pagesjaunes", "pagespro", "google.", "facebook.", "instagram.",
               "tripadvisor", "ubereats", "deliveroo", "justeat", "petitfute",
               "yelp.", "mappy.", "linkedin.")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s6")


def slug(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s


def targets() -> list[tuple[str, str]]:
    """(commune, cp) pairs from OUR list, so the crawl covers exactly the
    population we must enrich — not PJ's idea of where bakeries are.
    Marseille is split per arrondissement: PJ's 'marseille-13' page will not
    paginate through 700 listings, but 13001..13016 each will."""
    if not OURS_PATH.exists():
        sys.exit(f"{OURS_PATH} not found — run scripts/m2_s2_transform.py first.")
    with OURS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    pairs = Counter((r["commune"], r["code_postal"]) for r in rows if r["commune"])
    # Busiest first: if the run is interrupted, the most valuable pages are done.
    return [cp for cp, _ in pairs.most_common()]


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def mark_done(url: str) -> None:
    with DONE_PATH.open("a", encoding="utf-8") as f:
        f.write(url + "\n")
        f.flush()


def flush_rows(rows: list[dict]) -> int:
    """Append + flush. A fresh handle per write, closed immediately — the file
    analogue of m1_s9j's fresh-connection-per-write fix."""
    if not rows:
        return 0
    new = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        if new:
            w.writeheader()
        w.writerows(rows)
        fh.flush()
    return len(rows)


def extract_listings(page, search_url: str) -> list[dict]:
    """Read the result cards. PJ's markup changes; every selector below has a
    fallback, and a card that yields neither a phone nor a site is dropped
    rather than written as an empty row."""
    out = []
    cards = page.query_selector_all("li.bi, article.bi, div.bi-generic, li[id^='bi-']")
    for c in cards:
        try:
            html = c.inner_html()
            txt = c.inner_text()
        except Exception:
            continue

        name = ""
        for sel in ("h3", "a.bi-denomination", ".denom", "h2"):
            el = c.query_selector(sel)
            if el:
                name = (el.inner_text() or "").strip()
                if name:
                    break

        phone = ""
        m = PHONE_RE.search(txt.replace(" ", " "))
        if m:
            phone = re.sub(r"[\s.\-]", " ", m.group(0)).strip()

        website = ""
        for a in c.query_selector_all("a[href^='http']"):
            href = a.get_attribute("href") or ""
            low = href.lower()
            if not any(x in low for x in AGGREGATORS):
                website = href.split("?")[0]
                break

        postcode, city = "", ""
        mm = re.search(r"\b(13\d{3})\b\s*([A-ZÀ-ÿ][^\n,]*)", txt)
        if mm:
            postcode, city = mm.group(1), mm.group(2).strip()
        addr = ""
        for sel in (".bi-address", "a.adresse", ".adresse"):
            el = c.query_selector(sel)
            if el:
                addr = " ".join((el.inner_text() or "").split())
                break

        if not (phone or website):
            continue
        lid = ""
        mid = re.search(r'id="bi-([^"]+)"', html) or re.search(r"data-pjid=\"([^\"]+)\"", html)
        if mid:
            lid = mid.group(1)
        out.append({
            "listing_id": lid, "name": name, "phone": phone, "website": website,
            "address": addr, "postcode": postcode, "city": city,
            "search_url": search_url,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest Pages Jaunes for dept-13 bakeries")
    ap.add_argument("--pilot", type=int, default=0, help="only the N busiest communes")
    ap.add_argument("--headful", action="store_true", help="visible browser (clear a challenge)")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright required:\n  pip install playwright\n  playwright install chromium")

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    todo = targets()
    if args.pilot:
        todo = todo[:args.pilot]
    done = load_done()
    log.info(f"{len(todo)} commune(s) targeted, {len(done)} search URLs already done")

    written_total = 0
    blocked = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        ctx_args = {
            "locale": "fr-FR",
            "timezone_id": "Europe/Paris",
            "viewport": {"width": 1366, "height": 900},
            "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        }
        if STATE_PATH.exists():
            ctx_args["storage_state"] = str(STATE_PATH)
        ctx = browser.new_context(**ctx_args)
        page = ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        for commune, cp in todo:
            where = f"{slug(commune)}-{cp}"
            for pageno in range(1, MAX_PAGES_PER_COMMUNE + 1):
                url = f"{BASE}/annuaire/chercherlespros?quoiqui={WHAT}&ou={where}&page={pageno}"
                if url in done:
                    continue
                try:
                    # domcontentloaded, not load: 'load' hangs on stuck third-party
                    # widgets, which is how the agriculture scrape stalled.
                    resp = page.goto(url, wait_until="domcontentloaded")
                    status = resp.status if resp else 0
                except Exception as exc:
                    log.warning(f"{where} p{pageno}: {type(exc).__name__} — skipping")
                    break

                if status in (403, 429) or "datadome" in (page.content() or "").lower():
                    blocked += 1
                    log.warning(f"{where} p{pageno}: BLOCKED (status {status}). "
                                "Re-run with --headful to clear the challenge once; "
                                "state is persisted afterwards.")
                    break

                # Consent banner, first page only in practice.
                for sel in ("#didomi-notice-agree-button", "button#onetrust-accept-btn-handler",
                            "text=Tout accepter"):
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click(timeout=2000)
                            break
                    except Exception:
                        pass

                rows = extract_listings(page, url)
                n = flush_rows(rows)          # <- on disk before anything else
                written_total += n
                mark_done(url)
                log.info(f"{where} p{pageno}: written={n} (run total {written_total})")
                if n == 0:
                    break                      # no more result pages for this commune
                time.sleep(random.uniform(*PAGE_DELAY))

        try:
            ctx.storage_state(path=str(STATE_PATH))
        except Exception:
            pass
        ctx.close()
        browser.close()

    log.info("─" * 62)
    log.info(f"written={written_total} rows this run -> {OUT_PATH}")
    if blocked:
        log.warning(f"{blocked} page(s) were blocked. Pages Jaunes uses DataDome; "
                    "a --headful run to solve one challenge usually unlocks the rest.")
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            allrows = list(csv.DictReader(fh, delimiter=";"))
        log.info(f"file now holds {len(allrows)} rows: "
                 f"{sum(1 for r in allrows if r['phone'])} phones, "
                 f"{sum(1 for r in allrows if r['website'])} websites")
    log.info("Next: python scripts/m2_s7_match.py")


if __name__ == "__main__":
    main()
