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

Resume: `pj_done.txt` records a `{commune}-{cp}|p{n}` key per harvested page,
independent of how that page was reached. Re-running skips them. Kill it any
time.

Politeness: PAGE_DELAY between pages, one browser, one context. This is a slow
crawl over 162 commune/postcode pairs, not a hammering.

Output: exports/boulangerie/checkpoints/pj_listings.csv
        (listing_id, name, phone, mobile, website, address, postcode, city,
         category, detail_url, search_url)
Matching to SIRET rows is m2_s7's job, and requires location agreement —
`postcode` and `city` are load-bearing there, because PJ gives no lat/lon.

ATTACH + IN-PAGE MODE (V5, 2026-08-13) — the route that has never been tried.
Every LAUNCHED browser (bundled Chromium and real Chrome alike) was detected
by Cloudflare, and a human-solved `cf_clearance` did not survive
`page.goto()`. Two things follow, and this script now does both:

  1. **Never launch.** The human runs her OWN Chrome with remote debugging and
     solves the challenge as a normal visitor; we ATTACH over CDP.
  2. **Never navigate.** `--attach` defaults to IN-PAGE navigation: we type
     into PJ's own search field, click its search button, and advance by
     clicking its "page suivante" link. The measured failure was *automated
     navigation*, so the fix is to stop navigating — every fetch becomes an
     in-page click/XHR inside the session Cloudflare already trusts.
     `--goto` forces the old constructed-URL route for comparison.

    1. Close all Chrome windows, then start:
       "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
           --remote-debugging-port=9222 ^
           --user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile"
    2. Browse to pagesjaunes.fr YOURSELF, pass the challenge, and leave a
       bakery results page open in that tab.
    3. python scripts/m2_s6_pagesjaunes.py --attach --pilot 1 --dump-html
       -> JUDGE IT BY THE DUMPED HTML, never by an HTTP 200. Count
       `li.bi.bi-generic` cards in the dump and compare with the rows written;
       20 cards -> 3 rows means the selectors are wrong, not that PJ is empty.
       Every selector logs which fallback matched, so the pilot reports the
       real DOM instead of failing silently.
    4. Scale up. On a challenge mid-crawl the run PAUSES for up to 5 minutes
       so you can solve it in the visible window, then resumes by itself.

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
WHAT = "boulangerie-patisserie"      # URL slug, used by the goto/launched modes
WHAT_HUMAN = "boulangerie patisserie"  # what a human types into the form
PAGE_DELAY = (3.0, 6.0)      # seconds between pages, randomised
MAX_PAGES_PER_COMMUNE = 20   # PJ paginates 20/page. Raised from 8 on 2026-08-14:
                             # 34 communes HIT the old cap and Marseille 1er alone
                             # advertises 370 results (~19 pages), so 8 was truncating
                             # exactly the densest areas — where 453 of the 905 still
                             # phoneless businesses live.
NAV_TIMEOUT = 30_000
HUMAN_WAIT_MIN = 5.0         # minutes to wait for a hand-solved challenge

# Selectors for the IN-PAGE route. Every one has fallbacks and the matching
# one is LOGGED, because we have never seen a real PJ page: the pilot must
# report what the DOM actually uses instead of failing silently.
CARD_SEL = "li.bi.bi-generic, li[id^='bi-']"
SEARCH_WHAT_SEL = ("input#quoiqui", "input[name='quoiqui']", "input#quoi",
                   "input[placeholder*='Que']", "input[placeholder*='quoi']",
                   "input[aria-label*='Que']")
SEARCH_WHERE_SEL = ("input#ou", "input[name='ou']", "input[placeholder*='Où']",
                    "input[placeholder*='ou']", "input[aria-label*='Où']")
SUBMIT_SEL = ("button#findId", "button[type='submit']", "input[type='submit']",
              "button:has-text('Trouver')", "button:has-text('Rechercher')")
NEXT_SEL = ("a#pagination-next", "a[rel='next']", "a.link_pagination.next",
            "a[aria-label*='suivante']", "a:has-text('Suivant')",
            ".pagination a.next", "a.next")

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
# Block markers, NARROWED 2026-08-14 after a false positive cost a pilot run.
# The old list contained "captcha" and "challenge-platform", and a perfectly
# good 502 KB results page (20 cards) was thrown away because PJ's ordinary
# stylesheet contains `.g-recaptcha iframe{height:7.8rem}` and Cloudflare
# ships /cdn-cgi/challenge-platform/ on pages it is NOT challenging. Only
# phrases that appear on an actual interstitial survive here — and they are
# consulted only when the page has no result cards (see is_blocked).
BLOCK_MARKERS = ("just a moment...", "verifying you are human",
                 "attention required! | cloudflare", "/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page",
                 "datadome", "geo.captcha-delivery.com")
# <script>/<style> bodies are stripped before matching: a marker inside code
# is a false positive by construction.
CODE_BLOCK_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
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


def pj_location_slug(commune: str, cp: str) -> str:
    """PJ's own location slug, read off a live results URL 2026-08-14:
        13001 -> marseille-1er-arrondissement-13
        13100 -> aix-en-provence-13
    Marseille is split by arrondissement, which is exactly the granularity
    targets() uses, so each CP maps to one PJ page instead of one 700-listing
    city page that will not paginate far enough.
    """
    c = slug(commune)
    # Our own registry data spells Marseille several ways — "MARSEILLE",
    # "MARSEILLE 11", "MARSEILLE 11EME" — so match on the prefix and let the
    # POSTCODE decide the arrondissement. Before this, "MARSEILLE 11EME" built
    # `marseille-11eme-13`, a slug PJ does not use.
    if c.startswith("marseille") and cp.startswith("130") and cp[3:].isdigit():
        n = int(cp[3:])
        if 1 <= n <= 16:
            return f"marseille-{'1er' if n == 1 else str(n) + 'e'}-arrondissement-13"
    return f"{c}-13"


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


SEEN_IDS: set = set()


def load_seen_ids() -> None:
    """PJ's commune pages overlap — a Marseille 1er search already returns
    13002-13006 listings — so the same listing_id arrives from several
    crawls. Dedupe on write instead of shipping the same shop twice."""
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                if r.get("listing_id"):
                    SEEN_IDS.add(r["listing_id"])


def flush_rows(rows: list[dict]) -> int:
    """Append + flush. A fresh handle per write, closed immediately — the file
    analogue of m1_s9j's fresh-connection-per-write fix."""
    rows = [r for r in rows
            if not r.get("listing_id") or r["listing_id"] not in SEEN_IDS]
    for r in rows:
        if r.get("listing_id"):
            SEEN_IDS.add(r["listing_id"])
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


def first_selector(page, selectors: tuple):
    """First selector that actually matches, plus which one — the pilot must
    tell us what the real DOM uses, not what we hoped it used."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            continue
        if el:
            return el, sel
    return None, ""


def is_blocked(content: str, status: int = 0, n_cards: int = 0) -> bool:
    """EVIDENCE OF SUCCESS OUTRANKS EVIDENCE OF FAILURE.

    Learned the hard way 2026-08-14: a naive substring scan called a real
    results page a challenge because PJ's stylesheet mentions `.g-recaptcha`.
    Result cards on the page are proof we are through, whatever the markup
    also happens to contain — so they are checked first and end the question.
    """
    if n_cards > 0:
        return False
    if status in (403, 429):
        return True
    visible = CODE_BLOCK_RE.sub(" ", content).lower()
    return any(k in visible for k in BLOCK_MARKERS)


def count_cards(page) -> int:
    try:
        return len(page.query_selector_all(CARD_SEL))
    except Exception:
        return 0


def wait_for_human(page, minutes: float = HUMAN_WAIT_MIN) -> bool:
    """A human is at the keyboard — let them clear the challenge by hand.

    Sam's scraper waits 30 s for a manual solve; this is the same idea made
    interactive and patient. Aborting the whole run on the first challenge
    (the old --attach behaviour) throws away a session that a human could
    rescue in ten seconds.
    """
    log.warning("=" * 62)
    log.warning("CHALLENGE DETECTED — SOLVE IT IN THE CHROME WINDOW NOW.")
    log.warning(f"Waiting up to {minutes:.0f} min; the run resumes by itself.")
    log.warning("=" * 62)
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        time.sleep(3)
        try:
            content = page.content() or ""
        except Exception:
            continue
        if not is_blocked(content, n_cards=count_cards(page)):
            log.info("challenge cleared — resuming")
            return True
    log.warning("still challenged after the wait")
    return False


def settle(page) -> None:
    """Wait for the result cards, tolerating PJ's slow third-party widgets."""
    for waiter in (lambda: page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT),
                   lambda: page.wait_for_selector(CARD_SEL, timeout=15_000)):
        try:
            waiter()
        except Exception:
            pass


def dismiss_consent(page) -> None:
    for sel in ("#didomi-notice-agree-button",
                "button#onetrust-accept-btn-handler",
                "button:has-text('Tout accepter')"):
        try:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=2000)
                time.sleep(0.5)
                return
        except Exception:
            pass


def open_commune(page, commune: str, cp: str, what: str = "boulangerie") -> bool:
    """Open a commune's first results page by URL, inside the ATTACHED session.

    MEASURED 2026-08-14, and it overturns the V4 note: `page.goto()` returns
    HTTP 200 with 20 cards when we are attached to the human's own Chrome.
    The V4 failure ("clearance does not survive automated navigation") was a
    property of a LAUNCHED browser, not of navigation itself.

    Driving PJ's search FORM was tried first and abandoned: typing into
    `input#ou` does not update PJ's resolved location (it keeps an internal
    slug + idOu), so every "search" silently re-ran Marseille. The URL is the
    honest way to say which commune we want.
    """
    url = f"{BASE}/annuaire/{pj_location_slug(commune, cp)}/{what}"
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        status = resp.status if resp else 0
    except Exception as exc:
        log.warning(f"{commune} {cp}: navigation failed {type(exc).__name__}")
        return False
    settle(page)
    n = count_cards(page)
    log.info(f"{commune} {cp}: HTTP {status}, cards={n}, url={page.url[:78]}")
    if n == 0 and status == 404:
        log.warning(f"{commune} {cp}: 404 — PJ slug is probably wrong "
                    f"({pj_location_slug(commune, cp)})")
    return True


def search_in_page(page, what: str, where_text: str) -> bool:
    """Run the search through PJ's OWN form — no top-level navigation.

    THE WHOLE POINT (measured 2026-08-13): a human-solved `cf_clearance` does
    NOT survive `page.goto()`, headless or headful — Cloudflare binds it to a
    live fingerprint. But typing in a field and clicking a button is what the
    human who earned that clearance was already doing. Every fetch this
    function causes is an in-page navigation or XHR from a session Cloudflare
    already trusts, which is the one route nobody has tested.
    """
    dismiss_consent(page)
    el_what, s_what = first_selector(page, SEARCH_WHAT_SEL)
    el_where, s_where = first_selector(page, SEARCH_WHERE_SEL)
    if not (el_what and el_where):
        log.warning(f"search form NOT found (quoi={s_what or 'none'}, "
                    f"ou={s_where or 'none'}) — re-run with --dump-html and fix "
                    "SEARCH_WHAT_SEL / SEARCH_WHERE_SEL against the real DOM")
        return False
    url_before = page.url
    log.info(f"search form: quoi={s_what} ou={s_where} -> "
             f"{what!r} / {where_text!r}")
    try:
        for el, text in ((el_what, what), (el_where, where_text)):
            el.click(timeout=5000)
            el.fill("")
            el.type(text, delay=60)      # typed, not pasted — humans type
            time.sleep(0.8)              # let the autocomplete settle
        # PJ resolves the location through its own autocomplete
        # ("13001" -> "Marseille 1er arrondissement"). Taking the first
        # suggestion is what a human does and what makes the location a real
        # PJ place rather than a free-text string it may silently ignore.
        page.keyboard.press("ArrowDown")
        time.sleep(0.3)
        page.keyboard.press("Enter")
        settle(page)
        if page.url == url_before:
            # Enter only accepted the suggestion; submit explicitly.
            el_sub, s_sub = first_selector(page, SUBMIT_SEL)
            if el_sub:
                log.info(f"submitting via {s_sub}")
                el_sub.click(timeout=5000)
                settle(page)
    except Exception as exc:
        log.warning(f"could not drive the search form: {type(exc).__name__}: {exc}")
        return False
    if page.url == url_before:
        log.warning("the URL did not change — the search may not have fired; "
                    "extracting whatever is on screen anyway")
    else:
        log.info(f"search landed on {page.url[:100]}")
    return True


def click_next(page) -> bool:
    """Advance by CLICKING PJ's own pagination link. Returns False at the end
    of the result set — which is a normal stop, not a failure."""
    el, sel = first_selector(page, NEXT_SEL)
    if not el:
        return False
    try:
        el.scroll_into_view_if_needed(timeout=3000)
        el.click(timeout=5000)
    except Exception as exc:
        log.info(f"pagination click failed ({sel}): {type(exc).__name__}")
        return False
    settle(page)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest Pages Jaunes for dept-13 bakeries")
    ap.add_argument("--pilot", type=int, default=0, help="only the N busiest communes")
    ap.add_argument("--what", default="boulangerie",
                    help="PJ category slug. The 816-listing 'ceiling' was "
                         "measured on `boulangerie` alone; PJ also serves "
                         "boulangerie-patisserie, boulangeries-patisseries, "
                         "boulanger-patissier… (2026-08-20). A non-default "
                         "slug prefixes its own done-keys, so the original "
                         "harvest is never re-run.")
    ap.add_argument("--headful", action="store_true", help="visible browser (clear a challenge)")
    ap.add_argument("--attach", action="store_true",
                    help=f"attach to YOUR already-running Chrome over CDP ({CDP_URL}) "
                         "instead of launching one — see the docstring for setup")
    ap.add_argument("--dump-html", action="store_true",
                    help="save every fetched page's HTML under checkpoints/pj_html/ "
                         "(use on the pilot to verify selectors against reality)")
    ap.add_argument("--goto", action="store_true",
                    help="force the legacy constructed-URL route even under "
                         "--attach (in-page navigation is the attach default)")
    ap.add_argument("--start-url", default="",
                    help="navigate ONCE to this URL before starting — paste the "
                         "URL of a results page you reached by hand")
    args = ap.parse_args()

    # In-page navigation is the DEFAULT for --attach, and it is the whole
    # point of attaching: our own measurement says a human-solved clearance
    # dies on page.goto(). Driving the site's own form and pagination links
    # keeps every fetch inside the session Cloudflare already trusts.
    in_page = args.attach and not args.goto

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright required:\n  pip install playwright\n  playwright install chromium")

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    load_seen_ids()
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
                     f"({len(ctx.pages)} open tab(s)); current page: {page.url[:80]}")
            log.info(f"navigation mode: {'IN-PAGE (form + pagination clicks)' if in_page else 'goto (constructed URLs)'}")
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

        if args.start_url:
            # A single deliberate navigation from the trusted session. Whether
            # THIS survives is itself the measurement the V4 note is about.
            log.info(f"start-url: navigating once to {args.start_url[:90]}")
            try:
                resp = page.goto(args.start_url, wait_until="domcontentloaded")
                st = resp.status if resp else 0
                nc = count_cards(page)
                log.info(f"start-url: HTTP {st}, cards={nc}, "
                         f"blocked={is_blocked(page.content() or '', st, nc)}")
            except Exception as exc:
                log.warning(f"start-url failed: {type(exc).__name__}: {exc}")

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

        def handle_page(where: str, pageno: int, search_url: str) -> tuple:
            """Read whatever is on screen NOW. Returns (rows_written, ok, stop).

            `ok` = we saw a genuine results page. `stop` = abandon this commune.
            """
            content = (page.content() or "")
            low = content.lower()
            if args.dump_html:
                dump = HTML_DIR / f"{where}_p{pageno}.html"
                dump.write_text(content, encoding="utf-8")
                log.info(f"HTML saved -> {dump.relative_to(CHECK_DIR)}")

            n_cards = count_cards(page)
            if is_blocked(content, n_cards=n_cards):
                if not wait_for_human(page):
                    return 0, False, True
                content = (page.content() or "")
                low = content.lower()
                n_cards = count_cards(page)
                if is_blocked(content, n_cards=n_cards):
                    return 0, False, True
            clicked = reveal_phones(page)           # numbers exist only after this
            if clicked:
                log.info(f"{where} p{pageno}: revealed {clicked} phone number(s)")
                if args.dump_html:
                    # The DOM the extractor actually reads.
                    (HTML_DIR / f"{where}_p{pageno}_revealed.html").write_text(
                        page.content() or "", encoding="utf-8")
            rows = extract_listings(page, search_url)
            n = flush_rows(rows)                    # on disk before anything else

            # A results page proves itself by cards or by PJ's own markup. An
            # interstitial dressed as HTTP 200 yields 0 cards too, and marking
            # it done would bury the rest of this commune forever.
            real = bool(rows) or n_cards > 0 or any(k.lower() in low for k in RESULTS_MARKUP)
            log.info(f"{where} p{pageno}: cards={n_cards} written={n}"
                     f"{'' if real else '  (NO results markup — not marking done)'}")
            return n, real, False

        for commune, cp in todo:
            if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                log.error(f"{consecutive_blocks} communes blocked in a row — stopping. "
                          "Re-solve the challenge in Chrome and re-run; "
                          "pj_done.txt resumes where this left off.")
                break
            # Legacy keys (no prefix) belong to the original `boulangerie`
            # harvest; a new slug writes its own keyspace so neither run can
            # bury the other.
            where = f"{slug(commune)}-{cp}"
            if args.what != "boulangerie":
                where = f"{args.what}:{where}"
            keys = [f"{where}|p{n}" for n in range(1, MAX_PAGES_PER_COMMUNE + 1)]
            if all(k in done for k in keys):
                continue
            commune_ok = False

            if in_page:
                # ---- IN-PAGE ROUTE: drive PJ's own form, never navigate ----
                if not open_commune(page, commune, cp, args.what):
                    if is_blocked(page.content() or "", n_cards=count_cards(page))                             and wait_for_human(page):
                        if not open_commune(page, commune, cp, args.what):
                            consecutive_blocks += 1
                            continue
                    else:
                        consecutive_blocks += 1
                        continue
                for pageno in range(1, MAX_PAGES_PER_COMMUNE + 1):
                    key = f"{where}|p{pageno}"
                    if key in done:
                        log.info(f"{where} p{pageno}: already done — paginating past it")
                    else:
                        n, real, stop = handle_page(where, pageno, page.url)
                        written_total += n
                        if stop:
                            blocked += 1
                            break
                        if real:
                            mark_done(key)
                            commune_ok = True
                            consecutive_blocks = 0
                    time.sleep(random.uniform(*PAGE_DELAY))
                    if not click_next(page):
                        break                       # end of this commune's results
            else:
                # ---- LEGACY ROUTE: constructed URLs (launched/headful modes) ----
                for pageno in range(1, MAX_PAGES_PER_COMMUNE + 1):
                    key = f"{where}|p{pageno}"
                    if key in done:
                        continue
                    url = f"{BASE}/annuaire/chercherlespros?quoiqui={WHAT}&ou={where}&page={pageno}"
                    try:
                        # domcontentloaded, not load: 'load' hangs on stuck
                        # third-party widgets, which stalled the agri scrape.
                        resp = page.goto(url, wait_until="domcontentloaded")
                        status = resp.status if resp else 0
                    except Exception as exc:
                        log.warning(f"{where} p{pageno}: {type(exc).__name__} — skipping")
                        break
                    if status in (403, 429):
                        blocked += 1
                        log.warning(f"{where} p{pageno}: HTTP {status}")
                        if not wait_for_human(page):
                            break
                    dismiss_consent(page)
                    n, real, stop = handle_page(where, pageno, url)
                    written_total += n
                    if stop:
                        blocked += 1
                        break
                    if real:
                        mark_done(key)
                        commune_ok = True
                        consecutive_blocks = 0
                    if n == 0 and not real:
                        break
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
