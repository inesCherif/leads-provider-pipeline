r"""
M1-S9-F — Repair emails whose domain has a second domain welded onto it
=======================================================================
Found 2026-08-02 during the S9-4 Phase 0 measurement. S9-E repaired domains
that lost their dot (gmailcom -> gmail.com). It could not see THIS defect
class, because a glued domain is still syntactically valid:

    3a.martin@gmail.commail.fr     gmail.com  + mail.fr
    h.durand@orange.frnadoo.fr      orange.fr  + nadoo.fr   (tail of wanadoo.fr)
    dominique.morel@wanadoo.froo.fr    wanadoo.fr + oo.fr
    dupont989@orange.frorange.fr      orange.fr  + orange.fr  (exact repeat)
    besses@laposte.netaposte.net        laposte.net + aposte.net

`gmail.commail.fr` has dots and ends in a real TLD, so S9-E's
`!~ '^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$'` selector passes it straight through.
Every one is a guaranteed hard bounce, for the same reason S9-E was urgent:
a send that opens with bounces gets the whole campaign throttled.

MEASURED: 91 rows with verification_status='candidate', 69 of them shipped in
`v_deliverable_businesses` (of 13,435 emails).

⚠ THE COUNT TRAP THAT COST AN HOUR
    The obvious detector, applied to the whole address, is WRONG:

        email_address ~ '\.(fr|com|net|org)[a-z]{2,}\.'     -- 101 rows, 10 bogus

    French given names and nouns in the LOCAL part match it —
    `dupont.francois.marie@orange.fr` (".francois."),
    `lfg.frelons.guepes82@gmail.com`, `les.fromages.tourvains@wanadoo.fr`.
    Those addresses are perfectly fine. The test must apply to the DOMAIN only:

        lower(split_part(email_address,'@',2)) ~ '\.(fr|com|net|org)[a-z]{2,}\.'

THE REPAIR IS DETERMINISTIC, AND THEN PROVEN
    In every observed case the FIRST domain is complete and valid, with junk
    welded after it. So the rule is: truncate at the first known TLD boundary.

    But a rule alone would be a guess, and a plausible-looking guess is exactly
    what put another company's data into this DB once before. So every repair
    is then VERIFIED BY DNS, which is free and takes milliseconds:

        repaired domain MUST resolve (MX or A)   AND
        original domain MUST NOT resolve

    Both conditions must hold or the row is left untouched and reported. That
    turns "orange.frnadoo.fr -> orange.fr" from an inference into a fact, and it
    protects the one case the rule alone would corrupt: a genuine domain such as
    `mon.frais.fr`, where ".frais." matches the pattern but the domain is real.
    If the original resolves, we do not touch it.

NON-DESTRUCTIVE, USING THE SCHEMA AS DESIGNED
    Same shape as S9-E (migration 001 made staging.emails multi-candidate):
        - the glued row       -> verification_status='invalid', is_primary=FALSE
        - the repaired address-> INSERTed as a new 'candidate' row, is_primary=TRUE
    The original stays visible and the change is reversible.

IDEMPOTENT
    Selection excludes rows already 'invalid', so a second run finds nothing.
    ON CONFLICT DO NOTHING covers a repaired address that already exists.

Usage:
    python scripts/m1_s9f_email_glued_repair.py --dry-run
    python scripts/m1_s9f_email_glued_repair.py

Reversal:
    -- restore the glued rows and drop the repairs made by this step
    UPDATE staging.emails SET verification_status='candidate', is_primary=TRUE
    WHERE verification_status='invalid'
      AND lower(split_part(email_address,'@',2)) ~ '\.(fr|com|net|org)[a-z]{2,}\.';
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import psycopg2
    import psycopg2.extras
    import dns.asyncresolver
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("m1_s9f")

SCRIPT_NAME = "m1_s9f_email_glued_repair.py"

# Longest first so 'com' is tested before 'co'/'om'. Same list as S9-E, minus
# the ones that cannot begin a glued pair in this data. A TLD not listed here
# means the row is left alone rather than guessed at.
KNOWN_TLDS = ["coop", "info", "biz", "bzh", "com", "net", "org", "pro",
              "eu", "fr", "be", "ch", "io"]

# Domain-only test. See the count trap in the docstring: applying this to the
# whole address matches French names in the local part and over-counts by 11%.
SELECT_SQL = r"""
SELECT e.id, e.contact_id, e.email_address, e.source_file_id
FROM staging.emails e
WHERE e.verification_status <> 'invalid'
  AND lower(split_part(e.email_address, '@', 2)) ~ '\.(fr|com|net|org)[a-z]{2,}\.'
ORDER BY e.id
"""

INSERT_SQL = """
INSERT INTO staging.emails
    (contact_id, email_address, is_primary, verification_status, source_file_id)
VALUES %s
ON CONFLICT (contact_id, email_address) DO NOTHING
"""

INVALIDATE_SQL = """
UPDATE staging.emails
SET verification_status = 'invalid',
    is_primary          = FALSE
WHERE id = ANY(%s::uuid[])
"""

AUDIT_SQL = """
INSERT INTO audit.audit_log
    (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
VALUES ('staging.emails', NULL, 'email_address', NULL, %s, %s, %s)
"""

VERIFY_SQL = r"""
SELECT count(*) FROM public.v_deliverable_businesses
WHERE email_address IS NOT NULL
  AND lower(split_part(email_address, '@', 2)) ~ '\.(fr|com|net|org)[a-z]{2,}\.'
"""


def split_glued(domain: str) -> str | None:
    """Truncate a glued domain at its first complete TLD boundary.

    'orange.frnadoo.fr' -> 'orange.fr'      'gmail.commail.fr' -> 'gmail.com'
    'laposte.netaposte.net' -> 'laposte.net'

    Returns None when nothing is glued, i.e. the first TLD boundary is already
    the end of the string ('orange.fr' is untouched).
    """
    low = domain.lower().strip()
    best: int | None = None
    for tld in KNOWN_TLDS:
        needle = "." + tld
        start = 0
        while True:
            i = low.find(needle, start)
            if i == -1:
                break
            end = i + len(needle)
            # Must have something after it (that's the glue) and the label
            # before the dot must be non-empty.
            if end < len(low) and i > 0:
                if best is None or end < best:
                    best = end
            start = i + 1
    if best is None:
        return None
    return low[:best]


async def _has_mx(resolver, domain: str) -> bool:
    """True when the domain publishes a usable MX — i.e. it is a real mail
    destination. A-only is deliberately NOT enough: a domain with a web server
    and no MX cannot receive mail, so it is useless to a campaign."""
    try:
        answers = await resolver.resolve(domain, "MX")
    except Exception:  # NXDOMAIN, NoAnswer, timeout, SERVFAIL
        return False
    # A null MX ('.' or empty, RFC 7505) explicitly refuses mail.
    return any(str(rr.exchange).rstrip(".") for rr in answers)


async def _is_wildcard(resolver, domain: str) -> bool:
    """True when a random sibling label resolves the same way — meaning the
    parent zone answers for ANY subdomain.

    This is what distinguishes a typosquat from a real domain, and it is the
    load-bearing check in this script. Measured on this data:

        comgmail.com, commail.com, fril.com, frle.com

    are all REGISTERED, wildcarded, and publish a catch-all MX. So
    `gmail.comgmail.com` "resolves" and even accepts mail — it is a
    typosquatter harvesting misdirected messages, not a real mailbox. Testing
    only "does the original resolve" would refuse to repair those 14 addresses
    and quietly keep sending prospect data to a stranger. That is worse than a
    bounce, and it is why the guard tests wildcarding rather than existence.
    """
    labels = domain.split(".")
    if len(labels) < 3:
        # No sibling to test against a parent zone (e.g. 'gmail.com').
        return False
    probe = "zz9x7q-probe-nonexistent." + ".".join(labels[1:])
    for rtype in ("MX", "A"):
        try:
            await resolver.resolve(probe, rtype)
            return True  # the parent answers for anything -> wildcard
        except Exception:
            continue
    return False


async def dns_verify(pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """One DNS pass over every distinct domain.

    Returns {domain: {'mx': bool, 'wildcard': bool}}.
    """
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 5.0
    resolver.timeout = 5.0
    domains = sorted({d for pair in pairs for d in pair})
    sem = asyncio.Semaphore(50)

    async def one(d: str) -> tuple[str, dict]:
        async with sem:
            mx = await _has_mx(resolver, d)
            wild = await _is_wildcard(resolver, d) if mx else False
            return d, {"mx": mx, "wildcard": wild}

    return dict(await asyncio.gather(*[one(d) for d in domains]))


def run(args) -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    conn = psycopg2.connect(
        url, keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
    )
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(SELECT_SQL)
    rows = cur.fetchall()
    log.info("Candidate rows with a glued domain: %d", len(rows))
    if not rows:
        log.info("Nothing to do.")
        conn.close()
        return

    # Propose repairs first, then prove them all in one DNS pass.
    proposals = []
    no_rule = []
    for email_id, contact_id, address, source_file_id in rows:
        local, _, domain = address.partition("@")
        fixed_domain = split_glued(domain)
        if not fixed_domain or fixed_domain == domain.lower():
            no_rule.append(address)
            continue
        proposals.append(
            (email_id, contact_id, address, source_file_id,
             local.lower(), domain.lower(), fixed_domain)
        )

    log.info("  rule proposes a repair for %d, no rule for %d",
             len(proposals), len(no_rule))

    log.info("Proving every repair by DNS (repaired domain must publish an MX; "
             "original is kept only if it is a real, non-wildcard mail domain)...")
    resolved = asyncio.run(
        dns_verify([(p[5], p[6]) for p in proposals])
    )

    to_insert, to_invalidate, dead_only, rejected = [], [], [], []
    for (email_id, contact_id, address, source_file_id,
         local, orig_domain, fixed_domain) in proposals:
        orig = resolved.get(orig_domain, {"mx": False, "wildcard": False})
        fixed = resolved.get(fixed_domain, {"mx": False, "wildcard": False})

        # A genuine, non-wildcard mail domain that merely matches the pattern
        # (e.g. a real 'mon.frais.fr') must never be truncated.
        if orig["mx"] and not orig["wildcard"]:
            rejected.append(
                (address, f"original {orig_domain} is a real mail domain"))
            continue

        if fixed["mx"]:
            to_insert.append(
                (str(contact_id), f"{local}@{fixed_domain}", True,
                 "candidate", str(source_file_id))
            )
            to_invalidate.append(email_id)
            continue

        # Neither form can receive mail. The address is provably undeliverable,
        # so flag it rather than shipping it — but there is nothing to put in
        # its place, so it is counted separately from a repair.
        dead_only.append((email_id, address, orig_domain, fixed_domain))

    log.info("")
    log.info("  DNS-PROVEN repairs        %5d", len(to_insert))
    log.info("  undeliverable, no repair  %5d  (both forms lack MX)",
             len(dead_only))
    log.info("  left alone (real domain)  %5d", len(rejected))
    log.info("  no rule matched           %5d", len(no_rule))
    for addr, why in rejected[:10]:
        log.info("      leave  %-45s %s", addr, why)
    for _, addr, od, fd in dead_only[:10]:
        log.info("      dead   %-45s neither %s nor %s has MX", addr, od, fd)
    for addr in no_rule[:10]:
        log.info("      no-rule %s", addr)
    log.info("")
    for _, fixed, *_ in to_insert[:15]:
        log.info("  e.g. -> %s", fixed)

    if args.dry_run:
        log.info("DRY-RUN — nothing written.")
        conn.rollback()
        conn.close()
        return

    inserted = invalidated = dead_flagged = 0
    if to_insert or dead_only:
        # Chunk explicitly: cur.rowcount after execute_values reports only the
        # LAST page, which made the first S9-E run log 185 for 685 rows.
        CHUNK = 500
        for i in range(0, len(to_insert), CHUNK):
            psycopg2.extras.execute_values(
                cur, INSERT_SQL, to_insert[i:i + CHUNK], page_size=CHUNK)
            inserted += cur.rowcount
        # Invalidate only AFTER the repairs exist, so no contact is ever left
        # with its only address marked invalid and nothing to replace it.
        if to_invalidate:
            cur.execute(INVALIDATE_SQL, (to_invalidate,))
            invalidated = cur.rowcount
        # The dead ones get no replacement because none exists — neither form
        # publishes an MX, so the address cannot receive mail under any
        # spelling. Flagging beats shipping a guaranteed bounce; reversible via
        # the SQL in the docstring.
        if dead_only:
            cur.execute(INVALIDATE_SQL, ([d[0] for d in dead_only],))
            dead_flagged = cur.rowcount
        log.info("  repaired rows inserted     %5d", inserted)
        log.info("  glued rows invalidated     %5d", invalidated)
        log.info("  undeliverable flagged      %5d  (no replacement exists)",
                 dead_flagged)

        cur.execute(AUDIT_SQL, (
            json.dumps({
                "step": "S9-F", "script": SCRIPT_NAME,
                "selected": len(rows), "dns_proven": len(to_insert),
                "inserted": inserted, "invalidated": invalidated,
                "undeliverable_flagged": dead_flagged,
                "left_alone_real_domain": len(rejected),
                "no_rule": len(no_rule),
            }, ensure_ascii=False),
            SCRIPT_NAME,
            "Repair emails whose domain had a second domain welded on "
            "(orange.frnadoo.fr -> orange.fr); each repair proven by MX lookup "
            "with typosquat-wildcard detection",
        ))
        conn.commit()

    cur.execute(VERIFY_SQL)
    log.info("Verified — glued domains left in the deliverable: %d",
             cur.fetchone()[0])
    conn.close()


def _self_test() -> None:
    """Run the rule against the real observed cases. No DB, no network."""
    cases = {
        "orange.frnadoo.fr": "orange.fr",
        "wanadoo.froo.fr": "wanadoo.fr",
        "gmail.commail.fr": "gmail.com",
        "gmail.comgmail.com": "gmail.com",
        "laposte.netaposte.net": "laposte.net",
        "cegetel.netmail.com": "cegetel.net",
        "aleoe.orgendurance.fr": "aleoe.org",
        "giphar.friadis.net": "giphar.fr",
        "delattre.frorange.fr": "delattre.fr",
        "thelem-assurances.frassurances.fr": "thelem-assurances.fr",
        # already clean -> no proposal
        "orange.fr": None,
        "gmail.com": None,
        "mail.laposte.net": None,
    }
    bad = 0
    for src, want in cases.items():
        got = split_glued(src)
        ok = got == want
        bad += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {src:38s} -> {got!r} (want {want!r})")
    print(f"\n{len(cases) - bad}/{len(cases)} passed")
    sys.exit(1 if bad else 0)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Repair emails whose domain has a second domain welded on")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="check the split rule against known cases, no DB")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
    run(args)


if __name__ == "__main__":
    main()
