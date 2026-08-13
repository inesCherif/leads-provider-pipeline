"""
M2-S9 — Crawl bakery websites: e-mails, phones and social pages
=================================================================
"Enter each website and write down the emails it has" — this is that step.
V3 also writes down the PHONES and the FACEBOOK/INSTAGRAM/LINKEDIN pages,
because the same fetched HTML carries them and phone is the channel for
this sector.

Input: every checkpoint that carries a website for a SIRET row —
matched.csv (OSM + Google Maps harvests) AND discovered_sites.csv (m2_s8
search discovery). V2 read only matched.csv despite its docstring claiming
otherwise; the discovered sites were never crawled. Fixed here.

For each domain: fetch the home page plus the pages a French business puts
its address on — /contact, /mentions-legales, /nous-contacter, /a-propos —
then extract e-mails (including `mailto:` and the `[at]` obfuscations),
phones (normalized, surtaxé-flagged) and social page URLs, and record where
each one was found.

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

Three guards, each born from a real near-miss (see docs/m2_progress.md),
now applied to phones as well as e-mails:
  * >12 addresses (or phones) on one domain = a chain's store list, not a
    contact page — sophie-lebreuilly.com yielded 94 mailboxes for other
    départements. Only locally-relevant or generic mailboxes survive;
    phones cannot be locality-tested, so a phone store-list keeps NOTHING.
  * a domain claimed by several of our SIRETs is a network site (`reseau`,
    faible) — franceboulangerie.fr covered three of ours.
  * escape artifacts (`u003emarius@…`) are stripped, not shipped.

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

sys.path.insert(0, str(Path(__file__).parent))
from m2lib_contact import extract_phones_ctx, extract_social, is_surtaxe  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
MATCHED_PATH = CHECK_DIR / "matched.csv"
DISCOVERED_PATH = CHECK_DIR / "discovered_sites.csv"
OUT_PATH  = CHECK_DIR / "site_emails.csv"
CONTACTS_PATH = CHECK_DIR / "site_contacts.csv"
DONE_PATH = CHECK_DIR / "emails_done.txt"

SUBPAGES = ["", "/contact", "/contacts", "/nous-contacter", "/mentions-legales",
            "/mentions-legales/", "/a-propos", "/infos", "/contactez-nous",
            "/contact.html", "/contact.php", "/nous-trouver"]
TIMEOUT = 12
DELAY = 1.0

# A chain's "nos boutiques" page lists one mailbox per store, nationwide.
# sophie-lebreuilly.com yielded 94 addresses — abbeville@, amiens@, arras@ —
# and writing all of them onto one Marseille SIRET is the agriculture
# "stranger's email" bug with extra steps. Above this count the page is a
# store list, not a contact page, and only a locally-relevant or generic
# mailbox may be kept.
MAX_EMAILS_PER_DOMAIN = 12
MAX_PHONES_PER_DOMAIN = 12   # same logic; phones have no locality test, so
                             # a phone store-list keeps nothing at all
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
# Social pages registered as a "website" by the shop itself (Maps and OSM both
# allow it). They are captured as social links, never crawled as a site.
SOCIAL_HOSTS = ("facebook.com", "fb.com", "fb.me", "instagram.com",
                "linkedin.com", "tiktok.com", "twitter.com", "x.com")

FIELDNAMES = ["siret", "siren", "raison_sociale", "commune", "code_postal",
              "domain", "email", "found_on", "confirmation", "confiance"]
CONTACT_FIELDNAMES = ["siret", "siren", "raison_sociale", "commune",
                      "code_postal", "domain", "phone", "surtaxe",
                      "phones_autres", "facebook", "instagram", "linkedin",
                      "found_on", "confirmation", "confiance"]

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


def flush(path: Path, fieldnames: list, rows: list) -> None:
    """Append + flush per domain. Nothing accumulates across the network work."""
    new = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=";", quoting=csv.QUOTE_MINIMAL)
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


def load_targets() -> dict:
    """domain -> [row dicts]. matched.csv + discovered_sites.csv, deduped."""
    rows = []
    if MATCHED_PATH.exists():
        with MATCHED_PATH.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                if r["website"]:
                    rows.append({**r, "_name_hint": r.get("listing_name", "")})
    if DISCOVERED_PATH.exists():
        with DISCOVERED_PATH.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                if r["website"]:
                    rows.append({**r, "_name_hint": r.get("enseigne", "")})
    by_domain: dict = {}
    seen_pairs = set()
    for r in rows:
        d = urllib.parse.urlparse(r["website"]).netloc.lower().replace("www.", "")
        # A social page is not a website: Google Maps lets a shop register its
        # Facebook page as its site, and crawling facebook.com for a bakery's
        # e-mail yields nothing but a login wall.
        if any(h in d for h in SOCIAL_HOSTS):
            continue
        if not d or (r["siret"], d) in seen_pairs:
            continue
        seen_pairs.add((r["siret"], d))
        by_domain.setdefault(d, []).append(r)
    return by_domain


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl bakery sites for e-mails, phones, socials")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    try:
        import requests
    except ImportError:
        sys.exit("requests required:\n  pip install requests")

    by_domain = load_targets()
    if not by_domain:
        sys.exit("No websites found in matched.csv / discovered_sites.csv — "
                 "run m2_s7_match.py (and m2_s8_websites.py) first.")

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
    written = contacts_written = 0
    for i, domain in enumerate(todo, 1):
        rows_for_domain = by_domain[domain]
        wanted_tokens = set()
        wanted_sirets, wanted_sirens, wanted_cps = set(), set(), set()
        for r in rows_for_domain:
            wanted_tokens |= name_tokens(f"{r['raison_sociale']} {r['_name_hint']}")
            wanted_sirets.add(r["siret"])
            wanted_sirens.add(r["siren"])
            wanted_cps.add(r["code_postal"])

        emails: dict = {}       # email -> page it was found on
        phones: dict = {}       # normalized phone -> page it was found on
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
            for p in extract_phones_ctx(html):
                phones.setdefault(p, url)
            time.sleep(DELAY)

        if not reached:
            stats["unreachable"] += 1
            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(domain + "\n")
            continue

        social = extract_social(pages_text)

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

        # A phone store-list: unlike mailboxes (abbeville@…), phones carry no
        # locality marker we can test, so past the cap NOTHING is kept.
        if len(phones) > MAX_PHONES_PER_DOMAIN:
            stats["phone_store_list_dropped"] += 1
            phones = {}

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
            flush(OUT_PATH, FIELDNAMES, out)     # on disk before the next domain
            written += len(out)
            stats["domains_with_email"] += 1

        if phones or any(social.values()):
            ordered = sorted(phones, key=lambda p: (is_surtaxe(p), p))
            primary = ordered[0] if ordered else ""
            crows = []
            for r in rows_for_domain:
                crows.append({
                    "siret": r["siret"], "siren": r["siren"],
                    "raison_sociale": r["raison_sociale"], "commune": r["commune"],
                    "code_postal": r["code_postal"], "domain": domain,
                    "phone": primary,
                    "surtaxe": "oui" if primary and is_surtaxe(primary) else "",
                    "phones_autres": " | ".join(ordered[1:4]),
                    "facebook": social["facebook"],
                    "instagram": social["instagram"],
                    "linkedin": social["linkedin"],
                    "found_on": phones.get(primary, ""),
                    "confirmation": confirmation, "confiance": confiance,
                })
            flush(CONTACTS_PATH, CONTACT_FIELDNAMES, crows)
            contacts_written += len(crows)
            stats["domains_with_phone_or_social"] += 1

        with DONE_PATH.open("a", encoding="utf-8") as f:
            f.write(domain + "\n")
        log.info(f"[{i}/{len(todo)}] {domain:38.38s} emails={len(emails)} "
                 f"phones={len(phones)} conf={confirmation:6s} "
                 f"written={written}+{contacts_written}")

    log.info("─" * 62)
    log.info(f"written={written} email rows -> {OUT_PATH}")
    log.info(f"written={contacts_written} contact rows -> {CONTACTS_PATH}")
    log.info(f"domains yielding an email: {stats['domains_with_email']} | "
             f"phone/social: {stats['domains_with_phone_or_social']} | "
             f"unreachable: {stats['unreachable']}")
    for k in sorted(k for k in stats if k.startswith(("conf:", "phone_", "chain_"))):
        log.info(f"  {k:<26} {stats[k]}")
    log.info("Only 'confirme' rows may be pattern-expanded (m2_s10) or shipped "
             "without a warning column.")


if __name__ == "__main__":
    main()
