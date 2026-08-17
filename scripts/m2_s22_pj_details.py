"""
M2-S22 — Read the website off each Pages Jaunes detail page
=============================================================
The V6 harvest captured `detail_url` for all 816 PJ listings but never opened
one, and `website` is empty on every row. A PJ *результats* card shows the phone
and the address; the shop's own site is on the DETAIL page. That is the last
untapped free source of new domains — and new domains, not re-crawling, is
where the remaining e-mail lives (measured: 474 domains already crawled, only
7 left for --deep).

Output goes straight into `discovered_sites.csv` with `backend="pj_detail"`,
so every downstream step picks it up with zero changes: m2_s21 validates it,
m2_s9 crawls it for e-mails/phones/contact pages, m2_s14 ships it.

Requires the SAME attached-Chrome trick as m2_s6 — PJ is Cloudflare-protected
and a plain request gets nothing, but a browser YOU are already using is not
suspicious. Close Chrome, then:

    & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
      --remote-debugging-port=9222 `
      --user-data-dir="$env:LOCALAPPDATA\\pj_cdp_profile"

browse to pagesjaunes.fr once yourself, leave it open, then run this.

Usage:
    python scripts/m2_s22_pj_details.py --limit 20     # pilot first, always
    python scripts/m2_s22_pj_details.py                # all 816
"""

import argparse
import csv
import logging
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m2_s6_pagesjaunes import CDP_URL, is_blocked  # noqa: E402
from m2_s8_websites import AGGREGATORS, BAD_TLD    # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
PJ_PATH = CHECK_DIR / "pj_listings.csv"
MATCHED_PATH = CHECK_DIR / "matched.csv"
OUT_PATH = CHECK_DIR / "discovered_sites.csv"
DONE_PATH = CHECK_DIR / "pj_details_done.txt"

FIELDNAMES = ["siret", "siren", "raison_sociale", "enseigne", "commune",
              "code_postal", "website", "rank", "query", "backend",
              "phone", "phone_domain", "facebook", "instagram",
              "snippet_geo_ok"]
DELAY = 2.5          # PJ is a guest we are tolerated on; do not hammer it
TIMEOUT = 25_000

# PJ renders the shop's site as an outbound link. Its own domains and the
# usual social/aggregator noise are not "the shop's website".
PJ_OWN = ("pagesjaunes", "solocal", "118712", "pjms", "annuaire")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s22")


def read_rows(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def usable(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith("http"):
        return False
    host = urllib.parse.urlparse(low).netloc
    if any(p in host for p in PJ_OWN):
        return False
    if any(a in low for a in AGGREGATORS):
        return False
    return not any(low.endswith(t) or t + "/" in low for t in BAD_TLD)


def extract_site(page) -> str:
    """The outbound link PJ shows as the merchant's own site.

    PJ wraps it in a redirect (`/pros/voir-le-site?...&url=<encoded>`), so the
    real target is sometimes only in the query string.
    """
    for sel in ('a[title*="ite internet" i]', 'a[href*="voir-le-site"]',
                'a.teaser-item-website', 'a[data-pjstats*="SITE"]',
                'a[href^="http"][target="_blank"][rel*="nofollow"]'):
        for el in page.query_selector_all(sel):
            href = el.get_attribute("href") or ""
            if "url=" in href:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                for key in ("url", "u", "redirect"):
                    if q.get(key):
                        cand = urllib.parse.unquote(q[key][0])
                        if usable(cand):
                            return cand
            if usable(href):
                return href
    # NO text fallback. A first version scraped any domain-shaped string out of
    # the page body and immediately handed `biscuiteriemarseillaise.fr` to
    # "Boulangerie Aixoise" — a different company, picked up from surrounding
    # page furniture. A domain printed somewhere on a page is not a link the
    # merchant declared, and this is the same class of error as V3's naked
    # phone-digit extraction. Only a real outbound link counts.
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Read websites off PJ detail pages")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright required:\n  pip install playwright")

    listings = [r for r in read_rows(PJ_PATH) if r.get("detail_url")]
    if not listings:
        sys.exit(f"{PJ_PATH} has no detail_url — run m2_s6 --attach first.")

    # A PJ listing is only useful to us once m2_s7 has tied it to a SIRET;
    # an unmatched listing has nobody to attach the website to.
    matched = read_rows(MATCHED_PATH)
    by_listing = {m["listing_id"]: m for m in matched
                  if m.get("source") == "pagesjaunes" and m.get("listing_id")}
    done = load_done()
    todo = [r for r in listings
            if r["listing_id"] in by_listing and r["listing_id"] not in done]
    log.info(f"{len(listings)} PJ listings | {len(by_listing)} tied to a SIRET | "
             f"{len(done)} already read -> {len(todo)} to do")
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        sys.exit("nothing to do")

    found = blocked = 0
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:
            sys.exit(f"could not attach to Chrome at {CDP_URL} ({exc}).\n"
                     "Close Chrome, then start it with --remote-debugging-port=9222 "
                     "(see this file's docstring) and browse to pagesjaunes.fr once.")
        if not browser.contexts:
            sys.exit("attached, but Chrome has no window open")
        ctx = browser.contexts[0]
        page = ctx.new_page()
        log.info(f"attached to your Chrome at {CDP_URL}")

        for i, r in enumerate(todo, 1):
            m = by_listing[r["listing_id"]]
            site = ""
            try:
                resp = page.goto(r["detail_url"], timeout=TIMEOUT,
                                 wait_until="domcontentloaded")
                status = resp.status if resp else 0
                content = page.content() or ""
                # Result cards outrank block markers — the V6 lesson. A detail
                # page has no cards, so status + marker is all we have; a 200
                # carrying real contact markup is not a challenge page.
                if status != 200 or is_blocked(content, status):
                    blocked += 1
                    log.warning(f"[{i}/{len(todo)}] BLOCKED status={status} "
                                f"{r['detail_url']}")
                    time.sleep(DELAY * 3)
                    continue
                site = extract_site(page)
            except Exception as exc:
                log.warning(f"[{i}/{len(todo)}] error {type(exc).__name__} "
                            f"{r['detail_url']}")
            time.sleep(DELAY)

            if site:
                row = {k: "" for k in FIELDNAMES}
                row.update({
                    "siret": m["siret"], "siren": m["siren"],
                    "raison_sociale": m["raison_sociale"],
                    "enseigne": r.get("name", ""), "commune": m["commune"],
                    "code_postal": m["code_postal"], "website": site,
                    "rank": "1", "query": r["detail_url"], "backend": "pj_detail",
                    "snippet_geo_ok": "",
                })
                new = not OUT_PATH.exists()
                with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";",
                                       quoting=csv.QUOTE_MINIMAL)
                    if new:
                        w.writeheader()
                    w.writerow(row)
                    fh.flush()
                found += 1
            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(r["listing_id"] + "\n")
            log.info(f"[{i}/{len(todo)}] {r.get('name','')[:26]:26s} -> "
                     f"{site[:46] or '(no site)'}  found={found}")

        page.close()

    log.info("─" * 62)
    log.info(f"websites found: {found}/{len(todo)} | blocked: {blocked}")
    log.info(f"appended to {OUT_PATH} as backend=pj_detail")
    log.info("Next: m2_s21_validate_sites.py (judges the new domains), "
             "then m2_s9_emails.py --only-valid, then m2_s14 + m2_s19.")


if __name__ == "__main__":
    main()
