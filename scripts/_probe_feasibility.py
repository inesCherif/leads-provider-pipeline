"""
S9 Phase 0 - feasibility probes for the domain/email discovery engine.

READ-ONLY. Writes nothing, anywhere. Throwaway diagnostic: every later phase of
the plan depends on one of these answers, and each is cheap to establish now and
expensive to discover halfway through a six-hour run.

Each probe is independent and self-contained: a probe that blows up reports FAIL
and the rest still run. Nothing here should ever stop the script.

    python scripts/_probe_feasibility.py
    python scripts/_probe_feasibility.py --only smtp25,dns

What each probe decides
-----------------------
  smtp25     Is outbound port 25 open from this machine? If not, Phase 5 cannot
             do SMTP RCPT TO verification at all and degrades to MX + syntax.
             This is THE question - French consumer ISPs usually block port 25,
             and no proxy fixes it (mail servers judge the connecting IP's
             reputation, and free proxies rank worse than a home connection).
  dns        Async MX/A throughput. Phase 2 probes ~50k businesses x ~6 candidate
             domains; at 200/s that is 25 minutes, at 5/s it is a non-starter.
  agencebio  The single highest-yield free source: SIRET-keyed, no API key,
             50 req/s, returns siteWebs[] + phones + gerant. Probed against a
             real SIRET from our own DB, not a made-up one.
  afnic      The monthly .fr open data zip - does it exist, and does the CSV
             carry the holder department/type columns the trigram join needs?
  playwright Headless Chromium, for JS-rendered sites in Phase 3 and search
             scraping in Phase 4.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import socket
import ssl
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("probe")

RESULTS: list[tuple[str, str, str]] = []  # (probe, verdict, detail)


def record(probe: str, verdict: str, detail: str) -> None:
    RESULTS.append((probe, verdict, detail))
    level = logging.INFO if verdict in ("PASS", "INFO") else logging.WARNING
    log.log(level, "[%s] %s - %s", probe, verdict, detail)


# --------------------------------------------------------------------------
# Probe 1: outbound port 25
# --------------------------------------------------------------------------

# Three well-known MX hosts. If ALL three time out it is a local/ISP block, not
# one server being unfriendly - that distinction is the whole point of testing
# more than one.
SMTP_TARGETS = [
    ("gmail-smtp-in.l.google.com", "Google"),
    ("mx1.orange.fr", "Orange"),
    ("mx-eu.mail.am0.yahoodns.net", "Yahoo"),
]


def probe_smtp25(timeout: float = 8.0) -> None:
    reachable, blocked = [], []
    for host, label in SMTP_TARGETS:
        try:
            start = time.perf_counter()
            with socket.create_connection((host, 25), timeout=timeout) as sock:
                sock.settimeout(timeout)
                banner = sock.recv(1024).decode("utf-8", "replace").strip()
            elapsed = time.perf_counter() - start
            reachable.append(f"{label} ({elapsed:.1f}s): {banner[:80]}")
        except Exception as exc:  # noqa: BLE001 - any failure is a failure
            blocked.append(f"{label}: {type(exc).__name__} {exc}")

    if reachable:
        record(
            "smtp25",
            "PASS",
            f"port 25 OPEN - {len(reachable)}/{len(SMTP_TARGETS)} MX hosts "
            f"answered. SMTP RCPT TO verification is possible. "
            + " | ".join(reachable),
        )
    else:
        record(
            "smtp25",
            "FAIL",
            "port 25 BLOCKED on all "
            f"{len(SMTP_TARGETS)} MX hosts - Phase 5 degrades to MX + syntax "
            "only. No proxy fixes this. " + " | ".join(blocked),
        )


def probe_smtp_submission() -> None:
    """Ports 465/587 are for SENDING, not verifying - they need auth and will
    not answer RCPT TO for a third-party domain. Probed only to tell 'the ISP
    blocks 25 specifically' apart from 'this machine has no outbound network'."""
    open_ports = []
    for port in (465, 587):
        try:
            with socket.create_connection(("smtp.gmail.com", port), timeout=6):
                open_ports.append(str(port))
        except Exception:  # noqa: BLE001
            pass
    if open_ports:
        record(
            "smtp25-control",
            "INFO",
            f"submission ports {', '.join(open_ports)} reachable - so general "
            "outbound egress works; a port 25 failure above is a targeted "
            "ISP/firewall block, not a dead network.",
        )
    else:
        record(
            "smtp25-control",
            "WARN",
            "ports 465/587 also unreachable - outbound SMTP egress looks "
            "blocked wholesale (corporate firewall or VPN?).",
        )


# --------------------------------------------------------------------------
# Probe 2: async DNS throughput
# --------------------------------------------------------------------------

# Real French agricultural / SMB domains, plus deliberate non-existent ones so
# the NXDOMAIN path is timed too - Phase 2 is mostly misses, so miss latency is
# what actually sets the runtime.
DNS_SAMPLE = [
    "orange.fr", "gmail.com", "wanadoo.fr", "free.fr", "laposte.net",
    "agriculture.gouv.fr", "chambres-agriculture.fr", "agencebio.org",
    "terre-net.fr", "educagri.fr", "afnic.fr", "insee.fr",
    "bienvenue-a-la-ferme.com", "data.gouv.fr", "inrae.fr", "safer.fr",
] + [f"earl-{n}-probe-nonexistent-{n}.fr" for n in range(24)]


async def _resolve_one(resolver, domain: str) -> tuple[str, bool]:
    import dns.resolver  # noqa: PLC0415

    try:
        await resolver.resolve(domain, "MX")
        return domain, True
    except dns.resolver.NoAnswer:
        # Domain exists but has no MX. Still a real domain - Phase 2 counts it.
        return domain, True
    except Exception:  # noqa: BLE001 - NXDOMAIN, timeout, SERVFAIL
        return domain, False


async def _dns_run() -> tuple[int, int, float]:
    import dns.asyncresolver  # noqa: PLC0415

    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 5.0
    resolver.timeout = 5.0

    start = time.perf_counter()
    results = await asyncio.gather(
        *(_resolve_one(resolver, d) for d in DNS_SAMPLE)
    )
    elapsed = time.perf_counter() - start
    hits = sum(1 for _, ok in results if ok)
    return hits, len(results), elapsed


def probe_dns() -> None:
    try:
        hits, total, elapsed = asyncio.run(_dns_run())
    except Exception as exc:  # noqa: BLE001
        record("dns", "FAIL", f"{type(exc).__name__}: {exc}")
        return

    rate = total / elapsed if elapsed else 0.0
    # Phase 2 worst case: ~50k businesses x 6 candidates = 300k lookups.
    projected_min = (300_000 / rate / 60) if rate else float("inf")
    verdict = "PASS" if rate >= 20 else "WARN"
    record(
        "dns",
        verdict,
        f"{total} concurrent lookups in {elapsed:.2f}s = {rate:.0f}/s "
        f"({hits} resolved, {total - hits} NXDOMAIN as expected). "
        f"Phase 2 at 300k lookups projects ~{projected_min:.0f} min.",
    )


# --------------------------------------------------------------------------
# Probe 3: Agence Bio open data API
# --------------------------------------------------------------------------

AGENCE_BIO_BASE = "https://opendata.agencebio.org/api/gouv/operateurs/"


def _sample_sirets(limit: int = 5) -> list[str]:
    """Pull a few real SIRETs from our own DB. Probing the API with an invented
    SIRET proves nothing about our actual match rate."""
    try:
        import psycopg2  # noqa: PLC0415
        from dotenv import load_dotenv  # noqa: PLC0415
    except ImportError as exc:
        record("agencebio-db", "WARN", f"cannot read SIRETs: {exc}")
        return []

    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        record("agencebio-db", "WARN", "SUPABASE_DB_URL not set")
        return []

    try:
        conn = psycopg2.connect(
            url,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
    except Exception as exc:  # noqa: BLE001
        record("agencebio-db", "WARN", f"connect failed: {type(exc).__name__} {exc}")
        return []

    try:
        with conn.cursor() as cur:
            # Tier-1 rows with a real NAF are the likeliest to also be certified
            # organic, so this is a fair - not flattering - sample.
            cur.execute(
                """
                SELECT siret
                FROM public.v_enrichment_queue
                WHERE siret IS NOT NULL
                  AND naf_code LIKE '01%%'
                ORDER BY business_id
                LIMIT %s
                """,
                (limit,),
            )
            return [r[0].strip() for r in cur.fetchall() if r[0]]
    except Exception as exc:  # noqa: BLE001
        record("agencebio-db", "WARN", f"query failed: {type(exc).__name__} {exc}")
        return []
    finally:
        conn.close()


def probe_agence_bio() -> None:
    try:
        import requests  # noqa: PLC0415
    except ImportError as exc:
        record("agencebio", "FAIL", f"requests not importable: {exc}")
        return

    # 1. Is the API up at all?
    try:
        resp = requests.get(
            AGENCE_BIO_BASE, params={"nb": 2, "debut": 0}, timeout=20
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        record("agencebio", "FAIL", f"unreachable: {type(exc).__name__} {exc}")
        return

    # nbTotal comes back as a string on this endpoint - int() it before any
    # thousands-separator formatting, or f"{total:,}" raises ValueError.
    try:
        total = int(payload.get("nbTotal") or 0)
    except (TypeError, ValueError):
        total = 0
    items = payload.get("items") or []
    if not items:
        record("agencebio", "WARN", f"reachable but returned no items (nbTotal={total})")
        return

    keys = sorted(items[0].keys())
    wanted = ["siret", "siteWebs", "telephone", "telephoneCommerciale", "gerant"]
    present = [k for k in wanted if k in items[0]]
    record(
        "agencebio",
        "PASS" if len(present) >= 4 else "WARN",
        f"API live, nbTotal={total:,} operators. Fields we need present: "
        f"{present}. Full key list: {keys}",
    )

    # 2. Do OUR SIRETs actually match? This is the number that sets Phase 1a yield.
    sirets = _sample_sirets(5)
    if not sirets:
        record("agencebio-match", "WARN", "no SIRETs available to test matching")
        return

    matched, with_site, with_phone = 0, 0, 0
    for siret in sirets:
        try:
            r = requests.get(AGENCE_BIO_BASE, params={"siret": siret}, timeout=20)
            r.raise_for_status()
            found = (r.json() or {}).get("items") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("  siret %s -> %s %s", siret, type(exc).__name__, exc)
            continue
        if found:
            matched += 1
            op = found[0]
            if op.get("siteWebs"):
                with_site += 1
            if any(op.get(k) for k in ("telephone", "telephoneNational",
                                       "telephoneCommerciale")):
                with_phone += 1
        time.sleep(0.1)  # nowhere near their 50/s cap; be a good citizen

    record(
        "agencebio-match",
        "INFO",
        f"tiny sample of {len(sirets)} real SIRETs: {matched} found in Agence "
        f"Bio, {with_site} with a website, {with_phone} with a phone. "
        "NOT a yield estimate - Phase 1a pilot measures that on 500.",
    )


# --------------------------------------------------------------------------
# Probe 4: AFNIC .fr open data
# --------------------------------------------------------------------------

AFNIC_FTP = "https://www.afnic.fr/wp-media/ftp/documentsOpenData/"


def _afnic_candidates(months_back: int = 4) -> list[str]:
    """Files are published on the 15th, named YYYYMM_OPENDATA_A-NomsDeDomaineEnPointFr.
    Walk backwards so a run early in the month still finds the latest."""
    out, y, m = [], date.today().year, date.today().month
    for _ in range(months_back):
        out.append(f"{y}{m:02d}_OPENDATA_A-NomsDeDomaineEnPointFr")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def probe_afnic() -> None:
    try:
        import requests  # noqa: PLC0415
    except ImportError as exc:
        record("afnic", "FAIL", f"requests not importable: {exc}")
        return

    found_url, size = None, 0
    for stem in _afnic_candidates():
        for ext in (".zip", ".csv.zip"):
            url = f"{AFNIC_FTP}{stem}{ext}"
            try:
                head = requests.head(url, timeout=20, allow_redirects=True)
            except Exception:  # noqa: BLE001
                continue
            if head.status_code == 200:
                found_url = url
                size = int(head.headers.get("Content-Length") or 0)
                break
        if found_url:
            break

    if not found_url:
        record(
            "afnic",
            "WARN",
            "no monthly file found at the guessed URLs. The naming convention "
            f"may have changed - browse {AFNIC_FTP} manually before Phase 1b.",
        )
        return

    record("afnic", "PASS", f"found {found_url} ({size / 1e6:.0f} MB)")

    # Range-read the first chunk so we can see the header row without pulling
    # a multi-hundred-MB zip. Zip needs a full member to decompress, so this is
    # best-effort: if the range trick fails we just report the file exists.
    try:
        r = requests.get(
            found_url, headers={"Range": "bytes=0-2000000"}, timeout=60
        )
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as fh:
                header = fh.readline().decode("latin-1", "replace").strip()
        record(
            "afnic-columns",
            "PASS",
            f"{name} header: {header}",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "afnic-columns",
            "INFO",
            f"could not range-read the header ({type(exc).__name__}); expected "
            "for a truncated zip. Phase 1b downloads it in full and inspects "
            "there. Columns we need: 'Nom de domaine', 'Type du titulaire', "
            "'Departement titulaire'.",
        )


# --------------------------------------------------------------------------
# Probe 5: Playwright headless Chromium
# --------------------------------------------------------------------------

def probe_playwright() -> None:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:
        record("playwright", "FAIL", f"not importable: {exc}")
        return

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                )
            )
            # domcontentloaded, not the default "load": many French SMB sites
            # never fire load because a third-party widget hangs forever, and
            # Phase 3 must not lose a page over one stuck tracker.
            page.goto(
                "https://www.data.gouv.fr/",
                timeout=45_000,
                wait_until="domcontentloaded",
            )
            title = page.title()
            html_len = len(page.content())
            browser.close()
        record(
            "playwright",
            "PASS",
            f"headless Chromium OK, title={title!r}, {html_len:,} bytes of HTML",
        )
    except Exception as exc:  # noqa: BLE001
        record("playwright", "FAIL", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Probe 6: proxy path (Tor SOCKS5), for Phase 4 only
# --------------------------------------------------------------------------

def probe_proxy() -> None:
    try:
        with socket.create_connection(("127.0.0.1", 9050), timeout=3):
            pass
        record("proxy", "PASS", "Tor SOCKS5 listening on 127.0.0.1:9050")
        return
    except Exception:  # noqa: BLE001
        pass

    proxy_list = os.getenv("PROXY_LIST")
    if proxy_list:
        record("proxy", "INFO", f"PROXY_LIST env set ({len(proxy_list.split(','))} entries)")
    else:
        record(
            "proxy",
            "INFO",
            "no Tor on :9050 and no PROXY_LIST env var. Phases 1-3 do not need "
            "a proxy (open data + the prospect's own site). Only Phase 4 does.",
        )


# --------------------------------------------------------------------------

PROBES = {
    "smtp25": lambda: (probe_smtp25(), probe_smtp_submission()),
    "dns": probe_dns,
    "agencebio": probe_agence_bio,
    "afnic": probe_afnic,
    "playwright": probe_playwright,
    "proxy": probe_proxy,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--only",
        help=f"comma-separated subset of: {', '.join(PROBES)}",
    )
    args = ap.parse_args()

    selected = list(PROBES)
    if args.only:
        selected = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in selected if s not in PROBES]
        if unknown:
            sys.exit(f"Unknown probe(s): {unknown}. Known: {list(PROBES)}")

    log.info("=" * 72)
    log.info("S9 Phase 0 feasibility probes - READ ONLY, writes nothing")
    log.info("Python %s", sys.version.split()[0])
    log.info("=" * 72)

    for name in selected:
        log.info("--- %s ---", name)
        try:
            PROBES[name]()
        except Exception as exc:  # noqa: BLE001 - a probe must never stop the run
            record(name, "FAIL", f"probe itself crashed: {type(exc).__name__} {exc}")

    log.info("=" * 72)
    log.info("SUMMARY")
    log.info("=" * 72)
    width = max(len(p) for p, _, _ in RESULTS) if RESULTS else 10
    for probe, verdict, detail in RESULTS:
        log.info("%-*s  %-5s  %s", width, probe, verdict, detail)

    fails = [p for p, v, _ in RESULTS if v == "FAIL"]
    if fails:
        log.info("")
        log.info("FAILED probes: %s", ", ".join(fails))
    # Always exit 0: this is a diagnostic. A blocked port 25 is a finding to act
    # on, not a script error, and a non-zero exit here would read as a crash.
    return 0


if __name__ == "__main__":
    sys.exit(main())
