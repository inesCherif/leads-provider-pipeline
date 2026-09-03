"""
M3AG-S3 — Search-engine sweep per unreachable operator (Tavily, then ddgs)
=========================================================================
One query per operator that has neither phone nor e-mail (default target
set). Every RESULT is gated on its own text — geo (CP or commune) AND a
distinctive name token in title/url — before anything is mined from it:

    site      a host that is not an aggregator/social -> crawl target (m3ag_s4)
    phones    snippet phones (loose extractor is right for snippets), witness =
              the host — a snippet phone is a CLAIM, dialable only when a second
              independent witness names the same number (M2 rule, H15)
    e-mails   printed in the snippet — kept only if free-mail, same root as
              the host, or carrying a name token (third-party guard)
    social    facebook/instagram page URLs (for a later m2_s18-style harvest)

Output : checkpoints/search_hits.csv  (';', one row per accepted result,
         flushed per operator)   done-file: search_done.txt, keys
         "<backend>:<dept>:<row_id>" (backend-scoped, m2_s16's lesson).
Quota  : shared exports/boulangerie/checkpoints/search_quota.json — Tavily
         1,000/month, ddgs 300/day, hard-stopped by m2lib_search.

Usage:
    python scripts/m3ag_s3_search.py --departement 63 --backend tavily --pilot 20
    python scripts/m3ag_s3_search.py --departement 63 --backend tavily
    python scripts/m3ag_s3_search.py --departement 03 --backend ddgs
"""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_search import search, quota_state, QuotaExceeded, SearchAuthError   # noqa: E402
from m2lib_contact import extract_phones, is_surtaxe, plausible_fr_number, FREE_MAIL  # noqa: E402
from m3ag_lib import (CHECK_DIR, QUOTA_PATH, EMAIL_RE, load_operators, load_matches,  # noqa: E402
                      name_tokens, norm, geo_pass, host_of, root_domain,
                      is_aggregator, is_social, append_rows, load_done, mark_done)

OUT_PATH = CHECK_DIR / "search_hits.csv"
DONE_PATH = CHECK_DIR / "search_done.txt"

FIELDNAMES = ["row_id", "siret", "raisonSociale", "gerant", "ville", "codePostal",
              "backend", "query", "rank", "url", "host", "kind", "geo",
              "title", "phones", "emails"]

JUNK_LOCAL = {"noreply", "no-reply", "postmaster", "webmaster", "abuse", "exemple",
              "example", "email", "mail", "votre", "nom", "prenom", "xxx"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s3")


def build_query(op: dict) -> str:
    rs = op["raisonSociale"].strip()
    g = (op.get("gerant") or "").strip()
    label = rs
    if g and not name_tokens(g) <= name_tokens(rs):
        label = f"{rs} {g}"
    return f"{label} {op['ville']} {op['codePostal']}".strip()


def usable_email(e: str, host: str, toks: set) -> bool:
    e = e.lower().strip(".")
    local, _, dom = e.partition("@")
    if not local or not dom or local in JUNK_LOCAL or dom.endswith((".png", ".jpg")):
        return False
    # A snippet e-mail has ONE witness and no page to prove ownership, so the
    # address itself must carry the farm's name: `lea.morin@gmail.com`,
    # `richardgarnier@free.fr` pass; `contact@` on a guide page does not.
    # (pilot 2026-09-03: an osteopath and an obituary passed the geo+name
    # gates for a homonymous farmer — the name-in-address rule is what keeps
    # their mailbox off his row).
    flat = (local + dom).replace("-", "").replace(".", "").replace("_", "")
    named = any(t.lower() in flat for t in toks if len(t) >= 4)
    if not named:
        return False
    return dom in FREE_MAIL or root_domain(dom) == root_domain(host) or \
        any(t.lower() in dom.replace("-", "") for t in toks if len(t) >= 4)


def main() -> None:
    ap = argparse.ArgumentParser(description="Search sweep per unreachable operator")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--backend", default="tavily", choices=["tavily", "ddgs", "serper_web"])
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--targets", default="unreachable",
                    choices=["unreachable", "phoneless", "emailless", "all"])
    args = ap.parse_args()
    dept = args.departement

    ops = load_operators(dept)
    matches = load_matches(dept)
    done = load_done(DONE_PATH)

    def reached(op: dict) -> bool:
        ms = matches.get(op["_id"], [])
        has_phone = op["_has_phone"] or any(m.get("phone") or m.get("mobile") for m in ms)
        has_email = op["_has_email"] or any(m.get("email") for m in ms)
        if args.targets == "unreachable":
            return has_phone or has_email
        if args.targets == "phoneless":
            return has_phone
        if args.targets == "emailless":
            return has_email
        return False

    def dkey(op: dict) -> str:
        return f"{args.backend}:{dept}:{op['_id']}"

    todo = [op for op in ops if not reached(op) and dkey(op) not in done]
    # Operators with a named gérant first: a person's name is the most
    # distinctive query a farm can have. Then genuine agri before hors_agri.
    todo.sort(key=lambda op: (0 if op.get("gerant") else 1,
                              0 if op.get("flag_hors_agri") in ("", "0") else 1,
                              op["codePostal"]))
    if args.pilot:
        todo = todo[:args.pilot]
    elif args.limit:
        todo = todo[:args.limit]

    log.info(f"[{dept}] {len(ops)} operators | targets={args.targets} | "
             f"{len(done)} done keys | {len(todo)} to search via {args.backend}")
    log.info(f"quota before: {quota_state(QUOTA_PATH)}")

    stats = Counter()
    ops_hit = Counter()
    searched = 0
    try:
        for i, op in enumerate(todo, 1):
            q = build_query(op)
            try:
                results = search(q, args.backend, max_results=8, quota_path=QUOTA_PATH)
            except (QuotaExceeded, SearchAuthError):
                raise
            except Exception as exc:
                log.warning(f"[{i}/{len(todo)}] {op['raisonSociale'][:28]}: {type(exc).__name__}: {exc}")
                mark_done(DONE_PATH, dkey(op))
                continue

            toks = op["_tokens"]
            rows = []
            for rank, res in enumerate(results, 1):
                url = res.get("url") or ""
                title = res.get("title") or ""
                snippet = res.get("snippet") or ""
                blob = f"{title} {snippet} {url}"
                geo = geo_pass(op["codePostal"], op["ville"], blob)
                if not geo:
                    stats["result rejected: no geo marker"] += 1
                    continue
                head = set(norm(f"{title} {url.replace('-', ' ').replace('/', ' ')}").split())
                if toks and not (toks & head):
                    stats["result rejected: name not in title/url"] += 1
                    continue
                host = host_of(url)
                kind = "social" if is_social(url) else ("aggregator" if is_aggregator(url) else "site")
                phones = sorted(p for p in extract_phones(f"{title} {snippet}")
                                if plausible_fr_number(p) and not is_surtaxe(p))
                emails = sorted({e.lower() for e in EMAIL_RE.findall(f"{title} {snippet}")
                                 if usable_email(e, host, toks)})
                rows.append({
                    "row_id": op["_id"], "siret": op["siret"],
                    "raisonSociale": op["raisonSociale"], "gerant": op.get("gerant", ""),
                    "ville": op["ville"], "codePostal": op["codePostal"],
                    "backend": args.backend, "query": q, "rank": rank,
                    "url": url, "host": host, "kind": kind, "geo": geo,
                    "title": title[:200], "phones": "|".join(phones),
                    "emails": "|".join(emails),
                })
                stats[f"accepted: {kind}"] += 1
                if phones:
                    stats["phones found"] += len(phones)
                if emails:
                    stats["emails found"] += len(emails)
            if rows:
                append_rows(OUT_PATH, FIELDNAMES, rows)
                if any(r["kind"] == "site" for r in rows):
                    ops_hit["site"] += 1
                if any(r["phones"] for r in rows):
                    ops_hit["phone"] += 1
                if any(r["emails"] for r in rows):
                    ops_hit["email"] += 1
            mark_done(DONE_PATH, dkey(op))
            searched += 1
            if rows or i % 25 == 0:
                log.info(f"[{i}/{len(todo)}] {op['raisonSociale'][:26]:26.26} -> {len(rows)} hit(s) "
                         f"| ops with site {ops_hit['site']} phone {ops_hit['phone']} "
                         f"email {ops_hit['email']}")
    except QuotaExceeded as exc:
        log.warning(f"STOP (quota): {exc}")
        log.warning("Re-run later (ddgs: tomorrow; tavily: next month) — resumes from search_done.txt.")
    except SearchAuthError as exc:
        sys.exit(f"auth: {exc}")

    log.info("─" * 62)
    log.info(f"operators searched this run: {searched} of {len(todo)} targeted")
    for k, v in stats.most_common():
        log.info(f"  {k:<40} {v}")
    log.info(f"  operators with a site candidate {ops_hit['site']}, a phone claim "
             f"{ops_hit['phone']}, an e-mail {ops_hit['email']}")
    log.info(f"quota after: {quota_state(QUOTA_PATH)}")
    log.info("Next: m3ag_s4_crawl.py (sites -> e-mails), then m3ag_s8/s9.")


if __name__ == "__main__":
    main()
