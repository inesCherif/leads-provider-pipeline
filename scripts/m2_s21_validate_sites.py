"""
M2-S21 — Visit every candidate website and judge it, per SIRET
===============================================================
Sam's ask #1, 2026-08-17: "revisiter les sites et extraire le contenu (la
home) pour valider le site web (boulangerie) ou supprimer le site web si
incorrect."

WHY IT IS PER (SIRET, DOMAIN) AND NOT PER DOMAIN. `lamiedepain-boulangerie.fr`
is the right answer for two of our rows and the wrong answer for five others.
A verdict about a *domain* cannot express that; a verdict about a *pair* can.
132 domains in V6 ship on more than one SIRET, covering 710 of the 1,013
shipped sites, so this is the common case, not the corner case.

WHAT IT WRITES: checkpoints/site_verdicts.csv, one row per (siret, domain).
m2_s14 reads it and refuses to ship anything whose verdict is not `valide`
or `non_verifiable`; m2_s19 fails the build if that rule is broken.

Verdicts (see m2lib_validate):
    valide          bakery content + ownership proof for THIS siret   -> ships
    non_verifiable  reachable but JS-only shell, nothing to judge     -> ships, flagged
    reseau          chain domain, no page of this shop's own          -> deleted
    annuaire        directory/aggregator content                      -> deleted
    hors_sujet      real site, wrong business (incl. another bakery)  -> deleted
    parked / mort   parking page / unreachable                        -> deleted

`non_verifiable` ships on purpose: Sam asked us to delete sites that are
WRONG, not sites we failed to render. Deleting on our own inability is the
agriculture DNS-timeout bug — where our error was written down as a fact
about the data — and it is not repeated here.

Crash-safety and politeness follow m2_s9: append + flush after every domain,
`validate_done.txt` records finished domains so a re-run resumes, one second
between requests, at most 4 pages per domain plus (for chain domains only) a
sitemap lookup and one shop-page fetch per row.

Usage:
    python scripts/m2_s21_validate_sites.py --pilot          # 25 worst offenders
    python scripts/m2_s21_validate_sites.py --only-shipped   # the 435 V6 domains
    python scripts/m2_s21_validate_sites.py                  # every candidate
    python scripts/m2_s21_validate_sites.py --domain mapquest.com --force
"""

import argparse
import csv
import logging
import re
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m2_s9_emails import (SOCIAL_HOSTS, flush, load_done,  # noqa: E402
                          name_tokens, norm, read_rows)
from m2lib_validate import (VERDICT_SHIPS, bakery_score,  # noqa: E402
                            classify, is_directory_like, ownership,
                            shop_page_candidates, strip_tags)

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
EXPORT_DIR = PROJECT_ROOT / "exports" / "boulangerie"
MATCHED_PATH = CHECK_DIR / "matched.csv"
DISCOVERED_PATH = CHECK_DIR / "discovered_sites.csv"
EMAILS_PATH = CHECK_DIR / "site_emails.csv"
CONTACTS_PATH = CHECK_DIR / "site_contacts.csv"
ETAB_PATH = CHECK_DIR / "etablissements.csv"
OUT_PATH = CHECK_DIR / "site_verdicts.csv"
DONE_PATH = CHECK_DIR / "validate_done.txt"

# Home first: it is the page Sam named, and the one a directory gives itself
# away on. /contact and /mentions-legales are where a French business is
# legally required to print the SIRET that proves the site is theirs.
PAGES = ["", "/contact", "/mentions-legales"]
TIMEOUT = 12
DELAY = 1.0
MAX_SHOP_FETCH = 12          # per domain, so a 53-row chain cannot cost 53 fetches
CONTACT_URL_RE = re.compile(r"contact|nous-trouver|nous-joindre|coordonnees", re.I)

FIELDNAMES = ["siret", "siren", "raison_sociale", "commune", "code_postal",
              "domain", "url_recorded", "verdict", "reason", "ownership",
              "bakery_score", "text_len", "shop_url", "contact_url", "checked_at"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s21")


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url or "").netloc.lower().replace("www.", "")


def load_communes() -> frozenset:
    """Dept-13 commune names, for the directory breadth test."""
    out = set()
    for r in read_rows(ETAB_PATH):
        c = norm(r.get("commune", ""))
        if c:
            out.add(c)
    return frozenset(out)


def load_targets() -> dict:
    """domain -> [row dicts]. The same four sources m2_s14's site cascade
    reads, in the same order, so nothing that can ship goes unjudged."""
    rows = []
    for r in read_rows(MATCHED_PATH):
        if r.get("website"):
            rows.append({**r, "_hint": r.get("listing_name", ""), "_url": r["website"]})
    for r in read_rows(DISCOVERED_PATH):
        if r.get("website"):
            rows.append({**r, "_hint": r.get("enseigne", ""), "_url": r["website"]})
    for path in (EMAILS_PATH, CONTACTS_PATH):
        for r in read_rows(path):
            if r.get("domain"):
                rows.append({**r, "_hint": "", "_url": f"https://{r['domain']}"})

    by_domain: dict = {}
    seen = set()
    for r in rows:
        d = host_of(r["_url"]) or (r.get("domain") or "").lower().replace("www.", "")
        if not d or any(h in d for h in SOCIAL_HOSTS):
            continue
        key = (r.get("siret", ""), d)
        if key in seen:
            continue
        seen.add(key)
        by_domain.setdefault(d, []).append({
            "siret": r.get("siret", ""), "siren": r.get("siren", ""),
            "raison_sociale": r.get("raison_sociale", ""),
            "commune": r.get("commune", ""), "code_postal": r.get("code_postal", ""),
            "hint": r["_hint"], "url": r["_url"],
        })
    return by_domain


def load_known_phones() -> dict:
    """siret -> {phones} from the sources V3 measured as trustworthy. A page
    printing the number OSM and Google Maps independently give for this SIRET
    is strong ownership evidence."""
    out = defaultdict(set)
    for r in read_rows(MATCHED_PATH):
        if r.get("phone") and r.get("source") in ("osm", "serper_places", "pagesjaunes"):
            out[r["siret"]].add(r["phone"])
    return out


def load_shipped() -> dict:
    """domain -> how many V6 rows ship it. Drives --only-shipped and the
    pilot ordering, so `--pilot` re-tests the worst known offenders first."""
    counts: Counter = Counter()
    xlsx = EXPORT_DIR / "boulangerie_13_v6.xlsx"
    csv_path = EXPORT_DIR / "boulangerie_13_v6.csv"
    if xlsx.exists():
        try:
            import openpyxl
            wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
            ws = wb.active
            rows = ws.iter_rows(values_only=True)
            header = [str(c or "") for c in next(rows)]
            idx = header.index("Site web")
            for row in rows:
                d = host_of(str(row[idx] or ""))
                if d:
                    counts[d] += 1
            wb.close()
            return counts
        except Exception as exc:                      # pragma: no cover
            log.warning(f"could not read {xlsx.name} ({exc}); falling back to csv")
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                d = host_of(r.get("Site web", ""))
                if d:
                    counts[d] += 1
    return counts


def site_urls(sess, domain: str, cap: int = 300) -> list:
    """Every same-domain URL the site advertises, unfiltered.

    m2_s9's deep_urls() cannot be reused here: it keeps only contact-shaped
    links (contact|mention|horaire|trouver…), and a chain's shop page is
    named after the SHOP. Sam's own example,
    /boulangerie-la-mie-de-pain-marseille/, matches none of those words, so
    reusing deep_urls() silently guaranteed we would never find it.
    """
    urls, seen = [], set()

    def add(u: str) -> None:
        u = u.split("#")[0].split("?")[0].rstrip("/")
        if u and u not in seen and len(urls) < cap:
            seen.add(u)
            urls.append(u)

    for sm in (f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml"):
        try:
            resp = sess.get(sm, timeout=TIMEOUT)
        except Exception:
            continue
        if resp.status_code != 200 or "<" not in resp.text:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text, re.I)
        # A sitemap index points at more sitemaps; follow one level.
        for loc in locs[:20] if locs and locs[0].endswith(".xml") else []:
            try:
                sub = sess.get(loc, timeout=TIMEOUT)
                if sub.status_code == 200:
                    locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub.text, re.I)
            except Exception:
                continue
        for loc in locs:
            if domain in loc and not loc.endswith(".xml"):
                add(loc)
        break
    try:
        resp = sess.get(f"https://{domain}", timeout=TIMEOUT)
        if resp.status_code == 200:
            for href in re.findall(r'href=["\']([^"\']+)["\']', resp.text):
                full = urllib.parse.urljoin(f"https://{domain}/", href)
                if host_of(full) == domain:
                    add(full)
    except Exception:
        pass
    return urls


def fetch(sess, url: str) -> tuple:
    """(html, answered). `answered` is True when a server replied at all —
    even 403. A host that refuses our user-agent is alive, and calling it
    dead would delete a real bakery's site on the strength of our own
    failure. rubypayeur.com answered 403 in the first pilot."""
    try:
        resp = sess.get(url, timeout=TIMEOUT, allow_redirects=True)
    except Exception:
        return "", False
    if resp.status_code != 200 or not resp.text:
        return "", True
    return resp.text, True


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate that each site is really that bakery's")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pilot", action="store_true",
                    help="--limit 25 --only-shipped: the worst V6 offenders first")
    ap.add_argument("--only-shipped", action="store_true",
                    help="only domains that actually appear in the V6 deliverable")
    ap.add_argument("--domain", default="",
                    help="re-check one domain, or a comma-separated list")
    ap.add_argument("--redo-verdict", default="",
                    help="re-judge every domain holding a pair with this "
                         "verdict (e.g. hors_sujet) — for when the RULES "
                         "changed rather than the sites")
    ap.add_argument("--force", action="store_true", help="ignore the done file")
    args = ap.parse_args()
    if args.pilot:
        args.limit = args.limit or 25
        args.only_shipped = True

    try:
        import requests
    except ImportError:
        sys.exit("requests required:\n  pip install requests")

    by_domain = load_targets()
    if not by_domain:
        sys.exit("No candidate websites found — run m2_s7_match.py / m2_s8_websites.py first.")
    communes = load_communes()
    phones_of = load_known_phones()
    shipped = load_shipped()

    done = set() if args.force else load_done(DONE_PATH)
    todo = [d for d in by_domain if d not in done]
    if args.redo_verdict:
        # site_verdicts.csv is append-only and every reader takes the LAST row
        # for a pair, so re-judging simply supersedes the old verdict.
        want_v = {v.strip() for v in args.redo_verdict.split(",")}
        again = {r["domain"] for r in read_rows(OUT_PATH) if r["verdict"] in want_v}
        todo = [d for d in by_domain if d in again]
        log.info(f"--redo-verdict {sorted(want_v)}: {len(todo)} domains to re-judge")
    elif args.domain:
        wants = [w.strip().lower().replace("www.", "")
                 for w in args.domain.split(",") if w.strip()]
        todo = [d for d in by_domain if any(w in d for w in wants)]
    elif args.only_shipped:
        todo = [d for d in todo if d in shipped]
    # Worst first: a --limit 25 pilot then covers every junk domain we already
    # know about (mapquest, toogoodtogo, lafabrique, uneboulangerie, ange...).
    todo.sort(key=lambda d: (-shipped.get(d, 0), d))
    if args.limit:
        todo = todo[:args.limit]

    log.info(f"{len(by_domain)} candidate domains | {len(shipped)} shipped in V6 | "
             f"{len(done)} already judged -> {len(todo)} to do")
    log.info(f"{len(communes)} dept-13 commune names loaded for the directory test")

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "fr-FR,fr;q=0.9",
    })

    stats: Counter = Counter()
    written = 0
    for i, domain in enumerate(todo, 1):
        rows = by_domain[domain]
        # Keyed on SIREN, like the switchboard guard: a genuine company with
        # several établissements keeping ONE website is not a network site.
        shared = len({r["siren"] for r in rows if r["siren"]}) > 1

        html_all, contact_url, reached, answered = "", "", False, False
        for n_page, sub in enumerate(PAGES):
            url = f"https://{domain}{sub}"
            html, ans = fetch(sess, url)
            answered = answered or ans
            time.sleep(DELAY)
            # No TCP answer at all on the home page: /contact and
            # /mentions-legales live on the same dead host, so trying them
            # only buys two more 12-second timeouts. Measured: dead domains
            # were costing 36s each and dominated the run.
            if n_page == 0 and not ans:
                break
            if not html:
                continue
            reached = True
            html_all += " " + html
            if sub and not contact_url and CONTACT_URL_RE.search(sub):
                contact_url = url
        text = strip_tags(html_all)
        score = bakery_score(text)

        # Chain domains: look for a page belonging to THIS shop. Ines's rule
        # is shop-page-or-nothing, so the fetch happens here, once per domain.
        shop_urls: dict = {}
        if reached and shared:
            candidates = site_urls(sess, domain)
            budget = MAX_SHOP_FETCH
            for r in rows:
                if budget <= 0:
                    break
                for cand in shop_page_candidates(candidates, r["commune"], r["code_postal"])[:1]:
                    budget -= 1
                    shop_html, _ = fetch(sess, cand)
                    time.sleep(DELAY)
                    if not shop_html:
                        continue
                    shop_text = strip_tags(shop_html)
                    # The shop page must describe ONE shop. A directory's
                    # "/marseille-13001/..." listing page would otherwise be
                    # rescued by the very check meant to rescue chains.
                    if is_directory_like(shop_text, communes)[0]:
                        stats["shop_page_rejected_directory"] += 1
                        continue
                    own_here = ownership(
                        shop_text, sirets=[r["siret"]], sirens=[r["siren"]],
                        cps=[r["code_postal"]],
                        tokens=name_tokens(f"{r['raison_sociale']} {r['hint']}"),
                        phones=phones_of.get(r["siret"], ()))
                    # `cp` alone is CIRCULAR here: we selected this URL because
                    # it carries the commune or postcode, so finding the
                    # postcode on it proves nothing. Three different bakeries
                    # in Gardanne all claimed the same Pétrin Ribeïrou shop
                    # page that way. The name or a harder identifier must
                    # agree as well.
                    if own_here in ("siret", "siren", "tel", "cp+nom") \
                            and bakery_score(shop_text) >= 1:
                        shop_urls[r["siret"]] = (cand, own_here)
                        stats["shop_page_found"] += 1

        out = []
        for r in rows:
            own = ownership(text, sirets=[r["siret"]], sirens=[r["siren"]],
                            cps=[r["code_postal"]],
                            tokens=name_tokens(f"{r['raison_sociale']} {r['hint']}"),
                            phones=phones_of.get(r["siret"], ()),
                            # Never on a shared domain: a chain domain that
                            # resembles one franchisee's name proves nothing,
                            # and that is precisely how LA MIE DU PAIN in
                            # Vitrolles would re-acquire the chain's site.
                            domain="" if shared else domain,
                            names=(r["raison_sociale"], r["hint"]))
            shop_url, shop_own = shop_urls.get(r["siret"], ("", ""))
            verdict, reason = classify(
                reached=reached, text=text, own=shop_own or own, shared=shared,
                communes=communes, has_shop_page=bool(shop_url),
                blocked=answered)
            stats[f"verdict:{verdict}"] += 1
            out.append({
                "siret": r["siret"], "siren": r["siren"],
                "raison_sociale": r["raison_sociale"], "commune": r["commune"],
                "code_postal": r["code_postal"], "domain": domain,
                "url_recorded": r["url"], "verdict": verdict, "reason": reason,
                "ownership": shop_own or own, "bakery_score": score,
                "text_len": len(text), "shop_url": shop_url,
                "contact_url": contact_url,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            })
        flush(OUT_PATH, FIELDNAMES, out)          # on disk before the next domain
        written += len(out)
        with DONE_PATH.open("a", encoding="utf-8") as f:
            f.write(domain + "\n")

        kept = sum(1 for o in out if o["verdict"] in VERDICT_SHIPS)
        log.info(f"[{i}/{len(todo)}] {domain:36.36s} v6={shipped.get(domain, 0):3d} "
                 f"rows={len(out):3d} keep={kept:3d} bakery={score} "
                 f"{out[0]['verdict']:14s} {out[0]['reason'][:24]}")

    log.info("─" * 70)
    log.info(f"written={written} verdict rows -> {OUT_PATH}")
    for k in sorted(k for k in stats if k.startswith("verdict:")):
        log.info(f"  {k:<26} {stats[k]}")
    if stats["shop_page_found"]:
        log.info(f"  shop pages found on chain domains: {stats['shop_page_found']}")
    ships = sum(v for k, v in stats.items()
                if k.startswith("verdict:") and k.split(":", 1)[1] in VERDICT_SHIPS)
    log.info(f"would ship {ships} of {written} pairs; the rest are deleted by m2_s14.")


if __name__ == "__main__":
    main()
