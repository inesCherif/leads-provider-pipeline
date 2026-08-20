"""
M2-S10 — Generate e-mail candidates and PROVE them by SMTP
===========================================================
Sam's original six-step method ended at "guess the address, then verify".
Agriculture could never run it: `staging.sites.website_domain` was populated
for 0 of 128,267 rows and 78% of the addresses we held were personal ISP
mailboxes, so there was no domain to apply a pattern to. Boulangeries are the
opposite case, and after V3 the preconditions finally exist together:

    a confirmed own-domain website   +   a dirigeant's first and last name

for roughly 950 businesses that still have no e-mail at all.

HOW A GUESS BECOMES A FACT — and why nothing else ships. A generated address
is a hypothesis. This script keeps ONLY the ones an SMTP server accepts, and
it reuses `m1_s9g_verify_emails`' core rather than copying it, because that
core carries incident fixes a copy would silently lose:

  * a catch-all domain answers 250 to EVERYTHING, so a 250 proves nothing —
    `probe_domain` probes a random mailbox first and marks the whole domain
    `risky`. Every candidate on such a domain is DISCARDED here, because on a
    catch-all we cannot tell a real mailbox from an invented one.
  * OUR failure is never the data's fault: a timeout, a block or greylisting
    resolves to `unknown`, never `invalid`. That distinction is what the
    agriculture incident cost a night to learn.
  * the >20% dead-domain abort gate stops a broken run before it writes.

Only `valid` candidates are exported, labelled `pattern/verifie`. A guess that
was merely never refuted is not a lead, it is a bounce waiting to happen.

Pattern order is measured, not invented: patterns already observed on OUR
crawled corpus are tried first (see `mine_patterns`), then the generic French
small-business shapes.

Usage:
    python scripts/m2_s10_patterns.py --pilot 20    # ~20 domains, measure
    python scripts/m2_s10_patterns.py --dry-run     # generate, do not probe
    python scripts/m2_s10_patterns.py               # full run (resumable)
"""

import argparse
import asyncio
import csv
import logging
import re
import sys
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from m2lib_contact import is_third_party_email, FREE_MAIL        # noqa: E402

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
MATCHED_PATH = CHECK_DIR / "matched.csv"
SITE_EMAILS_PATH = CHECK_DIR / "site_emails.csv"
DISCOVERED_PATH = CHECK_DIR / "discovered_sites.csv"
OUT_PATH = CHECK_DIR / "pattern_candidates.csv"
DONE_PATH = CHECK_DIR / "patterns_done.txt"

# Generic shapes, tried after any pattern actually observed in our own corpus.
# `contact@` first: measured on 328 real French bakery addresses (2026-08-20,
# OSM sample over 12 departments), `contact@` is ~47% of all custom-domain
# bakery mailboxes and `prenom.nom@` is near-absent — the B2B literature's
# ordering does not transfer to micro-businesses. The named shapes stay, but
# they only ever run after every generic.
GENERIC = ["contact", "info", "commande", "bonjour", "boulangerie"]
NAMED = ["{p}.{n}", "{p}", "{n}", "{pi}{n}", "{pi}.{n}", "{p}{n}"]

MAX_CANDIDATES_PER_DOMAIN = 6      # politeness: one connection, few RCPTs

FIELDNAMES = ["siret", "siren", "raison_sociale", "commune", "domain",
              "candidate", "pattern", "status", "tool"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s10")


def deaccent(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "", s).lower()


def read(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def flush(rows: list) -> None:
    if not rows:
        return
    new = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";",
                           quoting=csv.QUOTE_MINIMAL)
        if new:
            w.writeheader()
        w.writerows(rows)
        fh.flush()


def mine_patterns(ours_by_siret: dict) -> Counter:
    """Which shapes do the addresses we ALREADY hold actually use?

    Only addresses on the business's own domain count — a gmail address tells
    us nothing about a pattern, and a supplier's address is not ours at all.
    """
    pats = Counter()
    for r in read(SITE_EMAILS_PATH):
        email, domain = (r.get("email") or "").lower(), (r.get("domain") or "").lower()
        if not email or not domain or is_third_party_email(email, domain):
            continue
        local = email.partition("@")[0]
        biz = ours_by_siret.get(r["siret"])
        if not biz:
            continue
        p, n = deaccent(biz.get("prenom", "")), deaccent(biz.get("nom", ""))
        if local in GENERIC:
            pats[local] += 1
        elif p and n:
            for shape in NAMED:
                if local == shape.format(p=p, n=n, pi=p[:1]):
                    pats[shape] += 1
                    break
    return pats


def candidates_for(domain: str, prenom: str, nom: str, ranked: list) -> list:
    p, n = deaccent(prenom), deaccent(nom)
    out = []
    for shape in ranked:
        if shape in GENERIC:
            local = shape
        else:
            if not (p and n):
                continue
            local = shape.format(p=p, n=n, pi=p[:1])
        if not local or len(local) < 2:
            continue
        addr = f"{local}@{domain}"
        if addr not in out:
            out.append(addr)
        if len(out) >= MAX_CANDIDATES_PER_DOMAIN:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate + SMTP-verify e-mail candidates")
    ap.add_argument("--pilot", type=int, default=0, help="only N domains")
    ap.add_argument("--dry-run", action="store_true", help="generate, never probe")
    args = ap.parse_args()

    try:
        from scripts.m1_s9g_verify_emails import (
            MAX_DEAD_DOMAIN_RATE, SMTP_WORKERS, KNOWN_BLOCKERS,
            VALID_SYNTAX, resolve_mx, probe_domain)
    except ImportError as exc:
        sys.exit(f"Cannot import the verification core: {exc}\n"
                 "Needs: pip install dnspython psycopg2-binary")

    ours = read(OURS_PATH)
    if not ours:
        sys.exit(f"{OURS_PATH} not found — run scripts/m2_s2_transform.py first.")
    by_siret = {r["siret"]: r for r in ours}

    # A business is a target when we know its own-domain website and a person,
    # and it has no address yet.
    have_email = {r["siret"] for r in read(SITE_EMAILS_PATH)}
    have_email |= {r["siret"] for r in read(MATCHED_PATH) if r.get("email")}
    # An aggregator that leaked into `website` must never be pattern-expanded:
    # `contact@fr.mappy.com` is a real mailbox at a company that is not our
    # prospect, and probing it would put a stranger's address in the file.
    try:
        from m2_s8_websites import AGGREGATORS
    except ImportError:
        AGGREGATORS = ("mappy.", "pagesjaunes", "google.", "facebook.")
    site_of: dict = {}
    skipped_agg = 0
    for r in read(MATCHED_PATH) + read(DISCOVERED_PATH):
        if r.get("website") and r["siret"] not in site_of:
            d = urllib.parse.urlparse(r["website"]).netloc.lower().replace("www.", "")
            if not d or d in FREE_MAIL:
                continue
            if any(a in d for a in AGGREGATORS):
                skipped_agg += 1
                continue
            site_of[r["siret"]] = d
    if skipped_agg:
        log.info(f"{skipped_agg} aggregator domain(s) skipped — never pattern-expanded")

    done = load_done()
    targets = []
    for s, dom in site_of.items():
        if s in have_email or dom in done:
            continue
        b = by_siret.get(s)
        if not b:
            continue
        targets.append((s, dom, b))

    # One probe per DOMAIN, not per business: a chain sharing a domain must not
    # be probed five times, and the SMTP core's rule is one connection per host.
    seen_dom = set()
    uniq = []
    for s, dom, b in targets:
        if dom in seen_dom:
            continue
        seen_dom.add(dom)
        uniq.append((s, dom, b))
    if args.pilot:
        uniq = uniq[:args.pilot]

    ranked = [p for p, _ in mine_patterns(by_siret).most_common()]
    ranked += [g for g in GENERIC if g not in ranked]
    ranked += [g for g in NAMED if g not in ranked]
    log.info(f"pattern order (mined first): {ranked[:8]}")
    log.info(f"{len(site_of)} businesses with an own domain | {len(have_email)} already "
             f"have an address | {len(done)} domains done -> {len(uniq)} domains to probe")

    plan = {}
    for s, dom, b in uniq:
        cands = [c for c in candidates_for(dom, b.get("prenom", ""), b.get("nom", ""), ranked)
                 if VALID_SYNTAX.match(c)]
        if cands:
            plan[dom] = (s, b, cands)
    log.info(f"{sum(len(v[2]) for v in plan.values())} candidates over {len(plan)} domains")
    if args.dry_run:
        for dom, (s, b, c) in list(plan.items())[:10]:
            log.info(f"  {dom:<34} {c}")
        log.info("--dry-run: nothing probed, nothing written.")
        return
    if not plan:
        log.info("nothing to do.")
        return

    domains = sorted(plan)
    mx = asyncio.run(resolve_mx(domains))
    dead = sum(1 for d in domains if mx.get(d, ("unknown", []))[0] == "dead")
    rate = dead / max(1, len(domains))
    log.info(f"MX: {sum(1 for d in domains if mx.get(d,('',[]))[0]=='mail_ok')} mail_ok, "
             f"{dead} dead ({rate:.1%}), rest unknown")
    # The gate that stopped agriculture shipping 10,478 good addresses as dead.
    if rate > MAX_DEAD_DOMAIN_RATE:
        sys.exit(f"ABORT: {rate:.1%} of domains look dead (> {MAX_DEAD_DOMAIN_RATE:.0%}). "
                 "That is far more likely to be our resolver than reality. "
                 "Nothing written.")

    from concurrent.futures import ThreadPoolExecutor
    stats = Counter()
    written = valid = 0

    def work(dom: str):
        verdict, hosts = mx.get(dom, ("unknown", []))
        s, b, cands = plan[dom]
        if verdict != "mail_ok" or not hosts:
            return dom, {c: ("unknown", f"s9g:mx_{verdict}") for c in cands}
        if any(h.lower().endswith(tuple(KNOWN_BLOCKERS)) for h in hosts):
            return dom, {c: ("unknown", "s9g:known_blocker") for c in cands}
        return dom, probe_domain(dom, hosts, cands)

    with ThreadPoolExecutor(max_workers=SMTP_WORKERS) as pool:
        for dom, res in pool.map(work, domains):
            s, b, _ = plan[dom]
            rows = []
            for addr, (status, tool) in res.items():
                stats[status] += 1
                # A catch-all domain ('risky') cannot distinguish a real
                # mailbox from an invented one — so of the guesses it proves
                # nothing about, exactly ONE ships, flagged: `contact@` on
                # the business's own domain. Ines's ruling 2026-08-20 ("if it
                # exists it exists"): a bakery running catch-all on its own
                # domain almost certainly reads contact@, measured at ~47% of
                # custom-domain bakery addresses. It ships as an ACCEPTED-
                # RISK tranche (`pattern/catchall`, statut `risque`), never
                # as a verified fact, and m2_s14's ownership gates still
                # apply. Every other catch-all candidate stays discarded.
                if status == "risky" and addr == f"contact@{dom}":
                    rows.append({
                        "siret": s, "siren": b["siren"],
                        "raison_sociale": b["raison_sociale"], "commune": b["commune"],
                        "domain": dom, "candidate": addr,
                        "pattern": "contact", "status": "risky", "tool": tool,
                    })
                    continue
                if status != "valid":
                    continue
                rows.append({
                    "siret": s, "siren": b["siren"],
                    "raison_sociale": b["raison_sociale"], "commune": b["commune"],
                    "domain": dom, "candidate": addr,
                    "pattern": addr.partition("@")[0], "status": status, "tool": tool,
                })
            if rows:
                flush(rows)
                written += len(rows)
                valid += 1
                log.info(f"  VALID {dom:<32} {[r['candidate'] for r in rows]}")
            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(dom + "\n")

    log.info("─" * 62)
    log.info(f"domains probed {len(domains)} | domains yielding a PROVEN address {valid}")
    log.info(f"candidate verdicts: {dict(stats)}")
    log.info(f"written={written} -> {OUT_PATH}")
    log.info("`valid` rows are SMTP-proven. `risky` rows are the catch-all "
             "tranche — contact@ on the business's own domain only, shipped "
             "FLAGGED by m2_s14 after its ownership gates (Ines 2026-08-20).")


if __name__ == "__main__":
    main()
