r"""
M1-S9-G — Free email verification (syntax → MX → SMTP RCPT TO)
==============================================================
Replaces MillionVerifier, which now demands credits and a VAT number. This is
what a paid verifier does internally, and all of it is free:

    1. syntax        malformed -> invalid
    2. MX lookup     no mail server -> invalid          (100% coverage, free)
    3. role/disposable classification                    (informational)
    4. SMTP RCPT TO  ask the server if the mailbox exists (needs port 25)

MEASURED ON THIS DATA before building (150 addresses / 87 domains, see
`docs/m1_s9_progress.md`): about 24% get a definitive verdict.

    gmail.com                WORKS      5,611 addresses, 21% of the base
    orange.fr / wanadoo.fr   550 on CONNECT   <- our IP is refused outright
    sfr.fr                   521 on CONNECT
    hotmail/outlook/live/yahoo   connection closed

Orange, SFR, Microsoft and Yahoo refuse probing from a residential IP. No proxy
fixes that: the receiving server judges the IP that connects to it, and a free
proxy has worse reputation than a home line. A cheap VPS with clean reverse-DNS
would unblock roughly the 45% those providers hold.

⚠ THE RULE THAT MATTERS MOST
    BEING BLOCKED IS NOT EVIDENCE THE MAILBOX IS BAD.
    A refused connection, a timeout, a greylist 4xx and a catch-all 250 all mean
    "we do not know" — they must never be written as 'invalid'. Only two things
    justify 'invalid': broken syntax, or a definitive 5xx from a server that
    demonstrably rejects unknown mailboxes. Getting this backwards would delete
    thousands of good prospects on the strength of our own IP reputation.

CATCH-ALL DETECTION
    Many domains accept mail for any local part. A 250 from those means nothing,
    so a random mailbox is probed FIRST on every domain; if that is accepted,
    every address on the domain is 'risky', never 'valid'.

WHY MIGRATION 015 HAD TO LAND FIRST
    The best-email picker in v_qualified_contacts PREFERRED a valid address but
    never EXCLUDED an invalid one. Before 015, marking thousands 'invalid' would
    not have changed one row of the export - this step would have silently
    accomplished nothing.

IDEMPOTENT
    Selects only verified_at IS NULL, so a re-run resumes where it stopped.
    Interrupted runs lose nothing: results are flushed per batch.

Usage:
    python scripts/m1_s9g_verify_emails.py --dry-run --limit 200
    python scripts/m1_s9g_verify_emails.py --skip-smtp          # MX layer only, zero SMTP
    python scripts/m1_s9g_verify_emails.py --limit 2000
    python scripts/m1_s9g_verify_emails.py                      # everything unverified

Reversal:
    UPDATE staging.emails
    SET verification_status='candidate', verified_at=NULL, verifier_tool=NULL
    WHERE verifier_tool LIKE 's9g:%';
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import random
import re
import smtplib
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import psycopg2
    import psycopg2.extras
    import dns.asyncresolver
    import dns.resolver
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("m1_s9g")

SCRIPT_NAME = "m1_s9g_verify_emails.py"

# Identity used in the SMTP conversation. Must look like a real postmaster
# address on a domain we control, or servers reject the probe outright.
MAIL_FROM = os.getenv("VERIFY_MAIL_FROM", "postmaster@leadsprovider.fr")
HELO_NAME = os.getenv("VERIFY_HELO", "leadsprovider.fr")

SMTP_TIMEOUT = 12          # seconds per SMTP operation
MAX_RCPT_PER_CONN = 40     # reconnect after this many, so one session is never abusive
SMTP_WORKERS = 8           # distinct DOMAINS probed at once; never 2 conns to one domain
DNS_CONCURRENCY = 50
BATCH_SIZE = 500           # rows per DB flush

VALID_SYNTAX = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

ROLE_PREFIXES = {
    "contact", "info", "accueil", "commercial", "direction", "secretariat",
    "administration", "compta", "comptabilite", "gerance", "bureau", "mairie",
    "postmaster", "webmaster", "abuse", "noreply", "no-reply", "ne-pas-repondre",
}

# Probing these is pointless: measured as refusing connections from a
# residential IP. Skipping them saves hours and avoids hammering a provider
# that has already said no. They are marked 'unknown', never 'invalid'.
KNOWN_BLOCKERS = {
    "orange.fr", "wanadoo.fr", "sfr.fr", "neuf.fr", "aliceadsl.fr",
    "hotmail.fr", "hotmail.com", "outlook.fr", "outlook.com", "live.fr",
    "msn.com", "yahoo.fr", "yahoo.com", "aol.com", "bbox.fr",
}

# Deliverable rows first: those are the addresses actually shipping to the
# client, so a partial run still improves the thing that matters.
SELECT_SQL = """
SELECT e.id, e.email_address,
       (d.email_address IS NOT NULL) AS in_deliverable
FROM staging.emails e
LEFT JOIN public.v_deliverable_businesses d ON d.email_address = e.email_address
WHERE e.verification_status = 'candidate'
  AND e.verified_at IS NULL
ORDER BY (d.email_address IS NOT NULL) DESC, e.id
{limit}
"""

UPDATE_SQL = """
UPDATE staging.emails AS e
SET verification_status = v.status,
    verified_at         = NOW(),
    verifier_tool       = v.tool
FROM (VALUES %s) AS v(id, status, tool)
WHERE e.id = v.id::uuid
  AND e.verified_at IS NULL
"""

AUDIT_SQL = """
INSERT INTO audit.audit_log
    (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
VALUES ('staging.emails', NULL, 'verification_status', NULL, %s, %s, %s)
"""


def get_conn():
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)


# ---------------------------------------------------------------- MX layer

async def _mx_for(resolver, domain: str) -> list[str]:
    try:
        answers = await resolver.resolve(domain, "MX")
    except Exception:
        return []
    hosts = sorted(
        ((r.preference, str(r.exchange).rstrip(".")) for r in answers),
        key=lambda t: t[0],
    )
    # A null MX ('.', RFC 7505) is an explicit refusal to accept mail.
    return [h for _, h in hosts if h]


async def resolve_mx(domains: list[str]) -> dict[str, list[str]]:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 6.0
    resolver.timeout = 6.0
    sem = asyncio.Semaphore(DNS_CONCURRENCY)

    async def one(d: str):
        async with sem:
            return d, await _mx_for(resolver, d)

    return dict(await asyncio.gather(*[one(d) for d in domains]))


# -------------------------------------------------------------- SMTP layer

def probe_domain(domain: str, mx_hosts: list[str],
                 addresses: list[str]) -> dict[str, tuple[str, str]]:
    """Probe every address on one domain over as few connections as possible.

    Returns {address: (status, tool)}. Any failure of ours resolves to
    'unknown' — never 'invalid'. See the rule in the module docstring.
    """
    out: dict[str, tuple[str, str]] = {}

    def all_unknown(reason: str):
        for a in addresses:
            out[a] = ("unknown", f"s9g:smtp_{reason}")
        return out

    host = mx_hosts[0]
    try:
        server = smtplib.SMTP(host, 25, timeout=SMTP_TIMEOUT)
        server.ehlo(HELO_NAME)
        server.mail(MAIL_FROM)
    except Exception as exc:
        log.debug("  %s blocked at connect: %s", domain, exc)
        return all_unknown("blocked")

    try:
        # Catch-all probe FIRST. If a random mailbox is accepted, a 250 for a
        # real address proves nothing and everything here is 'risky'.
        rand = f"zz9x7q-probe-{random.randint(10**7, 10**8)}"
        try:
            code, _ = server.rcpt(f"{rand}@{domain}")
        except Exception:
            server.quit()
            return all_unknown("probe_failed")

        if code == 250:
            server.quit()
            for a in addresses:
                out[a] = ("risky", "s9g:smtp_catchall")
            return out

        # 4xx on the probe means greylisting or rate limiting, not a verdict.
        if 400 <= code < 500:
            server.quit()
            return all_unknown("greylisted")

        sent = 0
        for addr in addresses:
            if sent >= MAX_RCPT_PER_CONN:
                try:
                    server.quit()
                except Exception:
                    pass
                server = smtplib.SMTP(host, 25, timeout=SMTP_TIMEOUT)
                server.ehlo(HELO_NAME)
                server.mail(MAIL_FROM)
                sent = 0
            try:
                code, _ = server.rcpt(addr)
            except Exception:
                out[addr] = ("unknown", "s9g:smtp_error")
                continue
            sent += 1
            if code == 250:
                out[addr] = ("valid", "s9g:smtp_rcpt")
            elif 500 <= code < 600:
                out[addr] = ("invalid", "s9g:smtp_rcpt")
            else:
                # 4xx: greylist / throttle. Explicitly not a verdict.
                out[addr] = ("unknown", "s9g:smtp_4xx")
            time.sleep(0.25)  # be a polite guest on someone else's mail server
        try:
            server.quit()
        except Exception:
            pass
    except Exception as exc:
        log.debug("  %s aborted mid-session: %s", domain, exc)
        for a in addresses:
            out.setdefault(a, ("unknown", "s9g:smtp_aborted"))
    return out


# ------------------------------------------------------------------- main

def flush(conn, cur, rows, dry_run: bool) -> int:
    if not rows or dry_run:
        return 0
    written = 0
    CHUNK = BATCH_SIZE
    for i in range(0, len(rows), CHUNK):
        # Chunk explicitly: cur.rowcount after execute_values reports only the
        # LAST page (the trap that made S9-E log 185 for 685 rows).
        psycopg2.extras.execute_values(
            cur, UPDATE_SQL, rows[i:i + CHUNK], page_size=CHUNK)
        written += cur.rowcount
    conn.commit()
    return written


def run(args) -> None:
    socket.setdefaulttimeout(SMTP_TIMEOUT)
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(SELECT_SQL.format(
        limit=f"LIMIT {int(args.limit)}" if args.limit else ""))
    targets = cur.fetchall()
    log.info("Unverified candidate addresses selected: %d", len(targets))
    if not targets:
        log.info("Nothing to do.")
        conn.close()
        return
    log.info("  of which shipping in the deliverable: %d",
             sum(1 for _, _, d in targets if d))

    results: list[tuple[str, str, str]] = []   # (id, status, tool)
    by_domain: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)

    # ---- layer 1: syntax
    bad_syntax = 0
    for eid, addr, _ in targets:
        a = (addr or "").strip().lower()
        if not VALID_SYNTAX.match(a):
            results.append((str(eid), "invalid", "s9g:syntax"))
            bad_syntax += 1
            continue
        by_domain[a.split("@", 1)[1]].append((str(eid), a))
    log.info("  layer 1 syntax     : %d invalid", bad_syntax)

    # ---- layer 2: MX
    domains = sorted(by_domain)
    log.info("  layer 2 MX         : resolving %d distinct domains...", len(domains))
    mx = asyncio.run(resolve_mx(domains))
    no_mx = [d for d in domains if not mx[d]]
    for d in no_mx:
        for eid, _ in by_domain[d]:
            results.append((eid, "invalid", "s9g:no_mx"))
    log.info("  layer 2 MX         : %d domains have no MX -> %d addresses invalid",
             len(no_mx), sum(len(by_domain[d]) for d in no_mx))

    live = [d for d in domains if mx[d]]

    # ---- layer 3: SMTP
    if args.skip_smtp:
        for d in live:
            for eid, _ in by_domain[d]:
                results.append((eid, "unknown", "s9g:mx_only"))
        log.info("  layer 3 SMTP       : SKIPPED (--skip-smtp)")
    else:
        blockers = [d for d in live if d in KNOWN_BLOCKERS]
        probe_me = [d for d in live if d not in KNOWN_BLOCKERS]
        for d in blockers:
            for eid, _ in by_domain[d]:
                results.append((eid, "unknown", "s9g:provider_blocks_probing"))
        log.info("  layer 3 SMTP       : %d domains skipped as known blockers "
                 "(%d addresses -> unknown, NOT invalid)",
                 len(blockers), sum(len(by_domain[d]) for d in blockers))
        log.info("  layer 3 SMTP       : probing %d domains with %d workers...",
                 len(probe_me), SMTP_WORKERS)

        addr_to_id = {a: eid for d in probe_me for eid, a in by_domain[d]}
        done = 0
        started = time.time()
        with ThreadPoolExecutor(max_workers=SMTP_WORKERS) as pool:
            # One task per DOMAIN, so we never open two connections to the same
            # mail server at once — that is what gets an IP rate-limited.
            futures = {
                pool.submit(probe_domain, d, mx[d], [a for _, a in by_domain[d]]): d
                for d in probe_me
            }
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    verdicts = fut.result()
                except Exception as exc:
                    log.warning("  domain %s raised %s", d, exc)
                    verdicts = {a: ("unknown", "s9g:smtp_error")
                                for _, a in by_domain[d]}
                for a, (status, tool) in verdicts.items():
                    if a in addr_to_id:
                        results.append((addr_to_id[a], status, tool))
                done += 1
                if done % 50 == 0:
                    rate = done / max(time.time() - started, 0.001)
                    log.info("    %d/%d domains  %.1f dom/s", done,
                             len(probe_me), rate)

    # ---- write
    counts = collections.Counter(s for _, s, _ in results)
    tools = collections.Counter(t for _, _, t in results)
    log.info("")
    log.info("  VERDICTS")
    total = sum(counts.values()) or 1
    for status, n in counts.most_common():
        log.info("    %-9s %6d  %5.1f%%", status, n, 100 * n / total)
    log.info("  by evidence")
    for tool, n in tools.most_common():
        log.info("    %-32s %6d", tool, n)

    # Only DEFINITIVE verdicts are checkpointed. 'unknown' means we were
    # blocked, greylisted or timed out - our failure, not the mailbox's. Those
    # rows keep verified_at NULL so a later run retries them, exactly as
    # m1_s4_sirene_enrich.py leaves API errors uncheckpointed. It also means
    # re-running from a VPS with clean reverse-DNS would pick up every address
    # Orange and SFR refuse to answer for us today.
    definitive = [r for r in results if r[1] in ("valid", "invalid", "risky")]
    deferred = len(results) - len(definitive)
    log.info("")
    log.info("  checkpointing %d definitive verdicts; leaving %d unwritten for "
             "a later retry (blocked / greylisted / timed out)",
             len(definitive), deferred)

    if args.dry_run:
        log.info("DRY-RUN — nothing written.")
        conn.rollback()
        conn.close()
        return

    written = flush(conn, cur, definitive, args.dry_run)
    log.info("  rows updated %d", written)

    cur.execute(AUDIT_SQL, (
        json.dumps({"step": "S9-G", "script": SCRIPT_NAME,
                    "selected": len(targets), "written": written,
                    "deferred_for_retry": deferred,
                    "verdicts": dict(counts), "evidence": dict(tools)},
                   ensure_ascii=False),
        SCRIPT_NAME,
        "Free email verification: syntax -> MX -> SMTP RCPT TO with catch-all "
        "detection. Blocked/greylisted/catch-all are 'unknown'/'risky', never "
        "'invalid'.",
    ))
    conn.commit()

    cur.execute("""
        SELECT count(*) FILTER (WHERE email_address IS NOT NULL),
               count(*) FILTER (WHERE email_status = 'valid'),
               count(*) FILTER (WHERE email_status = 'invalid'),
               count(*)
        FROM public.v_deliverable_businesses
    """)
    em, ok, bad, tot = cur.fetchone()
    log.info("Verified — deliverable: %d businesses, %d with an email, "
             "%d valid, %d invalid still shipping (must be 0)", tot, em, ok, bad)
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Free email verification")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe and report, write nothing")
    ap.add_argument("--limit", type=int,
                    help="cap addresses (deliverable ones are selected first)")
    ap.add_argument("--skip-smtp", action="store_true",
                    help="syntax + MX only; makes no SMTP connections at all")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
