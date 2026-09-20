"""M2-S18 — read contact e-mails off the shops' own Facebook / Instagram pages.

WHY THIS IS THE LAST POOL OF ANY SIZE
=====================================
V11 ships 179 e-mails. Only 208 of 1,704 bakeries have a website at all, and we
already hold an address for most of those — the crawl route is finished. But
473 businesses have a Facebook or Instagram page, and 386 of them ship no
e-mail. A French shop's FB page very often prints a contact address in its
"Infos" tab, and an IG business profile exposes `business_email` in the JSON
its own page loads.

WHAT MAKES THE ATTRIBUTION SAFE HERE, AND ONLY HERE
===================================================
Reading an address off a page and calling it the shop's is exactly the mistake
that produced 320 supplier addresses in V3 (`pavailler.com` on 24 bakery
sites). It is safe on this source ONLY because of what V11 did to the social
columns first:

  * H23 — every shipped social URL is a page ROOT, never a post or a video, so
    the page is somebody's identity and not an article about them;
  * H24 — no social URL is shared by two of our SIREN, so the page belongs to
    exactly one business in our base.

Those two gates are the precondition. If either is ever softened, this script
starts attributing a food blog's mailbox to a bakery and must be stopped.

PUBLIC PAGES ONLY — NO LOGIN. NOT NEGOTIABLE.
=============================================
Ines agreed this constraint when the pilot was designed, and it also protects
her: driving Facebook from her own signed-in Chrome (the CDP trick that opened
Pages Jaunes and Google) would put her personal account at risk of being
flagged, and an authenticated scrape is a different thing legally and ethically
from reading a public page. So this script launches its OWN empty browser —
no profile, no cookies, no session. If the public pages turn out to be
login-walled, that is a MEASUREMENT and the answer is "this source is closed",
never "log in".

WHY IT RENDERS INSTEAD OF FETCHING — the mistake this file already made
======================================================================
The first version of this script used `requests`, scored **0 of 20**, and was
one commit away from being written up as "FB/IG is closed". It was measuring
the wrong thing, which is precisely the Pages Jaunes reversal happening again:

  * Instagram answers an anonymous GET with **HTTP 200 and a 609 KB app
    shell** — 609,748 bytes for one profile and 609,746 for another, i.e. the
    same empty page. The profile is drawn by JavaScript afterwards. Nothing is
    login-walled; there is simply no data in the HTML.
  * `mbasic.facebook.com` now answers **400**.

Rendered anonymously, both open up. The first FB page tried printed a mailbox,
a mobile number and a postal address in plain view. *A measurement about one
setup is never a verdict about the goal.*

WHAT IS TAKEN, AND THE GEO GATE
===============================
E-mail, phone and the page's own printed address. The address is not payload,
it is EVIDENCE: these social URLs were originally harvested from search-result
blobs, so the page→business link is our weakest one. When the page prints a
postcode it must agree with ours, or the row is dropped. Phones ship as claims
(m2_s14 routes an unranked source to the corroboration pool), never as dialled
numbers.

THE GO/NO-GO
============
`--pilot 20` first, always. The project's threshold is 30%: fewer than 6 of 20
pages yielding a usable address means the source does not pay for the run, and
the honest move is to record the number and stop.

    python scripts/m2_s18_social_emails.py --pilot 20
    python scripts/m2_s18_social_emails.py            # only after the pilot passes

Resumable: one line per done SIRET in `social_done.txt`, rows appended and
flushed one at a time — the S9-J lesson (3,402 listings lost by writing only
at the end).
"""

import argparse
import csv
import logging
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m2_s9_emails import EMAIL_RE                          # noqa: E402
from m2lib_contact import is_third_party_email             # noqa: E402
from m2_s8_websites import AGGREGATORS                     # noqa: E402
from m2_s21_validate_sites import load_communes, norm      # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
OUT_PATH = CHECK_DIR / "social_emails.csv"
DONE_PATH = CHECK_DIR / "social_done.txt"

FIELDNAMES = ["siret", "siren", "raison_sociale", "network", "page_url",
              "email", "phone", "page_address", "found_via", "confiance"]

DELAY = 3.0          # slower than the site crawl: one shot per page, be a guest
TIMEOUT = 30_000
RENDER_WAIT = 3_500  # the profile is drawn by JS after domcontentloaded
CP_RE = re.compile(r"\b(\d{5})\b")
PHONE_RE = re.compile(
    r"(?:\+33|0033|0)[\s.\-]?[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")

# An address that is never a bakery's, however it was found. The suppliers and
# platforms that V3 measured, plus the social platforms themselves.
NEVER = ("facebook.com", "instagram.com", "fb.com", "meta.com", "gmail.com.",
         "sentry.io", "example.com", "wixpress.com", "squarespace.com",
         "godaddy.com", "shopify.com", "cloudflare.com", "google.com")

# The page did not load as a public page. Measured markers, French + English.
WALL = ("you must log in", "vous devez vous connecter", "connectez-vous",
        "log into facebook", "connexion à facebook", "connexion a facebook",
        "se connecter à facebook", "page isn't available", "contenu introuvable",
        "login_form", "loginform", "/login/?next=", "restricted_content")

# Instagram business profiles carry the address in their own bootstrap JSON.
IG_BIZ_RE = re.compile(r'"business_email"\s*:\s*"([^"]+)"')
# Facebook prints it in the Infos tab and, often, in the page's meta blob.
FB_MAILTO_RE = re.compile(r'mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})')

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s18")


def read_rows(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def latest_export() -> Path:
    """The newest vN csv — the social columns must be the CLEANED ones."""
    csvs = sorted((PROJECT_ROOT / "exports" / "boulangerie").glob("boulangerie_13_v*.csv"),
                  key=lambda p: int(re.search(r"_v(\d+)\.csv$", p.name).group(1)))
    if not csvs:
        sys.exit("no export found — run m2_s14 first; this script reads the "
                 "CLEANED social columns, not the raw checkpoints")
    return csvs[-1]


def targets(limit: int = 0, network: str = "") -> list:
    """Businesses with a social page and no e-mail, biggest first."""
    export = latest_export()
    rows = read_rows(export)
    log.info(f"reading social columns from {export.name}")
    ours = {r["siret"]: r for r in read_rows(OURS_PATH)}
    done = load_done()
    out = []
    for r in rows:
        s = r.get("SIRET", "")
        if s in done or (r.get("Email") or "").strip():
            continue
        for net, col in (("facebook", "Facebook"), ("instagram", "Instagram")):
            if network and net != network:
                continue
            u = (r.get(col) or "").strip()
            if u:
                out.append({"siret": s, "siren": r.get("SIREN", ""),
                            "raison_sociale": r.get("Raison sociale", ""),
                            "network": net, "page_url": u,
                            "_size": ours.get(s, {}).get("tranche_effectif", "")})
    def size(r):
        m = re.match(r"(\d+)", r.get("_size", "") or "")
        return int(m.group(1)) if m else 0
    out.sort(key=lambda r: -size(r))
    return out[:limit] if limit else out


def walled(html: str) -> bool:
    low = (html or "").lower()
    return len(low) < 800 or any(w in low for w in WALL)


def candidate_urls(network: str, page_url: str) -> list:
    """Public shapes worth one request each, cheapest first."""
    slug = urllib.parse.urlparse(page_url).path.strip("/")
    if network == "facebook":
        return [f"https://mbasic.facebook.com/{slug}/about",
                f"https://www.facebook.com/{slug}/about_contact_and_basic_info",
                page_url]
    return [page_url, f"https://www.instagram.com/{slug}/"]


_COMMUNES = None


def is_public_body(email: str) -> bool:
    """A town hall's mailbox, which is nobody's prospect.

    The pilot's third hit was `courrier@allauch.com` — the MAIRIE of Allauch —
    on a bakery's page, with the mairie's switchboard beside it. Same defect
    as `contact@saint-chamas.com` in the generated addresses (H25), arriving
    by a different route: a commune runs the page that lists its local shops.
    A dept-13 commune name AS the mail domain is the tell.
    """
    global _COMMUNES
    if _COMMUNES is None:
        try:
            _COMMUNES = load_communes()
        except Exception:
            _COMMUNES = frozenset()
    dom = (email or "").lower().partition("@")[2]
    root = dom.rpartition(".")[0].rpartition(".")[2] if dom.count(".") > 1 else dom.partition(".")[0]
    if any(k in dom for k in ("mairie", "ville-", ".gouv.", "prefecture", "cci.fr")):
        return True
    return bool(root) and norm(root.replace("-", " ")) in _COMMUNES


def usable_email(email: str, page_url: str) -> bool:
    e = (email or "").lower()
    if e.count("@") != 1 or "." not in e.partition("@")[2]:
        return False
    dom = e.partition("@")[2]
    if any(n in dom for n in NEVER):
        return False
    if any(a in dom for a in AGGREGATORS):
        return False
    if is_public_body(e):
        return False
    # The supplier guard, reused. crawled_domain is the SOCIAL host here, so a
    # corporate address never "shares the crawled domain" — the function then
    # keeps consumer mailboxes and rejects known suppliers, which is exactly
    # the judgement we want on a page whose owner is already established.
    return not is_third_party_email(e, dom)


def harvest(page, row: dict) -> dict:
    """What this public page shows about the business. Rendered, never fetched."""
    out = {"email": "", "phone": "", "page_address": "", "found_via": "",
           "walled": False}
    for url in candidate_urls(row["network"], row["page_url"]):
        try:
            page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(RENDER_WAIT)
            text = page.inner_text("body") or ""
            html = page.content() or ""
        except Exception:
            continue
        # Evidence of success outranks evidence of failure: a page that shows
        # the profile is not walled just because it also offers a login box —
        # every anonymous FB/IG page does. Only an EMPTY page is walled.
        if len(text) < 220 and walled(text):
            out["walled"] = True
            continue
        m = IG_BIZ_RE.search(html)
        if m and usable_email(m.group(1), url):
            out.update(email=m.group(1).lower(), found_via="business_email")
        if not out["email"]:
            m = FB_MAILTO_RE.search(html)
            if m and usable_email(m.group(1), url):
                out.update(email=m.group(1).lower(), found_via="mailto")
        if not out["email"]:
            # Only the VISIBLE text — the raw HTML of a social page is full of
            # the platform's own telemetry and script mailboxes.
            for cand in EMAIL_RE.findall(text):
                if usable_email(cand, url):
                    out.update(email=cand.lower(), found_via="page_text")
                    break
        pm = PHONE_RE.search(text)
        if pm:
            out["phone"] = pm.group(0)
        cps = CP_RE.findall(text)
        if cps:
            out["page_address"] = " ".join(text.split())[:160]
            out["_cps"] = cps
        if out["email"] or out["phone"]:
            return out
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Read e-mails off public FB/IG pages")
    ap.add_argument("--pilot", type=int, default=0,
                    help="stop after N pages and report — run this FIRST")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--network", choices=("facebook", "instagram"),
                    help="restrict to one network — the pilot measured them as "
                         "two different sources, not one")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright required:\n  pip install playwright")

    todo = targets(args.pilot or args.limit, args.network or "")
    if not todo:
        sys.exit("nothing to do — every social page is already checked")
    log.info(f"{len(todo)} social page(s) for businesses with no e-mail"
             + ("  [PILOT]" if args.pilot else ""))

    ours = {r["siret"]: r for r in read_rows(OURS_PATH)}
    found = phones = geo_dropped = 0
    walls = Counter()
    via = Counter()
    pw = sync_playwright().start()
    # OUR OWN empty browser: no user-data-dir, no profile, no cookies. This is
    # what keeps the "public pages only, no login" promise literally true, and
    # keeps Ines's own Facebook account out of it entirely.
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(locale="fr-FR")
    for i, r in enumerate(todo, 1):
        got = harvest(page, r)
        email, how = got["email"], got["found_via"]
        walls[r["network"]] += 1 if got["walled"] and not email else 0

        # THE GEO GATE. These page→business links came from search blobs, our
        # weakest evidence. When the page prints a postcode it has to be ours;
        # a page that names a different commune is a different business, and
        # its mailbox is not our prospect's.
        cps = got.get("_cps") or []
        mine = (ours.get(r["siret"], {}) or {}).get("code_postal", "")
        if cps and mine and mine not in cps:
            geo_dropped += 1
            log.info(f"[{i}/{len(todo)}] {r['raison_sociale'][:26]:<26} "
                     f"{r['network'][:2]}  GEO-REJECT (page says {cps[:2]}, we are {mine})")
            with DONE_PATH.open("a", encoding="utf-8") as fh:
                fh.write(r["siret"] + "\n")
                fh.flush()
            time.sleep(DELAY)
            continue

        if email or got["phone"]:
            found += 1 if email else 0
            phones += 1 if got["phone"] else 0
            if email:
                via[how] += 1
            row = {"siret": r["siret"], "siren": r["siren"],
                   "raison_sociale": r["raison_sociale"], "network": r["network"],
                   "page_url": r["page_url"], "email": email,
                   "phone": got["phone"], "page_address": got["page_address"],
                   "found_via": how or "phone_only",
                   # Never `confirme`: the page is the business's, but an
                   # address printed on it is still one step from the shop's
                   # own domain. m2_s14 ships this as a warned column.
                   "confiance": "faible"}
            new = not OUT_PATH.exists()
            with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";",
                                   quoting=csv.QUOTE_MINIMAL)
                if new:
                    w.writeheader()
                w.writerow(row)
                fh.flush()
        with DONE_PATH.open("a", encoding="utf-8") as fh:
            fh.write(r["siret"] + "\n")
            fh.flush()
        log.info(f"[{i}/{len(todo)}] {r['raison_sociale'][:26]:<26} "
                 f"{r['network'][:2]}  {email or '(no e-mail)'}"
                 f"{'  tel ' + got['phone'] if got['phone'] else ''}")
        time.sleep(DELAY)

    browser.close()
    pw.stop()
    log.info("─" * 65)
    log.info(f"e-mails: {found}/{len(todo)} | phones: {phones} | "
             f"by route: {dict(via)} | geo-rejected: {geo_dropped} | "
             f"empty/walled: {dict(walls)}")
    if args.pilot:
        rate = found / len(todo) if todo else 0
        log.info(f"PILOT hit-rate {rate:.0%} — the project's go/no-go is 30%.")
        if rate < 0.30:
            log.info("BELOW THRESHOLD. Record the number in docs/RUNBOOK.md "
                     "and close this source. Do NOT 'fix' it by logging in.")


if __name__ == "__main__":
    main()
