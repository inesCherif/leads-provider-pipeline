"""
M2-S11 — Verify the harvested e-mails (free, no vendor)
========================================================
Reuses the verification core built in m1_s9g_verify_emails.py by IMPORTING it
— `classify_domain`, `probe_domain`, `resolve_mx`, `VALID_SYNTAX`,
`KNOWN_BLOCKERS` are DB-free and sector-agnostic, so there is no reason to
copy them and every reason not to (a second copy would drift away from the
incident fixes baked into the first).

THE RULE, unchanged and non-negotiable:

    OUR FAILURE IS NOT EVIDENCE THE MAILBOX IS BAD.

A refused connection, a DNS timeout, a greylist 4xx and a catch-all 250 all
mean "we do not know". Writing any of those as `invalid` is what marked
10,478 good agriculture addresses dead and cut that deliverable from 13,433
to 8,042 before it was caught. Verdicts here are four-state:

    valide      SMTP accepted the recipient
    invalide    SMTP explicitly rejected it, or the syntax/domain is dead
    risque      catch-all domain — accepted, but accepts everything
    non verifie we could not get an answer (blocked ISP, timeout, greylist)

`non verifie` is the honest default and is never converted to a verdict.

Input : exports/boulangerie/checkpoints/site_emails.csv (+ matched.csv emails)
Output: exports/boulangerie/checkpoints/verified_emails.csv

Usage:
    python scripts/m2_s11_verify.py
    python scripts/m2_s11_verify.py --limit 20
"""

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OUT_PATH  = CHECK_DIR / "verified_emails.csv"

FIELDNAMES = ["email", "domain", "verdict", "detail"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s11")

VERDICT_FR = {"valid": "valide", "invalid": "invalide",
              "risky": "risque", "unknown": "non verifie"}


def collect_emails() -> dict[str, set]:
    """domain -> {addresses}. Reads every checkpoint that can carry an email."""
    out: dict[str, set] = {}
    for fname, col in (("site_emails.csv", "email"), ("matched.csv", "email"),
                       ("social_emails.csv", "email"),
                       ("rdap_emails.csv", "email")):
        p = CHECK_DIR / fname
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                e = (r.get(col) or "").strip().lower()
                if "@" in e:
                    out.setdefault(e.rpartition("@")[2], set()).add(e)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify harvested e-mails, free")
    ap.add_argument("--limit", type=int, default=0, help="only N domains (pilot)")
    args = ap.parse_args()

    try:
        import asyncio
        from scripts.m1_s9g_verify_emails import (
            KNOWN_BLOCKERS, VALID_SYNTAX, classify_domain, probe_domain)
    except ImportError as e:
        sys.exit(f"Cannot import the verification core: {e}\n"
                 "Needs: pip install dnspython")

    by_domain = collect_emails()
    if not by_domain:
        sys.exit("No e-mails found — run m2_s9_emails.py / m2_s7_match.py first.")
    domains = sorted(by_domain)
    if args.limit:
        domains = domains[:args.limit]
    total = sum(len(by_domain[d]) for d in domains)
    log.info(f"{total} address(es) over {len(domains)} domain(s)")

    results: dict[str, tuple[str, str]] = {}

    # Syntax first — free, and a malformed address needs no network at all.
    for d in domains:
        for e in list(by_domain[d]):
            if not VALID_SYNTAX.match(e):
                results[e] = ("invalid", "syntaxe")

    async def classify_all():
        # classify_domain takes its own resolver so concurrency stays bounded;
        # 50 parallel lookups overwhelmed it during the agriculture incident
        # and the timeouts were believed as verdicts. 12 is the measured-safe
        # value carried over from m1_s9g.
        import dns.asyncresolver
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = 8.0
        out = {}
        sem = asyncio.Semaphore(12)

        async def one(dom):
            async with sem:
                out[dom] = await classify_domain(resolver, dom)
        await asyncio.gather(*(one(d) for d in domains))
        return out

    log.info("Resolving MX/A records…")
    dom_status = asyncio.run(classify_all())

    dead = [d for d, v in dom_status.items() if (v[0] if isinstance(v, tuple) else v) == "dead"]
    dead_rate = len(dead) / max(1, len(domains))
    log.info(f"domains: {len(domains)} | dead {len(dead)} ({dead_rate:.0%})")
    # The gate that would have caught the agriculture incident before it wrote
    # anything: a plausible reality is 7-9% dead. Far above that means OUR
    # resolver is failing, not that the internet died.
    if dead_rate > 0.20:
        sys.exit(f"ABORT: {dead_rate:.0%} of domains look dead (ceiling 20%). "
                 "That is almost certainly our DNS failing, not theirs. "
                 "Nothing was written.")

    probed = skipped = 0
    for d in domains:
        addrs = sorted(a for a in by_domain[d] if a not in results)
        if not addrs:
            continue
        st = dom_status.get(d)
        state = st[0] if isinstance(st, tuple) else st
        mx = (st[1] if isinstance(st, tuple) and len(st) > 1 else []) or []
        if state == "dead":
            for a in addrs:
                results[a] = ("invalid", "domaine sans MX ni A")
            continue
        if d in KNOWN_BLOCKERS:
            # Orange/SFR/Outlook/Yahoo refuse probes from a residential IP.
            # That is our limitation; it says nothing about the mailbox.
            for a in addrs:
                results[a] = ("unknown", "FAI refuse le probe (IP residentielle)")
            skipped += len(addrs)
            continue
        try:
            verdicts = probe_domain(d, mx, addrs)
            for a in addrs:
                v = verdicts.get(a)
                results[a] = v if v else ("unknown", "pas de reponse")
            probed += len(addrs)
        except Exception as exc:
            for a in addrs:
                results[a] = ("unknown", f"probe error: {type(exc).__name__}")
        log.info(f"  {d:<38.38} {len(addrs)} addr -> "
                 f"{Counter(results[a][0] for a in addrs).most_common()}")

    rows = [{"email": e, "domain": e.rpartition("@")[2],
             "verdict": VERDICT_FR.get(v[0], v[0]), "detail": v[1]}
            for e, v in sorted(results.items())]
    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    c = Counter(r["verdict"] for r in rows)
    log.info("─" * 62)
    log.info(f"written={len(rows)} -> {OUT_PATH}")
    for k in ("valide", "invalide", "risque", "non verifie"):
        log.info(f"  {k:<12} {c.get(k, 0):>5}")
    log.info(f"probed {probed}, skipped {skipped} on blocker ISPs. "
             "'non verifie' is left as-is on purpose: a re-run from a VPS with "
             "clean reverse-DNS would resolve most of them.")


if __name__ == "__main__":
    main()
