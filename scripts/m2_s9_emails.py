"""
M2-S9 — Crawl bakery websites and extract their e-mail addresses
=================================================================
"Enter each website and write down the emails it has" — this is that step.

Input: any checkpoint that carries a website for a SIRET row. Today that is
matched.csv (OSM-sourced sites); m2_s8's discovered sites land in the same
file shape and are picked up automatically.

For each domain: fetch the home page plus the pages a French business puts
its address on — /contact, /mentions-legales, /nous-contacter, /a-propos —
then extract e-mails from the HTML (including `mailto:` and the `[at]`
obfuscations) and record where each one was found.

CONFIRMATION IS THE POINT, not the crawl. `EARL DU VIEUX CHENE` guessing to
`vieuxchene.fr` — a real site belonging to someone else — is how agriculture
nearly wrote a stranger's address into the client file. So every domain is
scored:

    siret / siren  : the page shows our identifier            -> confirmed
    cp + name      : postal code AND a name token on the page -> confirmed
    cp only / none : recorded, but flagged faible             -> never
                     pattern-expanded, and marked in the export

French sites are legally required to publish their SIRET in the mentions
légales, which is exactly what makes this test cheap and strong here.

Crash-safety: results are appended and flushed after EVERY domain, and
`emails_done.txt` records finished domains so a re-run resumes. Nothing is
held in memory across the slow part.

Usage:
    python scripts/m2_s9_emails.py                # all pending domains
    python scripts/m2_s9_emails.py --limit 20     # pilot
"""

import argparse
import csv
import logging
import re
import sys
import time
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
MATCHED_PATH = CHECK_DIR / "matched.csv"
OUT_PATH  = CHECK_DIR / "site_emails.csv"
DONE_PATH = CHECK_DIR / "emails_done.txt"

SUBPAGES = ["", "/contact", "/contacts", "/nous-contacter", "/mentions-legales",
            "/mentions-legales/", "/a-propos", "/infos", "/contactez-nous"]
TIMEOUT = 12
DELAY = 1.0

# A chain's "nos boutiques" page lists one mailbox per store, nationwide.
# sophie-lebreuilly.com yielded 94 addresses — abbeville@, amiens@, arras@ —
# and writing all of them onto one Marseille SIRET is the agriculture
# "stranger's email" bug with extra steps. Above this count the page is a
# store list, not a contact page, and only a locally-relevant or generic
# mailbox may be kept.
MAX_EMAILS_PER_DOMAIN = 12
GENERIC_LOCALS = {"contact", "info", "infos", "bonjour", "hello", "accueil",
                  "commande", "commandes", "boutique", "direction"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# contact [at] domain [dot] fr — the obfuscation small French sites still use
OBFUS_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|&#64;|\sat\s)\s*"
    r"([A-Za-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\sdot\s)\s*([A-Za-z]{2,})",
    re.I)
SIRET_RE = re.compile(r"\b(\d[\s.]?){13}\d\b")

# Addresses that belong to the site's builder or a plugin, not the business.
JUNK_LOCAL = {"noreply", "no-reply", "postmaster", "webmaster", "abuse",
              "sentry", "wordpress", "wixpress", "example", "email", "your"}
JUNK_DOMAIN = ("sentry.io", "wixpress.com", "example.com", "wordpress.org",
               "schema.org", "w3.org", "godaddy.com", "sentry-next.wixpress.com")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")

FIELDNAMES = ["siret", "siren", "raison_sociale", "commune", "code_postal",
              "domain", "email", "found_on", "confirmation", "confiance"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s9")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", " ", s).upper()


def name_tokens(s: str) -> set:
    stop = {"BOULANGERIE", "PATISSERIE", "SARL", "SAS", "SASU", "EURL", "LA",
            "LE", "LES", "DE", "DU", "DES", "ET", "MAISON", "FOURNIL", "SNC"}
    return {t for t in norm(s).split() if len(t) > 2 and t not in stop}


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def flush(rows: list[dict]) -> None:
    """Append + flush per domain. Nothing accumulates across the network work."""
    new = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        if new:
            w.writeheader()
        w.writerows(rows)
        fh.flush()


def extract_emails(html: str) -> set:
    found = set()
    for m in EMAIL_RE.finditer(html):
        found.add(m.group(0))
    for m in OBFUS_RE.finditer(html):
        found.add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    clean = set()
    for e in found:
        e = e.strip(".,;:'\"()<>").lower()
        # HTML/JSON escape artifacts glued to the local part: a page carrying
        # ">marius@..." yielded "u003emarius@..." — a guaranteed bounce
        # that looks like a real address. Same family as the welded-domain bug.
        e = re.sub(r"^(?:u00[0-9a-f]{2}|x[0-9a-f]{2}|amp|quot|lt|gt|nbsp|039)+", "", e)
        e = re.sub(r"^[^a-z0-9]+", "", e)
        if any(e.endswith(x) for x in IMG_EXT):
            continue
        local, _, dom = e.partition("@")
        if not dom or "." not in dom:
            continue
        if local in JUNK_LOCAL or any(d in dom for d in JUNK_DOMAIN):
            continue
        if len(local) > 64 or len(e) > 120:
            continue
        clean.add(e)
    return clean


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl bakery sites for e-mail addresses")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    try:
        import requests
    except ImportError:
        sys.exit("requests required:\n  pip install requests")

    if not MATCHED_PATH.exists():
        sys.exit(f"{MATCHED_PATH} not found — run scripts/m2_s7_match.py first.")
    with MATCHED_PATH.open(encoding="utf-8-sig", newline="") as fh:
        matched = [r for r in csv.DictReader(fh, delimiter=";") if r["website"]]

    # One crawl per domain, even when several rows share it (chains).
    by_domain: dict[str, list[dict]] = {}
    for r in matched:
        d = urllib.parse.urlparse(r["website"]).netloc.lower().replace("www.", "")
        if d:
            by_domain.setdefault(d, []).append(r)

    done = load_done()
    todo = [d for d in by_domain if d not in done]
    if args.limit:
        todo = todo[:args.limit]
    log.info(f"{len(by_domain)} distinct domains, {len(done)} already crawled, "
             f"{len(todo)} to do")

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "fr-FR,fr;q=0.9",
    })

    stats = Counter()
    written = 0
    for i, domain in enumerate(todo, 1):
        rows_for_domain = by_domain[domain]
        ref = rows_for_domain[0]
        wanted_tokens = set()
        wanted_sirets, wanted_sirens, wanted_cps = set(), set(), set()
        for r in rows_for_domain:
            wanted_tokens |= name_tokens(f"{r['raison_sociale']} {r['listing_name']}")
            wanted_sirets.add(r["siret"])
            wanted_sirens.add(r["siren"])
            wanted_cps.add(r["code_postal"])

        emails: dict[str, str] = {}     # email -> page it was found on
        pages_text = ""
        reached = False
        for sub in SUBPAGES:
            url = f"https://{domain}{sub}"
            try:
                resp = sess.get(url, timeout=TIMEOUT, allow_redirects=True)
            except Exception:
                continue
            if resp.status_code != 200 or not resp.text:
                continue
            reached = True
            html = resp.text
            pages_text += " " + html
            for e in extract_emails(html):
                emails.setdefault(e, url)
            time.sleep(DELAY)

        if not reached:
            stats["unreachable"] += 1
            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(domain + "\n")
            continue

        # Confirmation — is this site really THIS business?
        digits_blob = re.sub(r"[\s.\-]", "", pages_text)
        confirmation = "none"
        if any(s in digits_blob for s in wanted_sirets):
            confirmation = "siret"
        elif any(s in digits_blob for s in wanted_sirens):
            confirmation = "siren"
        else:
            up = norm(pages_text)
            cp_hit = any(cp in digits_blob for cp in wanted_cps if cp)
            name_hit = any(t in up for t in wanted_tokens)
            if cp_hit and name_hit:
                confirmation = "cp+nom"
            elif cp_hit:
                confirmation = "cp"
        confiance = "confirme" if confirmation in ("siret", "siren", "cp+nom") else "faible"
        # A domain claimed by several DIFFERENT companies is a franchise or
        # network site (franceboulangerie.fr covered three of ours), so its
        # mailbox belongs to the network, not to any one bakery. Measured, not
        # assumed: the same marius@ was about to be written onto 3 SIRETs.
        shared_domain = len({r["siret"] for r in rows_for_domain}) > 1
        if shared_domain:
            confirmation = "reseau"
            confiance = "faible"
            stats["shared_network_domain"] += 1
        stats[f"conf:{confirmation}"] += 1

        out = []
        for r in rows_for_domain:
            keep = emails
            if len(emails) > MAX_EMAILS_PER_DOMAIN:
                commune_key = norm(r["commune"]).replace(" ", "").lower()
                keep = {e: p for e, p in emails.items()
                        if e.partition("@")[0].replace(".", "").replace("-", "") == commune_key
                        or e.partition("@")[0] in GENERIC_LOCALS}
                stats["chain_store_list_capped"] += 1
                if not keep:
                    stats["chain_store_list_dropped"] += 1
            for e, page in sorted(keep.items()):
                out.append({
                    "siret": r["siret"], "siren": r["siren"],
                    "raison_sociale": r["raison_sociale"], "commune": r["commune"],
                    "code_postal": r["code_postal"], "domain": domain,
                    "email": e, "found_on": page,
                    "confirmation": confirmation, "confiance": confiance,
                })
        if out:
            flush(out)                       # on disk before the next domain
            written += len(out)
            stats["domains_with_email"] += 1
        with DONE_PATH.open("a", encoding="utf-8") as f:
            f.write(domain + "\n")
        log.info(f"[{i}/{len(todo)}] {domain:38.38s} emails={len(emails)} "
                 f"conf={confirmation:6s} written={written}")

    log.info("─" * 62)
    log.info(f"written={written} rows -> {OUT_PATH}")
    log.info(f"domains yielding an email: {stats['domains_with_email']} | "
             f"unreachable: {stats['unreachable']}")
    for k in sorted(k for k in stats if k.startswith("conf:")):
        log.info(f"  {k:<18} {stats[k]}")
    log.info("Only 'confirme' rows may be pattern-expanded (m2_s10) or shipped "
             "without a warning column.")


if __name__ == "__main__":
    main()
