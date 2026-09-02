"""
M3AG-S7 — Pages Jaunes harvest for agriculteurs (dept 63, then 03)
==================================================================
Adapted 2026-09-01 from the proven `m2_s6_pagesjaunes.py` (boulangeries 13).
Sam explicitly asked to measure how much PJ yields for farmers, so this runs
FIRST in the sector. Read m2_s6's docstring for the full war story; the rules
it earned are kept verbatim here:

  * ATTACH ONLY. Cloudflare defeated every launched browser (bundled Chromium
    AND real Chrome, headless AND headful); attaching over CDP to the human's
    own Chrome is the one measured-working route. Start Chrome first:
        chrome --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile"
    browse to pagesjaunes.fr once, leave it open.
  * Navigate commune pages BY URL (goto works when attached), paginate by
    CLICKING PJ's own "next" link. Never drive the search form (input#ou
    keeps an internal slug and silently re-runs the old location).
  * Result cards outrank any block marker (`is_blocked`); phones exist in the
    DOM only after clicking "Afficher le N°" (`reveal_phones`).
  * Flush after every page; `written=` means rows on disk. A crash costs one
    page. `pj_done.txt` keys resume the run.

What is NEW here vs m2_s6:
  * `--departement 63` — location slugs become `{commune}-63`, the postcode
    sniffer matches `63xxx`. No Marseille arrondissement special-case.
  * `--what` accepts a COMMA-SEPARATED slug list: farmers are spread over
    several PJ categories (agriculteurs, producteurs..., apiculteurs, ...)
    where bakeries had essentially one. Each slug gets its own done-keyspace
    (the m2-s26 lesson: backend-scoped done keys).
  * Targets come from `operateurs_{dept}.csv` (m3ag_s2) — OUR population,
    busiest commune first.

Usage:
    python scripts/m3ag_s7_pagesjaunes.py --attach --pilot 2 --dump-html  # slug pilot
    python scripts/m3ag_s7_pagesjaunes.py --attach                        # full crawl
    python scripts/m3ag_s7_pagesjaunes.py --attach --departement 03       # next dept
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
CHECK_DIR = PROJECT_ROOT / "exports" / "agriculteurs" / "checkpoints"
OUT_PATH  = CHECK_DIR / "pj_listings.csv"
DONE_PATH = CHECK_DIR / "pj_done.txt"
HTML_DIR  = CHECK_DIR / "pj_html"

BASE = "https://www.pagesjaunes.fr"
# Candidate category slugs for farmers, to be MEASURED by the pilot (cards vs
# 404 is the verdict, not our intuition — the m2-s25 `--what` lesson).
DEFAULT_WHATS = ("agriculteurs,producteurs-de-fruits-et-legumes,apiculteurs,"
                 "maraichers,fromagers,elevages")
PAGE_DELAY = (3.0, 6.0)
MAX_PAGES_PER_COMMUNE = 20
NAV_TIMEOUT = 30_000
HUMAN_WAIT_MIN = 5.0
MAX_CONSECUTIVE_BLOCKS = 3

CARD_SEL = "li.bi.bi-generic, li[id^='bi-']"
NEXT_SEL = ("a#pagination-next", "a[rel='next']", "a.link_pagination.next",
            "a[aria-label*='suivante']", "a:has-text('Suivant')",
            ".pagination a.next", "a.next")

FIELDNAMES = ["listing_id", "name", "phone", "mobile", "website", "address",
              "postcode", "city", "category", "detail_url", "search_url"]

PHONE_RE = re.compile(r"0[1-9](?:[\s.\-]?\d{2}){4}")
NBSP = " "
AGGREGATORS = ("pagesjaunes", "pagespro", "google.", "facebook.", "instagram.",
               "tripadvisor", "ubereats", "deliveroo", "justeat", "petitfute",
               "yelp.", "mappy.", "linkedin.")

BLOCK_MARKERS = ("just a moment...", "verifying you are human",
                 "attention required! | cloudflare",
                 "/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page",
                 "datadome", "geo.captcha-delivery.com")
CODE_BLOCK_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
CDP_URL = "http://localhost:9222"
RESULTS_MARKUP = ("bi-list", "SearchResults", "bi-generic", "denomination",
                  "annuaire", "resultats")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s7")

DEPT = "63"                                  # set from --departement in main()
# ANY French CP, not just the target dept's: PJ pads commune pages with
# "à proximité" results across the border (measured: 202 of the first 580
# listings were dept 43/42/03/23). Those rows keep their real location and
# the MATCHER filters by dept — a dept-03 listing harvested during the 63
# run is free inventory for the 03 run.
CP_RE = re.compile(r"\b(\d{5})\b\s*([A-ZÀ-ÿ][^\n,]*)")


def set_dept(dept: str) -> None:
    global DEPT
    DEPT = dept


def slug(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s


def pj_location_slug(commune: str) -> str:
    """`clermont-ferrand-63` shape — PJ's generic commune slug. Dept 63/03
    have no arrondissement cities, so no special-casing (m2's Marseille rule
    stays in m2)."""
    return f"{slug(commune)}-{DEPT}"


def targets() -> list[tuple[str, str]]:
    """(ville, cp) pairs from OUR operator list, busiest first — the crawl
    covers exactly the population we must enrich."""
    ours = CHECK_DIR / f"operateurs_{DEPT}.csv"
    if not ours.exists():
        sys.exit(f"{ours} not found — run m3ag_s2_transform.py first.")
    with ours.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    pairs = Counter((r["ville"], r["codePostal"]) for r in rows
                    if r["ville"] and r["codePostal"].startswith(DEPT))
    # One PJ page per COMMUNE, not per postcode: the pilot showed 63000 and
    # 63100 both resolve to `clermont-ferrand-63`, so keeping both re-crawls
    # the same pages under different done-keys. First (busiest) wins.
    out, seen_slugs = [], set()
    for (ville, cp), _ in pairs.most_common():
        s = slug(ville)
        if s in seen_slugs:
            continue
        seen_slugs.add(s)
        out.append((ville, cp))
    return out


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def mark_done(key: str) -> None:
    with DONE_PATH.open("a", encoding="utf-8") as f:
        f.write(key + "\n")
        f.flush()


SEEN_IDS: set = set()


def load_seen_ids() -> None:
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                if r.get("listing_id"):
                    SEEN_IDS.add(r["listing_id"])


def flush_rows(rows: list[dict]) -> int:
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
    clicked = 0
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
        time.sleep(2.0)
    return clicked


def extract_listings(page, search_url: str) -> list[dict]:
    out = []
    cards = page.query_selector_all(
        "li.bi.bi-generic, li.bi, article.bi, div.bi-generic, li[id^='bi-']")
    seen_ids = set()
    for c in cards:
        try:
            txt = c.inner_text()
        except Exception:
            continue
        card_id = (c.get_attribute("id") or "").strip()
        lid = card_id[3:] if card_id.startswith("bi-") else card_id
        if lid and lid in seen_ids:
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
        mm = CP_RE.search(addr or txt)
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
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            continue
        if el:
            return el, sel
    return None, ""


def is_blocked(content: str, status: int = 0, n_cards: int = 0) -> bool:
    """Evidence of success outranks evidence of failure (the m2 rule)."""
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


def open_commune(page, commune: str, cp: str, what: str) -> tuple[bool, int]:
    """Open a commune's first results page by URL inside the attached session.
    Returns (navigated_ok, http_status)."""
    url = f"{BASE}/annuaire/{pj_location_slug(commune)}/{what}"
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        status = resp.status if resp else 0
    except Exception as exc:
        log.warning(f"{commune} {cp} [{what}]: navigation failed {type(exc).__name__}")
        return False, 0
    settle(page)
    n = count_cards(page)
    log.info(f"{commune} {cp} [{what}]: HTTP {status}, cards={n}, url={page.url[:78]}")
    return True, status


def click_next(page) -> bool:
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
    ap = argparse.ArgumentParser(description="Harvest Pages Jaunes for agriculteurs")
    ap.add_argument("--departement", default="63", help="dept code (63 or 03)")
    ap.add_argument("--pilot", type=int, default=0, help="only the N busiest communes")
    ap.add_argument("--what", default=DEFAULT_WHATS,
                    help="comma-separated PJ category slugs; each gets its own "
                         "done-keyspace so runs never bury each other")
    ap.add_argument("--attach", action="store_true",
                    help=f"attach to YOUR running Chrome over CDP ({CDP_URL}) — "
                         "the only route that works (see docstring). REQUIRED.")
    ap.add_argument("--dump-html", action="store_true",
                    help="save fetched pages under checkpoints/pj_html/")
    args = ap.parse_args()

    if not args.attach:
        sys.exit("--attach is required: launched browsers are Cloudflare-dead "
                 "(measured on sector 2). Start your Chrome with "
                 '--remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile", '
                 "open pagesjaunes.fr once, then rerun with --attach.")

    set_dept(args.departement)
    whats = [w.strip() for w in args.what.split(",") if w.strip()]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright required:\n  pip install playwright")

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    if args.dump_html:
        HTML_DIR.mkdir(parents=True, exist_ok=True)
    load_seen_ids()
    todo = targets()
    if args.pilot:
        todo = todo[:args.pilot]
    done = load_done()
    log.info(f"[{DEPT}] {len(todo)} commune(s) x {len(whats)} slug(s); "
             f"{len(done)} page-keys already done")

    written_total = 0
    blocked = 0
    slug_yield: Counter = Counter()
    slug_404: Counter = Counter()

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:
            sys.exit(f"could not attach to Chrome at {CDP_URL} ({exc}).\n"
                     "Start Chrome first:\n"
                     '  chrome --remote-debugging-port=9222 '
                     '--user-data-dir="%LOCALAPPDATA%\\pj_cdp_profile"')
        if not browser.contexts:
            sys.exit("attached, but Chrome has no browser context — open a window first")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        log.info(f"attached to Chrome ({len(ctx.pages)} tab(s)); current: {page.url[:80]}")
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        def handle_page(where: str, pageno: int, search_url: str) -> tuple:
            content = (page.content() or "")
            low = content.lower()
            if args.dump_html:
                dump = HTML_DIR / f"{where.replace(':', '_')}_p{pageno}.html"
                dump.write_text(content, encoding="utf-8")
            n_cards = count_cards(page)
            if is_blocked(content, n_cards=n_cards):
                if not wait_for_human(page):
                    return 0, False, True
                content = (page.content() or "")
                low = content.lower()
                n_cards = count_cards(page)
                if is_blocked(content, n_cards=n_cards):
                    return 0, False, True
            clicked = reveal_phones(page)
            if clicked:
                log.info(f"{where} p{pageno}: revealed {clicked} phone number(s)")
            rows = extract_listings(page, search_url)
            n = flush_rows(rows)
            real = bool(rows) or n_cards > 0 or any(k.lower() in low for k in RESULTS_MARKUP)
            log.info(f"{where} p{pageno}: cards={n_cards} written={n}"
                     f"{'' if real else '  (NO results markup — not marking done)'}")
            return n, real, False

        consecutive_blocks = 0
        for what in whats:
            for commune, cp in todo:
                if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                    log.error(f"{consecutive_blocks} pages blocked in a row — stopping. "
                              "Re-solve the challenge in Chrome and rerun to resume.")
                    break
                where = f"{DEPT}:{what}:{slug(commune)}-{cp}"
                keys = [f"{where}|p{n}" for n in range(1, MAX_PAGES_PER_COMMUNE + 1)]
                if all(k in done for k in keys):
                    continue
                nav_ok, status = open_commune(page, commune, cp, what)
                if not nav_ok:
                    consecutive_blocks += 1
                    continue
                # A 403/429 with no cards is Cloudflare, full stop. Without
                # this check a blocked session "paginates past" already-done
                # keys for hours (measured 2026-09-02 01:08): one human-wait,
                # then the block counter does its job.
                if status in (403, 429) and count_cards(page) == 0:
                    if not wait_for_human(page):
                        blocked += 1
                        consecutive_blocks += 1
                        continue
                if status == 404:
                    # The slug does not exist for this commune (or at all) —
                    # a normal verdict, recorded, never retried.
                    slug_404[what] += 1
                    for k in keys:
                        if k not in done:
                            mark_done(k)
                    consecutive_blocks = 0
                    time.sleep(random.uniform(*PAGE_DELAY))
                    continue
                dismiss_consent(page)
                for pageno in range(1, MAX_PAGES_PER_COMMUNE + 1):
                    key = f"{where}|p{pageno}"
                    if key in done:
                        log.info(f"{where} p{pageno}: already done — paginating past it")
                    else:
                        n, real, stop = handle_page(where, pageno, page.url)
                        written_total += n
                        slug_yield[what] += n
                        if stop:
                            blocked += 1
                            consecutive_blocks += 1
                            break
                        if real:
                            mark_done(key)
                            consecutive_blocks = 0
                    time.sleep(random.uniform(*PAGE_DELAY))
                    if not click_next(page):
                        break
            else:
                continue
            break                       # inner loop hit the block ceiling

        browser.close()                 # disconnect CDP only — never close her Chrome

    log.info("─" * 62)
    log.info(f"[{DEPT}] written={written_total} new rows this run -> {OUT_PATH}")
    for what in whats:
        log.info(f"[{DEPT}]   slug {what:<40} rows={slug_yield[what]:>4}  404s={slug_404[what]}")
    if blocked:
        log.warning(f"{blocked} page(s) blocked — rerun after re-solving; pj_done.txt resumes.")
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            allrows = list(csv.DictReader(fh, delimiter=";"))
        log.info(f"file now holds {len(allrows)} rows: "
                 f"{sum(1 for r in allrows if r['phone'])} phones, "
                 f"{sum(1 for r in allrows if r['website'])} websites")
    log.info("Next: the geo matcher (m3ag_s8), then the yield report for Sam.")


if __name__ == "__main__":
    main()
