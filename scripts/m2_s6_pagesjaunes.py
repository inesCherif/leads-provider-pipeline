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

ATTACH MODE (V5, 2026-08-13). Every LAUNCHED browser — bundled Chromium and
real Chrome alike — was detected by Cloudflare, and a human-solved
`cf_clearance` did not survive automated navigation. The one untested free
route: the human runs her OWN Chrome with remote debugging and solves the
challenge as a normal visitor; this script then ATTACHES to that trusted,
living session over CDP and never launches anything.

    1. Close all Chrome windows, then start:
       "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
           --remote-debugging-port=9222 ^
           --user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile"
    2. Browse to pagesjaunes.fr YOURSELF, pass the challenge, check a search
       for "boulangerie marseille" really shows results.
    3. python scripts/m2_s6_pagesjaunes.py --attach --pilot 1 --dump-html
       -> inspect the saved HTML, fix selectors if reality differs, scale up.

The card selectors were rewritten 2026-08-13 from a WORKING scraper of Sam's:
cards are `li.bi.bi-generic`, the name sits in `.bi-denomination h3`, and —
critical — **phone numbers are hidden behind an "Afficher le N°" button** and
only appear in `#bi-fantomas-<id>` / `.number-contact` after a click. The
previous selectors were blind guesses that would have found almost no phones
even past Cloudflare.

Usage:
    python scripts/m2_s6_pagesjaunes.py --attach --pilot 1 --dump-html  # first
    python scripts/m2_s6_pagesjaunes.py --attach                        # full crawl
    python scripts/m2_s6_pagesjaunes.py --headful   # LAST-RESORT fallback:
                                                    # launched Chrome, solve by hand
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
# A persistent Chrome profile, not a storage-state file. MEASURED 2026-08-13:
# Pages Jaunes is behind CLOUDFLARE now, not DataDome as the V2 notes say, and
# a Cloudflare managed challenge inspects the browser itself — bundled
# Chromium announces `navigator.webdriver` and CDP artifacts, so the challenge
# loops forever no matter how many times a human clicks it. Real LAUNCHED
# Chrome let a human solve the challenge, but the clearance did NOT survive
# automated navigation — Cloudflare binds it to a live fingerprint, not a
# cookie. Hence --attach (above); this profile serves only the launched
# fallback modes.
PROFILE_DIR = CHECK_DIR / "pj_chrome_profile"

BASE = "https://www.pagesjaunes.fr"
WHAT = "boulangerie-patisserie"
PAGE_DELAY = (3.0, 6.0)      # seconds between pages, randomised
MAX_PAGES_PER_COMMUNE = 8    # PJ paginates ~20/page; 8 covers Marseille arrondissements
NAV_TIMEOUT = 30_000

FIELDNAMES = ["listing_id", "name", "phone", "mobile", "website", "address",
              "postcode", "city", "category", "detail_url", "search_url"]

PHONE_RE = re.compile(r"0[1-9](?:[\s.\-]?\d{2}){4}")
NBSP = " "          # PJ prints numbers with non-breaking spaces
AGGREGATORS = ("pagesjaunes", "pagespro", "google.", "facebook.", "instagram.",
               "tripadvisor", "ubereats", "deliveroo", "justeat", "petitfute",
               "yelp.", "mappy.", "linkedin.")

# Stop after this many communes blocked back to back. Without it a blocked run
# walks all ~119 communes hitting the same wall on page 1, wasting an hour to
# learn what the third commune already proved.
MAX_CONSECUTIVE_BLOCKS = 3
# Cloudflare (measured 2026-08-13 — NOT DataDome, the V2 notes were wrong)
# plus the old markers. Lowercase; matched against page content.lower().
BLOCK_MARKERS = ("datadome", "captcha", "just a moment", "cf-chl",
                 "challenge-platform", "turnstile", "verifying you are human")
CDP_URL = "http://localhost:9222"
HTML_DIR = CHECK_DIR / "pj_html"
# Markup that proves we received a real results page. If NEITHER a listing card
# NOR this markup is present, the page is a challenge/interstitial dressed as
# HTTP 200 — and marking it done would poison the resume file, burying pages
# 2-8 of that commune forever.
RESULTS_MARKUP = ("bi-list", "SearchResults", "bi-generic", "denomination",
                  "annuaire", "resultats")

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


def reveal_phones(page) -> int:
    """PJ hides every number behind an "Afficher le N°" button (learned from
    Sam's working scraper 2026-08-13); the number only exists in the DOM after
    a click, landing in `#bi-fantomas-<id>`. Click them all, tolerate misses.
    """
    clicked = 0
    # Primary selector = Sam's; fallbacks by visible text.
    for sel in ("button:has([aria-label='Afficher le numéro'])",
                "button:has-text('Afficher le N')",
                "a:has-text('Afficher le N')"):
        try:
            buttons = page.query_selector_all(sel)
        except Exception:
            continue
        if not buttons:
            continue
        for b in buttons:
            try:
                b.click(timeout=2000)
                clicked += 1
                time.sleep(0.2)
            except Exception:
                continue
        break
    if clicked:
        time.sleep(2.0)          # let the revealed numbers render
    return clicked


def extract_listings(page, search_url: str) -> list[dict]:
    """Read the result cards. Selectors follow Sam's working scraper
    (li.bi.bi-generic / .bi-denomination h3 / .bi-address / #bi-fantomas-<id>),
    each with a fallback. A card that yields neither a phone nor a site is
    dropped rather than written as an empty row. Call reveal_phones() FIRST.
    """
    out = []
    cards = page.query_selector_all(
        "li.bi.bi-generic, li.bi, article.bi, div.bi-generic, li[id^='bi-']")
    seen_ids = set()
    for c in cards:
        try:
            txt = c.inner_text()
        except Exception:
            continue
        # The card's own id attribute (Sam: card.getAttribute('id')). The old
        # regex searched inner_html, which never contains the card's own tag.
        card_id = (c.get_attribute("id") or "").strip()
        lid = card_id[3:] if card_id.startswith("bi-") else card_id
        if lid and lid in seen_ids:          # overlapping selectors above
            continue
        if lid:
            seen_ids.add(lid)

        name = ""
        for sel in (".bi-denomination h3", "h3", "a.bi-denomination", ".denom", "h2"):
            el = c.query_selector(sel)
            if el:
                name = (el.inner_text() or "").strip()
                if name:
                    break

        # Sam's extra columns: activity category + detail-page URL. The detail
        # page often carries the shop's own website — a future email lever.
        category = ""
        for sel in (".bi-activity-unit", "[class*='activity-unit']"):
            el = c.query_selector(sel)
            if el:
                category = " ".join((el.inner_text() or "").split())
                if category:
                    break
        detail_url = ""
        for sel in ("a.bi-denomination", "a[href*='/pros/']"):
            el = c.query_selector(sel)
            if el:
                href = el.get_attribute("href") or ""
                if href:
                    detail_url = href if href.startswith("http") else BASE + href
                    break

        # Phones: the revealed fantomas div first, raw card text as the
        # fallback. Sam's split: landline/09 -> phone, 06/07 -> mobile.
        found = []
        for sel in ((f"#bi-fantomas-{lid}",) if lid else ()) + (
                ".bi-fantomas .number-contact", ".number-contact"):
            el = c.query_selector(sel)
            if el:
                for m in PHONE_RE.finditer((el.inner_text() or "").replace(NBSP, " ")):
                    p = re.sub(r"[\s.\-]", " ", m.group(0)).strip()
                    if p not in found:
                        found.append(p)
                if found:
                    break
        if not found:
            for m in PHONE_RE.finditer(txt.replace(NBSP, " ")):
                p = re.sub(r"[\s.\-]", " ", m.group(0)).strip()
                if p not in found:
                    found.append(p)
        landlines = [p for p in found if not p.startswith(("06", "07"))]
        mobiles = [p for p in found if p.startswith(("06", "07"))]
        phone = landlines[0] if landlines else (mobiles[0] if mobiles else "")
        mobile = ""
        if landlines and mobiles:
            mobile = mobiles[0]
        elif len(mobiles) > 1:
            mobile = mobiles[1]

        website = ""
        for a in c.query_selector_all("a[href^='http']"):
            href = a.get_attribute("href") or ""
            low = href.lower()
            if not any(x in low for x in AGGREGATORS):
                website = href.split("?")[0]
                break

        addr = ""
        for sel in (".bi-address a", ".bi-address", "a.adresse", ".adresse"):
            el = c.query_selector(sel)
            if el:
                addr = " ".join((el.inner_text() or "").split())
                addr = addr.replace("Voir le plan", "").strip()
                break
        postcode, city = "", ""
        mm = re.search(r"\b(13\d{3})\b\s*([A-ZÀ-ÿ][^\n,]*)", addr or txt)
        if mm:
            postcode, city = mm.group(1), mm.group(2).strip()

        if not (phone or website):
            continue
        out.append({
            "listing_id": lid, "name": name, "phone": phone, "mobile": mobile,
            "website": website, "address": addr, "postcode": postcode,
            "city": city, "category": category, "detail_url": detail_url,
            "search_url": search_url,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest Pages Jaunes for dept-13 bakeries")
    ap.add_argument("--pilot", type=int, default=0, help="only the N busiest communes")
    ap.add_argument("--headful", action="store_true", help="visible browser (clear a challenge)")
    ap.add_argument("--attach", action="store_true",
                    help=f"attach to YOUR already-running Chrome over CDP ({CDP_URL}) "
                         "instead of launching one — see the docstring for setup")
    ap.add_argument("--dump-html", action="store_true",
                    help="save every fetched page's HTML under checkpoints/pj_html/ "
                         "(use on the pilot to verify selectors against reality)")
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
    consecutive_blocks = 0
    if args.dump_html:
        HTML_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = None
        if args.attach:
            # ATTACH: never launch. The human's own Chrome carries the trusted
            # fingerprint and the solved challenge; we borrow its session.
            # No init scripts, no spoofing — injecting anything into a genuine
            # session could itself be the tell.
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception as exc:
                sys.exit(f"could not attach to Chrome at {CDP_URL} ({exc}).\n"
                         "Start Chrome first (see the docstring):\n"
                         '  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                         '--remote-debugging-port=9222 '
                         '--user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile"')
            if not browser.contexts:
                sys.exit("attached, but Chrome has no browser context — open a window first")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            log.info(f"attached to running Chrome at {CDP_URL} "
                     f"({len(ctx.pages)} open tab(s))")
        else:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel="chrome",             # real Chrome, not bundled Chromium
                headless=not args.headful,
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1366, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            # Belt and braces: some challenge scripts read navigator.webdriver
            # directly before Chrome's own flag handling settles.
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        def save_state(tag: str = "") -> None:
            """The persistent profile keeps cookies on disk by itself; this is a
            readable backup. Saving only at the end would throw away the one
            thing a human had to do by hand if the run is interrupted."""
            try:
                ctx.storage_state(path=str(STATE_PATH))
                if tag:
                    log.info(f"session state saved ({tag}) -> {STATE_PATH.name}")
            except Exception as exc:
                log.warning(f"could not save session state: {type(exc).__name__}")

        for commune, cp in todo:
            if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                log.error(f"{consecutive_blocks} communes blocked in a row — stopping. "
                          "Run once with --headful and solve the challenge by hand; "
                          "the session is saved and the next run resumes here.")
                break
            where = f"{slug(commune)}-{cp}"
            commune_ok = False
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

                content = (page.content() or "")
                low = content.lower()
                if args.dump_html:
                    dump = HTML_DIR / f"{where}_p{pageno}.html"
                    dump.write_text(content, encoding="utf-8")
                    log.info(f"HTML saved -> {dump.relative_to(CHECK_DIR)}")
                if status in (403, 429) or any(k in low for k in BLOCK_MARKERS):
                    blocked += 1
                    log.warning(f"{where} p{pageno}: BLOCKED (status {status}).")
                    if args.attach:
                        # The trusted session got challenged on automated
                        # navigation — the exact failure mode we are testing
                        # for. Stop immediately; retrying burns the session.
                        log.error("attached session challenged/403'd on navigation. "
                                  "If this repeats on a fresh solve, PJ is closed "
                                  "to the CDP route too — record it in docs and stop.")
                        consecutive_blocks = MAX_CONSECUTIVE_BLOCKS
                        break
                    if args.headful:
                        # The whole point of --headful: a human is watching.
                        log.warning("─" * 62)
                        log.warning("SOLVE THE CHALLENGE IN THE BROWSER WINDOW NOW.")
                        log.warning("Waiting up to 5 minutes, then continuing automatically.")
                        log.warning("─" * 62)
                        for _ in range(150):        # 150 x 2s = 5 min
                            time.sleep(2)
                            try:
                                low2 = (page.content() or "").lower()
                            except Exception:
                                continue
                            if not any(k in low2 for k in BLOCK_MARKERS):
                                log.info("challenge cleared — saving session and retrying")
                                save_state("after challenge")
                                break
                        else:
                            log.warning("still challenged after 5 min — moving on")
                        # Retry this same URL once, now that cookies exist.
                        try:
                            resp = page.goto(url, wait_until="domcontentloaded")
                            status = resp.status if resp else 0
                            content = page.content() or ""
                            low = content.lower()
                        except Exception:
                            break
                        if status in (403, 429) or any(k in low for k in BLOCK_MARKERS):
                            break
                    else:
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

                clicked = reveal_phones(page)      # numbers exist only after this
                if clicked:
                    log.info(f"{where} p{pageno}: revealed {clicked} phone number(s)")
                    if args.dump_html:
                        # Second dump AFTER the reveal — this is the DOM the
                        # extractor actually reads.
                        dump = HTML_DIR / f"{where}_p{pageno}_revealed.html"
                        dump.write_text(page.content() or "", encoding="utf-8")
                rows = extract_listings(page, url)
                n = flush_rows(rows)          # <- on disk before anything else
                written_total += n

                # Only record a page as done when we are sure we SAW a results
                # page. A silent interstitial yields 0 cards too, and marking it
                # done buries pages 2-8 of this commune in pj_done.txt forever.
                real_page = bool(rows) or any(k.lower() in low for k in RESULTS_MARKUP)
                if real_page:
                    mark_done(url)
                    commune_ok = True
                    consecutive_blocks = 0
                else:
                    log.warning(f"{where} p{pageno}: 0 cards and no results markup — "
                                "NOT marking done (looks like an interstitial)")
                log.info(f"{where} p{pageno}: written={n} (run total {written_total})")
                if n == 0:
                    break                      # no more result pages for this commune
                time.sleep(random.uniform(*PAGE_DELAY))

            if commune_ok:
                save_state()                   # cheap, and never loses the challenge
            else:
                consecutive_blocks += 1

        save_state("end of run")
        if args.attach:
            # NEVER close the user's own context/tabs — only disconnect CDP.
            browser.close()
        else:
            ctx.close()

    log.info("─" * 62)
    log.info(f"written={written_total} rows this run -> {OUT_PATH}")
    if blocked:
        log.warning(f"{blocked} page(s) were blocked. Pages Jaunes is behind "
                    "Cloudflare; if even --attach was challenged on navigation, "
                    "PJ is closed to free automation — record it and stop.")
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            allrows = list(csv.DictReader(fh, delimiter=";"))
        log.info(f"file now holds {len(allrows)} rows: "
                 f"{sum(1 for r in allrows if r['phone'])} phones, "
                 f"{sum(1 for r in allrows if r['website'])} websites")
    log.info("Next: python scripts/m2_s7_match.py")


if __name__ == "__main__":
    main()
