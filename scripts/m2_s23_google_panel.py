"""
M2-S23 — Read the phone (and site) off each Google Business panel
==================================================================
Sam's V8 review, point 2. He searched

    boulangerie 9 RUE DE LA REPUBLIQUE 13002 MARSEILLE 13002 MARSEILLE

and Google showed a business panel carrying `04 91 90 60 80` — a number we do
not ship. That row (SLYM, SIRET 88085324700026) is empty in V8, and it is one
of ~840 with no phone at all. Google's panel is the last free source that
covers them one by one instead of by area.

WHY A BROWSER AND NOT AN API. The search quota is spent: Serper has 197
one-time credits against ~840 targets, Tavily is exhausted until 1 September,
ddgs allows 300/day. Attaching to the Chrome Ines already uses costs nothing
and is not rate-limited — the same trick that reopened Pages Jaunes in V6
after every doc in this repo had declared it closed.

    & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
      --remote-debugging-port=9222 `
      --user-data-dir="$env:LOCALAPPDATA\\pj_cdp_profile"

Browse to google.com once yourself, leave it open, then run this.

WHAT THIS SCRIPT REFUSES TO DO, and why each refusal is a scar:

  * It does not trust the query. The search embeds OUR address, so it is
    tempting to attach whatever comes back to the row we searched for. V3 did
    that with a named Maps query and got "Boulanger Aubagne", an ELECTRONICS
    retailer, for `COMPAGNIE BOULANGERE`. Every row written here goes through
    m2_s7 like any other listing, and the panel's PRINTED address is what has
    to agree — the question the query asked is not evidence of the answer.
  * It does not read a phone-shaped digit run. Only a `tel:` link or a number
    announced after Google's own `Téléphone` label counts. Naked extraction
    is what produced `01 11 24 63 33` from minified JS in V3.
  * It does not scrape a domain-shaped string as the website. Only a real
    outbound link, the m2_s22 lesson (`biscuiteriemarseillaise.fr` handed to
    a different company).
  * It does not type into the search box. PJ taught that a form can keep an
    internal location that your typing never changes; navigate by URL.

Output: `google_panel_listings.csv`, consumed by m2_s7 as source
`google_panel`. That label is deliberately absent from m2_s14's PHONE_RANK,
so a panel phone lands in the corroboration pool, not the dialled column: it
ships as `Telephone` only when a second, independent source names the same
number. Promoting it outright needs an agreement measurement first, the way
pagesjaunes earned its rank at 83.4% against OSM/Maps.

Usage:
    python scripts/m2_s23_google_panel.py --pilot 20    # ALWAYS first
    python scripts/m2_s23_google_panel.py               # the rest, resumable
"""

import argparse
import csv
import logging
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m2_s6_pagesjaunes import CDP_URL                       # noqa: E402
from m2_s8_websites import AGGREGATORS, BAD_TLD             # noqa: E402
from m2lib_contact import normalize_fr_phone, plausible_fr_number  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
MATCHED_PATH = CHECK_DIR / "matched.csv"
CONTACTS_PATH = CHECK_DIR / "site_contacts.csv"
OUT_PATH = CHECK_DIR / "google_panel_listings.csv"
DONE_PATH = CHECK_DIR / "google_panel_done.txt"

FIELDNAMES = ["listing_id", "name", "phone", "website", "address",
              "postcode", "city", "query"]

DELAY = 2.5          # a human pace; we are a guest in Ines's own session
TIMEOUT = 25_000
PANEL_WAIT = 1_500   # the panel paints after domcontentloaded (measured)

# Google's panel labels the number. Accept a number only inside this window,
# the extract_phones_ctx contract applied to a page we cannot select by class.
PHONE_LABEL_RE = re.compile(r"(?:t[ée]l[ée]phone|phone)\s*:?\s*", re.I)
PHONE_NEAR_RE = re.compile(
    r"(?<![\d/])(?:\+33|0033|0)[\s.\-]?[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")
CTX_WINDOW = 60

# The panel is addressable by Google's own semantic keys (measured live,
# 2026-08-17): the phone sits under `kc:/local:alt phone`, the address under
# `kc:/location/location:address`, and both live inside `#rhs`.
PANEL_PROOF = ("[data-attrid^='kc:/local'], "
               "[data-attrid='kc:/location/location:address']")
PHONE_ATTRID = "[data-attrid='kc:/local:alt phone']"
ADDR_ATTRID = "[data-attrid='kc:/location/location:address']"
ADDR_LABEL_RE = re.compile(r"^\s*adresse\s*:?\s*", re.I)
CP_RE = re.compile(r"\b(\d{5})\b")

# Markers of a company-directory snippet. If these survive into what we think
# is the panel address, we are reading the wrong part of the page.
SNIPPET_JUNK_RE = re.compile(
    r"SIRET|Clef NIC|Dossier d'urbanisme|Forme juridique|adresse postale",
    re.I)

# Google's own hosts are never a merchant's website.
GOOGLE_OWN = ("google.", "gstatic.", "googleusercontent.", "youtube.",
              "schema.org", "maps.app.goo.gl", "goo.gl", "support.google")

# A challenge page. Success outranks these: a page that yielded a labelled
# phone is not a block, whatever else it contains — the V6 `is_blocked`
# correction, where the bare word `captcha` in a stylesheet hid 20 real cards.
BLOCK_MARKERS = ("/sorry/", "trafic inhabituel", "unusual traffic",
                 "recaptcha", "nos systèmes ont détecté")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s23")


def read_rows(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def targets(limit: int = 0) -> list:
    """Rows with no trusted phone yet, biggest first.

    Same selection as m2_s16: a business already carrying an OSM/Maps/PJ
    number gains nothing from a third witness, and an interrupted run should
    have spent itself on the rows that matter most.
    """
    ours = read_rows(OURS_PATH)
    have = {m["siret"] for m in read_rows(MATCHED_PATH) if (m.get("phone") or "").strip()}
    have |= {c["siret"] for c in read_rows(CONTACTS_PATH)
             if (c.get("phone") or "").strip() and c.get("confiance") == "confirme"}
    done = load_done()

    def size(r):
        m = re.match(r"(\d+)", r.get("tranche_effectif", "") or "")
        return int(m.group(1)) if m else 0

    todo = [r for r in ours if r["siret"] not in have and r["siret"] not in done]
    todo.sort(key=lambda r: -size(r))
    return todo[:limit] if limit else todo


def usable_site(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith("http"):
        return False
    host = urllib.parse.urlparse(low).netloc
    if any(g in host for g in GOOGLE_OWN):
        return False
    if any(a in low for a in AGGREGATORS):
        return False
    return not any(low.endswith(t) or t + "/" in low for t in BAD_TLD)


def panel_blocked(text: str, status: int) -> bool:
    return status in (403, 429) or any(m in (text or "").lower() for m in BLOCK_MARKERS)


def panel_root(page):
    """The Business panel element, or None when Google showed no panel.

    MEASURED 2026-08-17, and the whole reason this file was rewritten: a
    desktop SERP carries ZERO `tel:` links, and the words `Téléphone` and
    `Adresse` appear in the ORGANIC SNIPPETS of company directories
    (societe.com prints `Adresse ... SIRET ... Clef NIC`). Reading the body
    text therefore harvested directory snippets and labelled them `panel`.
    The panel is addressable semantically: Google tags it with `data-attrid`
    keys under `kc:/local:*`, inside `#rhs` / `.kp-wholepage`. No panel is an
    honest no-result — never fall back to the page body.
    """
    for sel in ("#rhs", ".kp-wholepage"):
        el = page.query_selector(sel)
        if el and el.query_selector(PANEL_PROOF):
            return el
    return None


def extract_phone(panel) -> str:
    """A number the PANEL announces, or ""."""
    el = panel.query_selector('a[href^="tel:"]')
    if el:
        p = normalize_fr_phone((el.get_attribute("href") or "")[4:])
        if p and plausible_fr_number(p):
            return p
    for sel in (PHONE_ATTRID, "[data-attrid*='phone']"):
        for node in panel.query_selector_all(sel):
            for c in PHONE_NEAR_RE.finditer(node.inner_text() or ""):
                p = normalize_fr_phone(c.group(0))
                if p and plausible_fr_number(p):
                    return p
    # Last resort, still INSIDE the panel: its own labelled line.
    text = panel.inner_text() or ""
    for m in PHONE_LABEL_RE.finditer(text):
        for c in PHONE_NEAR_RE.finditer(text[m.end():m.end() + CTX_WINDOW]):
            p = normalize_fr_phone(c.group(0))
            if p and plausible_fr_number(p):
                return p
    return ""


def extract_site(panel) -> str:
    """The merchant's own site, from the panel's own Site Web button.

    Never "the first outbound link on the page" — that returned the first
    organic result (banette.fr, boulangerie-ange.fr) as the shop's site,
    the V7 defect that shipped mapquest.com and a dictionary as bakeries'
    websites.
    """
    for el in panel.query_selector_all('a[href^="http"]'):
        href = el.get_attribute("href") or ""
        label = ((el.get_attribute("aria-label") or "") + " " +
                 (el.inner_text() or "")).lower()
        if "site web" not in label and "site internet" not in label:
            continue
        if "/url?" in href:                      # Google's redirect wrapper
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            for key in ("q", "url"):
                if q.get(key) and usable_site(q[key][0]):
                    return urllib.parse.unquote(q[key][0])
            continue
        if usable_site(href):
            return href
    return ""


def extract_address(panel) -> str:
    for sel in (ADDR_ATTRID, "[data-attrid*='address']"):
        node = panel.query_selector(sel)
        if node:
            txt = " ".join((node.inner_text() or "").split())
            txt = ADDR_LABEL_RE.sub("", txt).strip(" :,")
            if txt and not SNIPPET_JUNK_RE.search(txt):
                return txt[:120]
    return ""


def extract_name(panel) -> str:
    """The panel's own heading — independent of our registry name, which is
    what makes it usable evidence in m2_s7's name test."""
    node = panel.query_selector("[data-attrid='title']") or panel.query_selector("h2")
    return " ".join((node.inner_text() or "").split())[:120] if node else ""


def panel_postcode(addr: str) -> str:
    """A real French postcode from the panel address, or "".

    `sorted(CP_RE.findall(addr))[0]` used to win, which on a directory
    snippet picked the SIRET's 5-digit NIC clef (`00028`) as the postcode
    and would have poisoned m2_s7's postcode pool.
    """
    for cp in CP_RE.findall(addr or ""):
        if not cp.startswith("00"):
            return cp
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Read phones off Google Business panels")
    ap.add_argument("--pilot", type=int, default=0,
                    help="stop after N rows and report — run this first")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright required:\n  pip install playwright")

    todo = targets(args.pilot or args.limit)
    if not todo:
        sys.exit("nothing to do — every phoneless row is already done")
    log.info(f"{len(todo)} target(s) without a trusted phone"
             + ("  [PILOT]" if args.pilot else ""))

    found_ph = found_site = blocked = geo_reject = no_panel = 0
    consecutive_blocks = 0
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:
            sys.exit(f"could not attach to Chrome at {CDP_URL} ({exc}).\n"
                     "Close Chrome, then start it with --remote-debugging-port=9222 "
                     "(see this file's docstring) and browse to google.com once.")
        if not browser.contexts:
            sys.exit("attached, but Chrome has no window open")
        page = browser.contexts[0].new_page()
        log.info(f"attached to your Chrome at {CDP_URL}")

        for i, r in enumerate(todo, 1):
            q = f"boulangerie {r['adresse']} {r['code_postal']} {r['commune']}"
            url = ("https://www.google.com/search?hl=fr&q="
                   + urllib.parse.quote_plus(q))
            phone = site = addr = name = cp = ""
            try:
                resp = page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
                status = resp.status if resp else 0
                # The panel renders after first paint; without this wait the
                # panel is simply absent and every row looks like a miss.
                page.wait_for_timeout(PANEL_WAIT)
                panel = panel_root(page)
                if panel is not None:
                    phone = extract_phone(panel)
                    site = extract_site(panel)
                    addr = extract_address(panel)
                    name = extract_name(panel)
                    cp = panel_postcode(addr)
                # Evidence of success outranks evidence of failure.
                if not phone and panel_blocked(page.inner_text("body") or "", status):
                    blocked += 1
                    consecutive_blocks += 1
                    log.warning(f"[{i}/{len(todo)}] BLOCKED status={status}")
                    if consecutive_blocks >= 3:
                        log.error("3 blocks in a row — stopping rather than "
                                  "burning your Chrome profile. Try again later.")
                        break
                    time.sleep(DELAY * 10)
                    continue
                consecutive_blocks = 0
                if panel is None:
                    no_panel += 1
            except Exception as exc:
                log.warning(f"[{i}/{len(todo)}] error {type(exc).__name__}")

            # THE GEO GATE, on what the panel PRINTS. The query already
            # contains our postcode, so finding it in the query proves
            # nothing; finding it in the panel's own address line is the
            # panel agreeing with us. Without a readable postcode we have no
            # location evidence at all, so the row is dropped rather than
            # shipped on the query's say-so.
            if phone or site:
                if not cp:
                    geo_reject += 1
                    phone = site = ""
                elif cp != r["code_postal"]:
                    geo_reject += 1
                    phone = site = ""

            if phone or site:
                row = {
                    "listing_id": f"gp_{r['siret']}",
                    # The panel's own heading; never our registry name, which
                    # would make m2_s7's name test compare our row against
                    # itself.
                    "name": name,
                    "phone": phone, "website": site, "address": addr,
                    "postcode": cp,
                    "city": r["commune"], "query": q,
                }
                new = not OUT_PATH.exists()
                with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";",
                                       quoting=csv.QUOTE_MINIMAL)
                    if new:
                        w.writeheader()
                    w.writerow(row)
                    fh.flush()
                found_ph += bool(phone)
                found_site += bool(site)

            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(r["siret"] + "\n")
            log.info(f"[{i}/{len(todo)}] {r['raison_sociale'][:24]:24.24} "
                     f"{phone or '(no phone)':16} {site[:34]}")
            time.sleep(DELAY + random.uniform(0, 1.0))

        page.close()

    log.info("─" * 62)
    log.info(f"phones: {found_ph}/{len(todo)} | websites: {found_site} | "
             f"blocked: {blocked} | geo-rejected: {geo_reject} | "
             f"no panel: {no_panel}")
    if args.pilot:
        rate = found_ph / len(todo) if todo else 0
        log.info(f"PILOT hit-rate {rate:.0%} — the project's go/no-go is 30%. "
                 "Open a few of the rows above and check the number belongs to "
                 "that address before running the full pass.")
    log.info("Next: m2_s7_match.py (the panel rows are judged like any listing), "
             "then m2_s21 --resume for new domains, m2_s9 --only-valid, "
             "m2_s14 --version v9, m2_s19 --version v9 --baseline v8 --strict.")


if __name__ == "__main__":
    main()
