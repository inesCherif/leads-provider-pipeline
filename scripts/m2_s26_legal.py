"""
M2-S26 — Mentions légales pass: the e-mail French law puts on every site
=========================================================================
LCEN (art. 1-1 since the 2024 SREN rewrite) obliges every professional site
to print a contact e-mail, and the natural place is the mentions-légales /
legal page. m2_s9 already crawls those paths — but it DROPS any address not
on the crawled domain (is_third_party_email), and small businesses routinely
put a sister company's or their personal mailbox there. Verified live: a
Marseille bakery's legal page carries its address on a DIFFERENT domain than
the site. This pass re-reads only the legal pages of validated sites whose
business still has no e-mail, and keeps an off-domain address only with
EVIDENCE:

  * `nom`     — the address names the business (email_belongs_to);
  * `soeur`   — the mailbox domain's core matches the site's own core
                (contact@atelierolivier13.fr on atelier-olivier13.fr);
  * `boite`   — a consumer mailbox (gmail/orange/…) printed on a page whose
                (siret, domain) pair m2_s21 judged `valide`: the page is
                PROVEN the business's own, and a platform or agency never
                signs its mentions légales with a gmail.

Anything else on the page — the web agency that built the site, the
platform hosting it (taplink, lacarte.menu print their OWN mentions) — has
no evidence and never ships. That is the V6 mapquest rule, applied here.

Output : exports/boulangerie/checkpoints/legal_emails.csv
Resume : exports/boulangerie/checkpoints/legal_done.txt (one domain per line)

Usage:
    python scripts/m2_s26_legal.py --selftest
    python scripts/m2_s26_legal.py --pilot 10
    python scripts/m2_s26_legal.py
"""

import argparse
import csv
import gzip
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2_s9_emails import extract_emails                    # noqa: E402
from m2lib_validate import email_belongs_to                # noqa: E402
from m2lib_contact import FREE_MAIL                        # noqa: E402
from m2_s24_rdap import dom_core                           # noqa: E402

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
VERDICTS_PATH = CHECK_DIR / "site_verdicts.csv"
OURS_PATH = CHECK_DIR / "etablissements.csv"
OUT_PATH = CHECK_DIR / "legal_emails.csv"
DONE_PATH = CHECK_DIR / "legal_done.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DELAY_S = 0.4
SHIPPABLE = {"valide", "non_verifiable"}
LEGAL_HREF = re.compile(
    r'href="([^"]*(?:mentions?[-_ ]?l[eé]gales?|/legal|cgv|cgu|'
    r'politique[-_]de[-_]confidentialite)[^"]*)"', re.IGNORECASE)
COMMON_PATHS = ("/mentions-legales", "/mentions-legales/", "/mentions_legales",
                "/legal", "/mentions")

FIELDNAMES = ["siret", "siren", "raison_sociale", "domain", "page",
              "email", "evidence", "pair_verdict"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s26")


def get(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except Exception:
        return ""


def keep_reason(email: str, site_domain: str, pair_verdict: str, names,
                commune: str = "") -> str:
    """Evidence that an address on this LEGAL page is the business's — or ''.

    The TOWN's name is not name evidence. The pilot kept
    `contact@vitrinesvenelles.org` (the shops association of Venelles) for a
    Venelles bakery because the commune's name sat in both — the town-hall
    trap in association form. Commune tokens are stripped from the names
    before the test, at the cost of refusing a shop genuinely named after
    its town; conservative loses less than a stranger's mailbox shipped.
    """
    dom = email.partition("@")[2]
    commune_toks = {t for t in re.sub(r"[^A-Za-z0-9 ]", " ", commune or "")
                    .upper().split() if len(t) > 3}
    stripped = []
    for n in names:
        words = [w for w in (n or "").split()
                 if w.upper().strip(",.'-") not in commune_toks]
        stripped.append(" ".join(words))
    if email_belongs_to(email, *stripped):
        return "nom"
    if dom_core(dom) and dom_core(dom) == dom_core(site_domain):
        return "soeur"
    if dom in FREE_MAIL and pair_verdict == "valide":
        return "boite"
    return ""


def selftest() -> None:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}: {got!r}"
              + ("" if good else f" (wanted {want!r})"))

    names = ("BOULANGERIE AIXOISE", "")
    check("named address kept",
          keep_reason("boulangerie.aixoise@gmail.com", "boulangerieaixoise.fr",
                      "valide", names), "nom")
    check("town's name is NOT name evidence (the Venelles association)",
          keep_reason("contact@vitrinesvenelles.org", "aaevenelles.org",
                      "valide", ("SNC MAURIN VENELLES", ""), commune="VENELLES"),
          "")
    check("sister-spelling domain kept",
          keep_reason("contact@atelierolivier13.fr", "atelier-olivier13.fr",
                      "valide", ("X", "")), "soeur")
    check("gmail on a PROVEN-own page kept",
          keep_reason("karim13000@gmail.com", "monfournil.fr", "valide",
                      ("Y", "")), "boite")
    check("gmail on a merely non_verifiable page refused",
          keep_reason("someone@gmail.com", "monfournil.fr", "non_verifiable",
                      ("Y", "")), "")
    check("web agency's mailbox refused",
          keep_reason("contact@wamimy.fr", "cosmos-patisserie.fr", "valide",
                      ("COSMOS", "")), "")
    check("platform's own mentions mailbox refused",
          keep_reason("support@taplink.ws", "lesdelices-patisserie.taplink.ws",
                      "valide", ("LES DELICES", "")), "")
    check("legal link regex finds accented spelling",
          bool(LEGAL_HREF.search('<a href="/fr/mentions-l%C3%A9gales">x</a>')
               or LEGAL_HREF.search('<a href="/mentions-legales.php">x</a>')),
          True)
    print("selftest:", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Mentions-légales e-mail pass")
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()

    with OURS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        ours = {r["siret"]: r for r in csv.DictReader(fh, delimiter=";")}

    def read(name):
        p = CHECK_DIR / name
        if not p.exists():
            return []
        with p.open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    have_email = {r["siret"] for r in read("site_emails.csv")}
    have_email |= {r["siret"] for r in read("matched.csv") if r.get("email")}
    have_email |= {r["siret"] for r in read("social_emails.csv") if r.get("email")}
    have_email |= {r["siret"] for r in read("rdap_emails.csv")}

    # Targets: shippable (siret, domain) pairs whose business has no address.
    targets: dict[str, list] = {}
    for r in read("site_verdicts.csv"):
        if r.get("verdict") in SHIPPABLE and r["siret"] in ours \
                and r["siret"] not in have_email:
            targets.setdefault(r["domain"].lower(), []).append(
                (r["siret"], r["verdict"]))

    done = set()
    if DONE_PATH.exists():
        done = {l.strip() for l in DONE_PATH.read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = sorted(d for d in targets if d not in done)
    log.info(f"target domains (validated site, business without e-mail): "
             f"{len(targets)} | done: {len(done & set(targets))} | to read: {len(todo)}")
    if args.pilot:
        todo = todo[:args.pilot]

    stats = Counter()
    new_file = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as out_fh, \
         DONE_PATH.open("a", encoding="utf-8") as done_fh:
        w = csv.DictWriter(out_fh, fieldnames=FIELDNAMES, delimiter=";",
                           quoting=csv.QUOTE_MINIMAL)
        if new_file:
            w.writeheader()
        for i, d in enumerate(todo, 1):
            home = get(f"https://{d}/") or get(f"http://{d}/")
            pages = []
            if home:
                links = {urllib.parse.urljoin(f"https://{d}/", h)
                         for h in LEGAL_HREF.findall(home)}
                pages = [u for u in links
                         if urllib.parse.urlparse(u).netloc.lower()
                         .replace("www.", "") == d][:3]
            if not pages:
                pages = [f"https://{d}{p}" for p in COMMON_PATHS[:3]]
            found: set = set()
            for u in pages:
                html = get(u)
                if html:
                    found |= extract_emails(html)
                time.sleep(DELAY_S)
            wrote = 0
            for siret, verdict in targets[d]:
                b = ours[siret]
                names = (b.get("raison_sociale", ""), b.get("enseigne", ""))
                for e in sorted(found):
                    why = keep_reason(e, d, verdict, names,
                                      commune=b.get("commune", ""))
                    if not why:
                        stats["no evidence (refused)"] += 1
                        continue
                    w.writerow({"siret": siret, "siren": b["siren"],
                                "raison_sociale": b["raison_sociale"],
                                "domain": d, "page": pages[0][:120], "email": e,
                                "evidence": why, "pair_verdict": verdict})
                    out_fh.flush()
                    wrote += 1
                    stats[f"kept ({why})"] += 1
                    log.info(f"[{i}/{len(todo)}] {d} -> {e} ({why})")
            if not found:
                stats["no address on legal pages"] += 1
            done_fh.write(d + "\n"); done_fh.flush()
            time.sleep(DELAY_S)

    log.info("─" * 62)
    for k, v in stats.most_common():
        log.info(f"  {k:<36} {v:>5}")
    log.info(f"output -> {OUT_PATH}")
    log.info("Next: python scripts/m2_s11_verify.py  (then m2_s14 — H17 order)")


if __name__ == "__main__":
    main()
