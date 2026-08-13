"""
M2-S8 — Discover bakery websites (Brave Search)
================================================
The registry has no website field, and OSM only knew 109 of them. This finds
more, so that m2_s9 has domains to read e-mails off.

Engine choice was MEASURED, not assumed (2026-08-13, this machine):

    Pages Jaunes    403 + CAPTCHA on every URL shape        blocked
    DuckDuckGo lite 403                                     blocked
    Mojeek          403                                     blocked
    Bing            200 but served results for another
                    query entirely — the repo's old recipe
                    no longer returns real hits             useless
    Startpage       200, 1 link                             useless
    Brave Search    200, 57 links, correct hit              WORKS

Precision measured against 6 businesses whose real site we already knew from
OSM: searching the RAISON SOCIALE found the right domain only 2/6, because a
bakery's legal name is not its shop name (`LEPRADPCH` really trades as
sylvaindepuichaffray.fr). So this script searches the ENSEIGNE first when one
exists, and — crucially — **never trusts the result**. Confirmation is
m2_s9's job: it fetches the page and demands the SIRET, or the postal code
plus a name token, before the domain counts. An unconfirmed domain ships as
`faible` and is never pattern-expanded.

That division of labour is the whole design: this step may be sloppy, because
the next step is strict.

Crash-safety: one row appended and flushed per business, `sites_done.txt`
records finished SIRETs, so a re-run resumes and nothing is held in memory.

Usage:
    python scripts/m2_s8_websites.py --limit 50      # pilot
    python scripts/m2_s8_websites.py                 # all pending (slow)
"""

import argparse
import csv
import logging
import random
import re
import sys
import time
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
MATCHED_PATH = CHECK_DIR / "matched.csv"
OUT_PATH  = CHECK_DIR / "discovered_sites.csv"
DONE_PATH = CHECK_DIR / "sites_done.txt"

SEARCH = "https://search.brave.com/search?q="
DELAY = (2.5, 4.5)
NAV_TIMEOUT = 25_000

# Directories, registry mirrors, delivery apps and social networks. None of
# them is the bakery's own site, and every one of them would pass a naive
# "the page mentions the business" confirmation because that is their content.
AGGREGATORS = (
    "pagesjaunes", "pagespro", "118712", "118000", "justacote", "yelp.",
    "tripadvisor", "petitfute", "mappy.", "google.", "facebook.", "instagram.",
    "linkedin.", "twitter.", "x.com", "tiktok.", "youtube.", "pinterest.",
    "ubereats", "deliveroo", "just-eat", "justeat", "thefork", "lafourchette",
    "societe.com", "pappers", "infogreffe", "verif.com", "manageo", "kompass",
    "lefigaro.fr", "bilansgratuits", "annuaire-entreprises", "sirene",
    "entreprises.", "b-reputation", "dirigeants.", "score3", "corporama",
    "restaurantguru", "love-spots", "villepratique", "boulangeriespatisseries",
    "boulangeries-patisseries", "franceboulangerie", "boulangerieautourdemoi",
    "cylex", "wikipedia", "leboncoin", "indeed", "hellowork", "wanted.jobs",
    "openstreetmap", "foursquare", "misterbandb", "resto.fr", "lesannonces",
    "amazon.", "doctolib", "avis-", "trustpilot", "brave.com", "michelin",
    "lefooding", "gaultmillau", "marmiton", "journaldesfemmes", "mesinfos",
    "madeinmarseille", "sortiraparis", "actu.fr", "laprovence",
    # added after the first pilot: every "hit" it produced was one of these
    "edecideur", "marseille-tourisme", "mapstr", "petitesaffiches", "figaro",
    "tourisme", "office-tourisme", "yellowpages", "nomao", "citiwaki",
    "topboulangerie", "resto-", "lannuaire", "annuaire", "guide-", "avis.",
)
BAD_TLD = (".gouv.fr", ".gov", ".edu")

FIELDNAMES = ["siret", "siren", "raison_sociale", "enseigne", "commune",
              "code_postal", "website", "rank", "query"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s8")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 ]+", " ", s).upper()


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def flush(row: dict) -> None:
    new = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        if new:
            w.writeheader()
        w.writerow(row)
        fh.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover bakery websites via Brave Search")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright required:\n  pip install playwright\n  playwright install chromium")

    if not OURS_PATH.exists():
        sys.exit(f"{OURS_PATH} not found — run scripts/m2_s2_transform.py first.")
    with OURS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        ours = list(csv.DictReader(fh, delimiter=";"))

    # Skip businesses whose site we already know from a harvest.
    known = set()
    if MATCHED_PATH.exists():
        with MATCHED_PATH.open(encoding="utf-8-sig", newline="") as fh:
            known = {r["siret"] for r in csv.DictReader(fh, delimiter=";") if r["website"]}
    done = load_done()
    todo = [r for r in ours if r["siret"] not in known and r["siret"] not in done]

    # Biggest first. "Don't scrape the whole base" is a standing rule here, and
    # a sole trader with no employees almost never has a website — measured at
    # 0/40 on an unsorted pilot. Businesses with a declared workforce are a
    # different population, so whatever fraction of the run completes covers
    # the most promising targets first.
    EFF_ORDER = {"": 0, "0 salarie": 1, "1 a 2 salaries": 2, "3 a 5 salaries": 3,
                 "6 a 9 salaries": 4, "10 a 19 salaries": 5, "20 a 49 salaries": 6,
                 "50 a 99 salaries": 7, "100 a 199 salaries": 8}
    todo.sort(key=lambda r: (-EFF_ORDER.get(r["tranche_effectif"], 9),
                             0 if r["enseigne"] else 1))
    if args.limit:
        todo = todo[:args.limit]
    log.info(f"{len(ours)} businesses, {len(known)} already have a site, "
             f"{len(done)} already searched -> {len(todo)} to do")

    found = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="fr-FR", timezone_id="Europe/Paris",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
        page = ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        for i, r in enumerate(todo, 1):
            # Enseigne first: it is the name on the shopfront and therefore the
            # name on the website. Raison sociale is a legal string that often
            # appears nowhere on the internet.
            label = r["enseigne"] or r["raison_sociale"]
            q = f'{label} {r["commune"]} boulangerie patisserie'
            try:
                page.goto(SEARCH + urllib.parse.quote(q), wait_until="domcontentloaded")
                hrefs = [a.get_attribute("href") or ""
                         for a in page.query_selector_all('a[href^="http"]')]
            except Exception as exc:
                log.warning(f"[{i}/{len(todo)}] {label[:28]}: {type(exc).__name__}")
                with DONE_PATH.open("a", encoding="utf-8") as f:
                    f.write(r["siret"] + "\n")
                continue

            best, rank = "", 0
            seen = []
            for h in hrefs:
                d = urllib.parse.urlparse(h).netloc.lower().replace("www.", "")
                if not d or d in seen:
                    continue
                seen.append(d)
                if any(x in d for x in AGGREGATORS) or d.endswith(BAD_TLD):
                    continue
                rank = len(seen)
                best = f"https://{d}"
                break

            if best:
                flush({"siret": r["siret"], "siren": r["siren"],
                       "raison_sociale": r["raison_sociale"], "enseigne": r["enseigne"],
                       "commune": r["commune"], "code_postal": r["code_postal"],
                       "website": best, "rank": rank, "query": q})
                found += 1
            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(r["siret"] + "\n")
            if i % 25 == 0 or best:
                log.info(f"[{i}/{len(todo)}] {label[:26]:26.26} -> "
                         f"{best or '(rien)':42.42} found={found}")
            time.sleep(random.uniform(*DELAY))

        ctx.close()
        browser.close()

    log.info("─" * 62)
    log.info(f"candidate sites found: {found}/{len(todo)} ({found/max(1,len(todo)):.0%})")
    log.info("These are CANDIDATES. m2_s9_emails.py confirms each one against "
             "the SIRET or postcode+name on the page before it counts.")


if __name__ == "__main__":
    main()
