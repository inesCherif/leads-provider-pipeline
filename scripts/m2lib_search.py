"""
m2lib_search — free-tier search backends behind one interface
=============================================================
Library, not a stage. The V2 lesson (measured 2026-08-13): every SCRAPED
search route dies at the IP/fingerprint layer — DataDome on Pages Jaunes,
CAPTCHA on Brave after ~85 queries, 403 on DDG lite/Mojeek, a stale Bing
recipe. More delays on a blocked IP change nothing. The V3 answer is
official APIs with free tiers that need no credit card:

    serper_maps    Serper.dev  2,500 credits ONE-TIME (account-wide),
                   *3 credits per call* — Google Maps listings incl. PHONE
                   NUMBERS and websites, ~20 per query, `ll` geo-anchoring.
                   MEASURED 2026-08-13: /places returns NO phone field;
                   /maps does. /maps has NO pagination (page=2 -> empty),
                   so dense areas need several anchors, not several pages.
    serper_places  Serper.dev  same pool, 1 credit — geo+website, NO phone
    serper_web     Serper.dev  same pool, 1 credit — Google organic results
    tavily         Tavily      1,000 credits / MONTH, web search + snippets
    ddgs           `ddgs` lib  no key, unofficial DuckDuckGo — pilot-grade
                   fallback only, capped hard per day out of politeness

Every call charges a persistent quota counter
(exports/boulangerie/checkpoints/search_quota.json) BEFORE the request and
HARD-STOPS at the free-tier limit: this project never sails past a free tier
into billing, and a crashed run resumes with an honest count. Charging on
attempt (not success) overcounts slightly on network errors — that is the
safe direction.

Keys: SERPER_API_KEY, TAVILY_API_KEY in .env (see .env.example).

Usage:
    python scripts/m2lib_search.py --selftest   # offline, no key needed
    python scripts/m2lib_search.py --ping       # 1 real query per backend
    python scripts/m2lib_search.py --quota      # show counters
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
QUOTA_PATH = CHECK_DIR / "search_quota.json"

# One UA for every M2 fetcher (was duplicated across s6/s8/s9).
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DELAY = (1.5, 3.5)          # seconds, randomized, after every network call
TIMEOUT = 20

# All serper_* backends share ONE account-wide credit pool -> one counter key.
POOL_OF = {"serper_maps": "serper", "serper_places": "serper",
           "serper_web": "serper", "tavily": "tavily", "ddgs": "ddgs"}
COST = {"serper_maps": 3}    # per-call credits; every other backend costs 1
LIMITS = {"serper": 2500,    # one-time free credits, no reset
          "tavily": 1000,    # resets monthly
          "ddgs": 300}       # our own politeness cap, resets daily

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2lib_search")


class QuotaExceeded(RuntimeError):
    """Free-tier limit reached — the run must stop, not degrade silently."""


class SearchAuthError(RuntimeError):
    """401/403 from an API — wrong or missing key, retrying is pointless."""


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass


def _requests():
    try:
        import requests
        return requests
    except ImportError:
        sys.exit("requests required:\n  pip install requests")


# ---------------------------------------------------------------- quota -----

def _load_quota(path: Path = QUOTA_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_quota(q: dict, path: Path = QUOTA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(q, indent=2), encoding="utf-8")


def charge(backend: str, n: int = 1, path: Path = QUOTA_PATH) -> None:
    """Count n credits against the backend's pool; raise QuotaExceeded at cap.

    tavily resets when the month changes, ddgs when the day changes; serper
    credits are one-time and never reset.
    """
    pool = POOL_OF[backend]
    q = _load_quota(path)
    entry = q.get(pool, {"used": 0})
    today = date.today()
    if pool == "tavily":
        month = today.strftime("%Y-%m")
        if entry.get("month") != month:
            entry = {"used": 0, "month": month}
    elif pool == "ddgs":
        day = today.isoformat()
        if entry.get("day") != day:
            entry = {"used": 0, "day": day}
    limit = LIMITS[pool]
    if entry["used"] + n > limit:
        raise QuotaExceeded(
            f"{pool}: {entry['used']}/{limit} used — refusing to exceed the "
            f"free tier. ({'resets monthly' if pool == 'tavily' else 'resets daily' if pool == 'ddgs' else 'one-time credits'})")
    entry["used"] += n
    entry["limit"] = limit
    q[pool] = entry
    _save_quota(q, path)


def quota_state(path: Path = QUOTA_PATH) -> dict:
    q = _load_quota(path)
    return {pool: {"used": q.get(pool, {}).get("used", 0), "limit": lim}
            for pool, lim in LIMITS.items()}


def reset_pool(pool: str, path: Path = QUOTA_PATH) -> dict:
    """Zero ONE pool's counter — for when a new API key brings a fresh
    allowance (e.g. the fresh Tavily key of 2026-08-13). This is the only
    sanctioned way to reset: it logs before/after and stamps the reset date
    in the file, so the trail survives. Never hand-edit search_quota.json.
    """
    if pool not in LIMITS:
        raise ValueError(f"unknown pool {pool!r} — one of {sorted(LIMITS)}")
    q = _load_quota(path)
    before = q.get(pool, {}).get("used", 0)
    today = date.today()
    entry = {"used": 0, "limit": LIMITS[pool], "reset_at": today.isoformat()}
    if pool == "tavily":
        entry["month"] = today.strftime("%Y-%m")
    elif pool == "ddgs":
        entry["day"] = today.isoformat()
    q[pool] = entry
    _save_quota(q, path)
    log.info(f"pool '{pool}' reset: used {before} -> 0 / {LIMITS[pool]}")
    return entry


# ---------------------------------------------------- payload normalizers ---
# Pure functions so --selftest can exercise them offline with canned JSON.
# Normalized result shape (keys always present):
#   title, url, snippet, phone, address, lat, lon, rating, source_id

def _blank() -> dict:
    return {"title": "", "url": "", "snippet": "", "phone": "", "address": "",
            "lat": "", "lon": "", "rating": "", "source_id": ""}


def _norm_serper_maps(payload: dict) -> list:
    out = []
    for p in payload.get("places", []):
        r = _blank()
        r["title"] = p.get("title") or ""
        r["url"] = p.get("website") or ""
        r["snippet"] = p.get("type") or ""
        r["phone"] = p.get("phoneNumber") or ""
        r["address"] = p.get("address") or ""
        r["lat"] = p.get("latitude", "")
        r["lon"] = p.get("longitude", "")
        r["rating"] = p.get("rating", "")
        r["source_id"] = str(p.get("cid") or p.get("placeId") or "")
        out.append(r)
    return out


def _norm_serper_places(payload: dict) -> list:
    out = []
    for p in payload.get("places", []):
        r = _blank()
        r["title"] = p.get("title") or ""
        r["url"] = p.get("website") or ""
        r["snippet"] = p.get("category") or ""
        r["phone"] = p.get("phoneNumber") or ""
        r["address"] = p.get("address") or ""
        r["lat"] = p.get("latitude", "")
        r["lon"] = p.get("longitude", "")
        r["rating"] = p.get("rating", "")
        r["source_id"] = str(p.get("cid") or p.get("placeId") or "")
        out.append(r)
    return out


def _norm_serper_web(payload: dict) -> list:
    out = []
    for p in payload.get("organic", []):
        r = _blank()
        r["title"] = p.get("title") or ""
        r["url"] = p.get("link") or ""
        r["snippet"] = p.get("snippet") or ""
        out.append(r)
    return out


def _norm_tavily(payload: dict) -> list:
    out = []
    for p in payload.get("results", []):
        r = _blank()
        r["title"] = p.get("title") or ""
        r["url"] = p.get("url") or ""
        r["snippet"] = p.get("content") or ""
        out.append(r)
    return out


def _norm_ddgs(items: list) -> list:
    out = []
    for p in items or []:
        r = _blank()
        r["title"] = p.get("title") or ""
        r["url"] = p.get("href") or p.get("url") or ""
        r["snippet"] = p.get("body") or ""
        out.append(r)
    return out


# -------------------------------------------------------------- backends ----

def _post_json(url: str, headers: dict, body: dict) -> dict:
    """POST with bounded retry. 429/5xx retried, 401/403 fatal."""
    requests = _requests()
    last = None
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=TIMEOUT)
        except Exception as exc:
            last = exc
            time.sleep(2 ** attempt * 2)
            continue
        if resp.status_code in (401, 403):
            raise SearchAuthError(f"{url}: HTTP {resp.status_code} — check the API key in .env")
        if resp.status_code == 429 or resp.status_code >= 500:
            last = RuntimeError(f"HTTP {resp.status_code}")
            time.sleep(2 ** attempt * 2)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"{url}: giving up after 3 attempts ({last})")


def _serper(query: str, mode: str, max_results: int, ll: str = "") -> list:
    key = os.environ.get("SERPER_API_KEY", "")
    if not key:
        raise SearchAuthError("SERPER_API_KEY missing — add it to .env")
    endpoint = {"places": "places", "maps": "maps"}.get(mode, "search")
    body = {"q": query, "gl": "fr", "hl": "fr"}
    if ll:
        body["ll"] = ll                      # "@lat,lon,15z" — maps only
    payload = _post_json(f"https://google.serper.dev/{endpoint}",
                         {"X-API-KEY": key, "Content-Type": "application/json"},
                         body)
    rows = {"places": _norm_serper_places, "maps": _norm_serper_maps
            }.get(mode, _norm_serper_web)(payload)
    return rows[:max_results]


def _tavily(query: str, max_results: int) -> list:
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        raise SearchAuthError("TAVILY_API_KEY missing — add it to .env")
    payload = _post_json("https://api.tavily.com/search",
                         {"Authorization": f"Bearer {key}",
                          "Content-Type": "application/json"},
                         {"query": query, "search_depth": "basic",
                          "country": "france", "max_results": max_results})
    return _norm_tavily(payload)


def _ddgs(query: str, max_results: int) -> list:
    try:
        from ddgs import DDGS                       # current package name
    except ImportError:
        try:
            from duckduckgo_search import DDGS      # its former name
        except ImportError:
            sys.exit("ddgs required for the ddgs backend:\n  pip install ddgs")
    with DDGS() as d:
        items = list(d.text(query, region="fr-fr", max_results=max_results))
    return _norm_ddgs(items)


def available_backends() -> list:
    """Backends usable right now, given keys/libs present."""
    _load_env()
    out = []
    if os.environ.get("SERPER_API_KEY"):
        out += ["serper_places", "serper_web"]
    if os.environ.get("TAVILY_API_KEY"):
        out.append("tavily")
    try:
        import ddgs  # noqa: F401
        out.append("ddgs")
    except ImportError:
        try:
            import duckduckgo_search  # noqa: F401
            out.append("ddgs")
        except ImportError:
            pass
    return out


def search(query: str, backend: str, max_results: int = 8,
           quota_path: Path = QUOTA_PATH, ll: str = "") -> list:
    """One search. Charges quota FIRST (hard-stop), sleeps AFTER (politeness).

    `ll="@lat,lon,15z"` anchors serper_maps geographically (ignored elsewhere).
    Returns normalized rows; raises QuotaExceeded / SearchAuthError — callers
    must let QuotaExceeded stop the run, never swallow it.
    """
    if backend not in POOL_OF:
        raise ValueError(f"unknown backend {backend!r} — one of {sorted(POOL_OF)}")
    _load_env()
    charge(backend, COST.get(backend, 1), quota_path)
    if backend == "serper_maps":
        rows = _serper(query, "maps", max_results, ll)
    elif backend == "serper_places":
        rows = _serper(query, "places", max_results)
    elif backend == "serper_web":
        rows = _serper(query, "web", max_results)
    elif backend == "tavily":
        rows = _tavily(query, max_results)
    else:
        rows = _ddgs(query, max_results)
    time.sleep(random.uniform(*DELAY))
    return rows


# -------------------------------------------------------------- selftest ----

def selftest() -> int:
    import tempfile
    failed = 0

    def check(label, got, want):
        nonlocal failed
        ok = got == want
        if not ok:
            failed += 1
        print(f"  {'OK ' if ok else 'FAIL'} {label}"
              + ("" if ok else f": {got!r} != {want!r}"))

    print("normalizers (canned payloads, offline):")
    places = _norm_serper_places({"places": [{
        "title": "Boulangerie Marius", "address": "12 Rue de Rome, 13001 Marseille",
        "latitude": 43.297, "longitude": 5.38, "rating": 4.6,
        "phoneNumber": "+33 4 91 12 34 56", "website": "https://marius.fr",
        "cid": "123456789"}]})
    check("places count", len(places), 1)
    check("places phone", places[0]["phone"], "+33 4 91 12 34 56")
    check("places url=website", places[0]["url"], "https://marius.fr")
    check("places source_id", places[0]["source_id"], "123456789")
    maps = _norm_serper_maps({"places": [{
        "title": "Boulangerie Aixoise", "address": "45 Rue Davso, 13001 Marseille",
        "latitude": 43.293, "longitude": 5.376, "type": "Boulangerie",
        "phoneNumber": "+33 4 91 33 93 85", "website": "http://ba.fr",
        "cid": "9696", "placeId": "ChIJx"}]})
    check("maps phone", maps[0]["phone"], "+33 4 91 33 93 85")
    check("maps cid preferred over placeId", maps[0]["source_id"], "9696")
    web = _norm_serper_web({"organic": [{"title": "t", "link": "https://a.fr",
                                         "snippet": "Tél 04 91 00 00 00"}]})
    check("web url", web[0]["url"], "https://a.fr")
    check("web snippet kept", "04 91" in web[0]["snippet"], True)
    tav = _norm_tavily({"results": [{"title": "t", "url": "https://b.fr",
                                     "content": "c"}]})
    check("tavily url", tav[0]["url"], "https://b.fr")
    dd = _norm_ddgs([{"title": "t", "href": "https://c.fr", "body": "b"}])
    check("ddgs url", dd[0]["url"], "https://c.fr")
    check("all keys always present",
          sorted(places[0]) == sorted(web[0]) == sorted(_blank()), True)

    print("quota counter (temp file):")
    with tempfile.TemporaryDirectory() as td:
        qp = Path(td) / "q.json"
        for _ in range(3):
            charge("serper_places", 1, qp)
        charge("serper_web", 1, qp)            # same pool
        check("serper pool shared", _load_quota(qp)["serper"]["used"], 4)
        charge("serper_maps", COST["serper_maps"], qp)   # 3-credit call
        check("maps costs 3", _load_quota(qp)["serper"]["used"], 7)
        try:
            charge("serper_places", LIMITS["serper"], qp)
            check("hard-stop raises", "no exception", "QuotaExceeded")
        except QuotaExceeded:
            check("hard-stop raises", True, True)
        check("failed charge not recorded", _load_quota(qp)["serper"]["used"], 7)
        # tavily monthly reset
        _save_quota({"tavily": {"used": 999, "limit": 1000, "month": "2020-01"}}, qp)
        charge("tavily", 1, qp)
        e = _load_quota(qp)["tavily"]
        check("tavily reset on new month", (e["used"], e["month"] != "2020-01"), (1, True))
        # ddgs daily reset
        _save_quota({"ddgs": {"used": 299, "limit": 300, "day": "2020-01-01"}}, qp)
        charge("ddgs", 1, qp)
        check("ddgs reset on new day", _load_quota(qp)["ddgs"]["used"], 1)
        # --reset-pool: zeroes ONE pool, leaves the others untouched
        _save_quota({"tavily": {"used": 1000, "limit": 1000, "month": "2026-08"},
                     "serper": {"used": 2299, "limit": 2500}}, qp)
        reset_pool("tavily", qp)
        after = _load_quota(qp)
        check("reset zeroes the pool", after["tavily"]["used"], 0)
        check("reset stamps a date", "reset_at" in after["tavily"], True)
        check("reset leaves other pools alone", after["serper"]["used"], 2299)
        charge("tavily", 1, qp)
        check("charging after reset counts from 0", _load_quota(qp)["tavily"]["used"], 1)

    print(f"\n{'ALL OK' if not failed else str(failed) + ' FAILED'}")
    return 1 if failed else 0


def ping() -> int:
    backends = available_backends()
    if not backends:
        print("No backend configured. Add SERPER_API_KEY / TAVILY_API_KEY to "
              ".env, or `pip install ddgs`.")
        return 1
    q = "boulangerie vieux port marseille"
    rc = 0
    for b in backends:
        try:
            rows = search(q, b, max_results=3)
            first = rows[0] if rows else {}
            print(f"  {b:<14} {len(rows)} results | first: "
                  f"{(first.get('title') or '')[:30]!r} "
                  f"{('phone=' + first['phone']) if first.get('phone') else ''}")
        except Exception as exc:
            print(f"  {b:<14} FAILED: {exc}")
            rc = 1
    print("\nquota now:")
    for pool, st in quota_state().items():
        print(f"  {pool:<8} {st['used']}/{st['limit']}")
    return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Search backends (library)")
    ap.add_argument("--selftest", action="store_true", help="offline checks")
    ap.add_argument("--ping", action="store_true", help="1 real query per configured backend")
    ap.add_argument("--quota", action="store_true", help="show quota counters")
    ap.add_argument("--reset-pool", metavar="POOL", choices=sorted(LIMITS),
                    help="zero ONE pool's counter (use after installing a new "
                         "API key that carries a fresh allowance)")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    if args.reset_pool:
        reset_pool(args.reset_pool)
        for pool, st in quota_state().items():
            print(f"  {pool:<8} {st['used']}/{st['limit']}")
        sys.exit(0)
    if args.ping:
        sys.exit(ping())
    if args.quota:
        for pool, st in quota_state().items():
            print(f"  {pool:<8} {st['used']}/{st['limit']}")
        sys.exit(0)
    ap.print_help()
