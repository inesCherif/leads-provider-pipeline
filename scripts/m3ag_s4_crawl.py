"""
M3AG-S4 — Crawl candidate websites for e-mails / phones, validated per (operator, domain)
=====================================================================================
Targets = every (operator, domain) pair we hold:
    * the operator's own `siteWebs` (Agence Bio, owner-declared)
    * `website` on accepted matches (OSM, Pages Jaunes…)   — matched_<dept>.csv
    * `kind=site` results of the search sweep              — search_hits.csv
    * `places_listings.csv` websites, when Places has run

Pages read: home, /contact, /contacts, /nous-contacter, /mentions-legales,
/a-propos (+ sitemap-driven deep links when --deep). Extraction reuses
m2_s9 (`extract_emails`, obfuscation-aware) and m2lib_contact
(`extract_phones_ctx` — a number must be ANNOUNCED as a phone; the loose
extractor read minified JS as phone numbers in V3).

Validation reuses m2lib_validate.classify with the agri vocabulary injected:
the verdict is per (operator, domain), never per domain — one chain site is
right for one row and wrong for the next (V7 lesson). Only `valide` and
`non_verifiable` ship; e-mails additionally pass `is_third_party_email`
(a farm's supplier is not the farm) and the store-list cap.

Output : checkpoints/site_verdicts.csv, checkpoints/site_contacts.csv (';')
Done   : checkpoints/crawl_done.txt  keys "<dept>:<row_id>:<domain>"

Usage:
    python scripts/m3ag_s4_crawl.py --departement 63 --pilot 20
    python scripts/m3ag_s4_crawl.py --departement 63
    python scripts/m3ag_s4_crawl.py --departement 03 --deep
"""

import argparse
import logging
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import requests                                                      # noqa: E402
from m2_s9_emails import extract_emails, deep_urls, SUBPAGES, JUNK_LOCAL, GENERIC_LOCALS  # noqa: E402
from m2lib_contact import (extract_phones_ctx, extract_social, is_third_party_email,     # noqa: E402
                           is_surtaxe, plausible_fr_number, normalize_fr_phone)
from m2lib_validate import (strip_tags, classify, ownership, site_confiance,            # noqa: E402
                            VERDICT_SHIPS)
from m3ag_lib import (CHECK_DIR, load_operators, load_matches, read_csv, append_rows,     # noqa: E402
                      load_done, mark_done, host_of, is_aggregator, is_social,
                      agri_score, name_tokens, norm)

VERDICTS_PATH = CHECK_DIR / "site_verdicts.csv"
CONTACTS_PATH = CHECK_DIR / "site_contacts.csv"
DONE_PATH = CHECK_DIR / "crawl_done.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 12
DELAY = 0.8
MAX_EMAILS_PER_DOMAIN = 12     # a page with more is a store/member list

VERDICT_FIELDS = ["dept", "row_id", "siret", "raisonSociale", "domain", "source",
                  "reached", "blocked", "verdict", "reason", "own", "shared",
                  "agri_score", "pages_read", "text_chars"]
CONTACT_FIELDS = ["dept", "row_id", "siret", "raisonSociale", "domain", "source",
                  "verdict", "confiance", "email", "phone", "facebook", "instagram"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s4")


def domain_of(url: str) -> str:
    u = url.strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    h = host_of(u)
    return h if "." in h else ""


def collect_targets(dept: str, ops: list, matches: dict) -> list:
    """[(op, domain, source)] deduped on (row_id, domain)."""
    seen = set()
    out = []

    def add(op, url, source):
        d = domain_of(url)
        if not d or is_aggregator(d) or is_social(d):
            return
        k = (op["_id"], d)
        if k in seen:
            return
        seen.add(k)
        out.append((op, d, source))

    by_id = {op["_id"]: op for op in ops}
    for op in ops:
        for u in (op.get("siteWebs") or "").split("; "):
            if u.strip():
                add(op, u, "agencebio")
    for rid, ms in matches.items():
        op = by_id.get(rid)
        if not op:
            continue
        for m in ms:
            if m.get("website"):
                add(op, m["website"], m.get("source", "match"))
    for h in read_csv(CHECK_DIR / "search_hits.csv"):
        op = by_id.get(h["row_id"])
        if op and not is_aggregator(h["url"]) and not is_social(h["url"]):
            add(op, h["url"], f"search:{h['backend']}")
    for p in read_csv(CHECK_DIR / "places_listings.csv"):
        op = by_id.get(p.get("row_id", ""))
        if op and p.get("website"):
            add(op, p["website"], "places")
    return out


def fetch(sess: requests.Session, url: str):
    """(status, html) — status 0 on network failure (after one slower retry).

    2026-09-03: with four jobs sharing the line, 109/200 dept-03 sites came
    back 'mort' and answered 200 minutes later. Our timeout is not a fact
    about their server — retry once, and let the caller flag status 0."""
    for attempt, to in enumerate((TIMEOUT, TIMEOUT * 2)):
        try:
            r = sess.get(url, timeout=to, allow_redirects=True)
            ok = "html" in (r.headers.get("content-type") or "") or r.text[:200].lstrip().startswith("<")
            return r.status_code, (r.text if ok else "")
        except requests.exceptions.SSLError:
            return -1, ""          # host alive, TLS broken: caller tries http
        except Exception:
            if attempt == 0:
                time.sleep(1.5)
    return 0, ""


def crawl_domain(sess: requests.Session, domain: str, deep: bool) -> tuple:
    """(reached, blocked, html_all, pages_read, final_host)."""
    html_parts = []
    reached = blocked = net_fail = False
    pages = 0
    final_host = domain
    urls = [f"https://{domain}{p}" for p in SUBPAGES]
    if deep:
        urls += [u for u in deep_urls(sess, domain) if u not in urls]
    for i, u in enumerate(urls):
        status, html = fetch(sess, u)
        if status in (0, -1) and i == 0:
            # https failed outright: try http once
            status, html = fetch(sess, f"http://{domain}")
        if status == 0 and i == 0:
            net_fail = True
        if status in (401, 403, 429, 503):
            blocked = True
        if status == 200 and html:
            reached = True
            pages += 1
            html_parts.append(html)
        elif status in (404, 410) and i > 0:
            continue
        if i == 0 and not reached and not blocked:
            break            # home page dead: do not hammer subpages
        time.sleep(DELAY / 3)
    return reached, blocked, "\n".join(html_parts), pages, final_host


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl candidate sites for contacts, validated per operator")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore crawl_done.txt")
    ap.add_argument("--redo-mort", action="store_true",
                    help="re-crawl only the targets whose LAST verdict is 'mort'")
    args = ap.parse_args()
    dept = args.departement

    ops = load_operators(dept)
    matches = load_matches(dept)
    targets = collect_targets(dept, ops, matches)
    done = set() if args.force else load_done(DONE_PATH)
    todo = [(op, d, s) for op, d, s in targets if f"{dept}:{op['_id']}:{d}" not in done]
    if args.redo_mort:
        last = {}
        for v in read_csv(VERDICTS_PATH):
            last[(v["row_id"], v["domain"])] = v["verdict"]
        todo = [(op, d, s) for op, d, s in targets if last.get((op["_id"], d)) == "mort"]
    # "shared" = WE attached this domain to several operators
    claims = Counter(d for _, d, _ in targets)
    communes = frozenset(norm(op["ville"]) for op in ops)
    # phones we already trust per operator (owner-declared + matched directories)
    known_phones = defaultdict(set)
    for op in ops:
        for p in (op.get("telephone"), op.get("telephoneCommerciale")):
            if p:
                known_phones[op["_id"]].add(p)
        for m in matches.get(op["_id"], []):
            for p in (m.get("phone"), m.get("mobile")):
                if p:
                    known_phones[op["_id"]].add(p)

    if args.pilot:
        todo = todo[:args.pilot]
    elif args.limit:
        todo = todo[:args.limit]
    log.info(f"[{dept}] {len(targets)} (operator, domain) targets | {len(done)} done | {len(todo)} to crawl")

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    stats = Counter()
    n_email_ops = set()
    net_fails = 0
    for i, (op, domain, source) in enumerate(todo, 1):
        reached, blocked, html, pages, net_fail = crawl_domain(sess, domain, args.deep)
        net_fails += int(net_fail)
        # Sanity gate (the agriculture DNS lesson): if our OWN network fails
        # on most of a run, stop writing verdicts — they would be about us.
        if i >= 20 and net_fails / i > 0.5:
            log.error(f"ABORT: {net_fails}/{i} targets failed at the network level — "
                      "that is our connection, not their sites. Re-run later "
                      "(--redo-mort re-tries what was written as 'mort').")
            break
        text = strip_tags(html) if html else ""
        siret = op.get("siret") or ""
        toks = name_tokens(op["raisonSociale"], op.get("gerant", ""))
        own = ownership(text, sirets=[siret] if siret else (), sirens=[siret[:9]] if siret else (),
                        cps=[op["codePostal"]], tokens=[t for t in toks],
                        phones=list(known_phones[op["_id"]]), domain=domain,
                        names=(op["raisonSociale"], op.get("gerant", ""))) if text else "none"
        shared = claims[domain] > 1
        verdict, reason = classify(reached=reached, text=text, own=own, shared=shared,
                                   communes=communes, blocked=blocked, score_fn=agri_score)
        vrow = {"dept": dept, "row_id": op["_id"], "siret": siret,
                "raisonSociale": op["raisonSociale"], "domain": domain, "source": source,
                "reached": int(reached), "blocked": int(blocked), "verdict": verdict,
                "reason": reason, "own": own, "shared": int(shared),
                "agri_score": agri_score(text), "pages_read": pages, "text_chars": len(text)}
        append_rows(VERDICTS_PATH, VERDICT_FIELDS, [vrow])
        stats[f"verdict: {verdict}"] += 1
        if net_fail:
            stats["network failure on our side (mort)"] += 1

        crows = []
        if verdict in VERDICT_SHIPS and html:
            emails = {e for e in extract_emails(html)
                      if not is_third_party_email(e, domain)
                      and e.partition("@")[0] not in JUNK_LOCAL}
            if len(emails) > MAX_EMAILS_PER_DOMAIN:
                stats["store-list page (emails dropped)"] += 1
                emails = set()
            phones = {p for p in extract_phones_ctx(html)
                      if plausible_fr_number(p) and not is_surtaxe(p)}
            soc = extract_social(html)
            conf = site_confiance(verdict, own, reason)
            base = {"dept": dept, "row_id": op["_id"], "siret": siret,
                    "raisonSociale": op["raisonSociale"], "domain": domain,
                    "source": source, "verdict": verdict, "confiance": conf,
                    "email": "", "phone": "", "facebook": soc.get("facebook", ""),
                    "instagram": soc.get("instagram", "")}
            for e in sorted(emails):
                crows.append({**base, "email": e})
            for p in sorted(phones)[:12]:
                crows.append({**base, "phone": p})
            if not emails and not phones and (soc.get("facebook") or soc.get("instagram")):
                crows.append(base)
            if emails:
                n_email_ops.add(op["_id"])
                stats["emails kept"] += len(emails)
            if phones:
                stats["phones on page"] += len(phones)
        if crows:
            append_rows(CONTACTS_PATH, CONTACT_FIELDS, crows)
        mark_done(DONE_PATH, f"{dept}:{op['_id']}:{domain}")
        if crows or i % 20 == 0:
            log.info(f"[{i}/{len(todo)}] {op['raisonSociale'][:22]:22.22} {domain[:30]:30.30} "
                     f"{verdict:<15} {reason[:22]:22.22} e={sum(1 for r in crows if r['email'])} "
                     f"t={sum(1 for r in crows if r['phone'])}")
        time.sleep(DELAY)

    log.info("─" * 62)
    log.info(f"crawled {len(todo)} targets; operators gaining an e-mail: {len(n_email_ops)}")
    for k, v in stats.most_common():
        log.info(f"  {k:<36} {v}")
    log.info("Next: python scripts/m3ag_s11_verify.py ; then m3ag_s9_export.py")


if __name__ == "__main__":
    main()
