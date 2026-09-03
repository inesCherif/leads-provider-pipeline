"""
M3AG-S9 — Merge every enrichment source into Sam's column layout and write the deliverable
=======================================================================================
Input : checkpoints/operateurs_<dept>.csv   (m3ag_s2 — the API baseline)
        checkpoints/matched_<dept>.csv      (m3ag_s8 — PJ / OSM / bienvenue-ferme / Places)
        checkpoints/site_contacts.csv + site_verdicts.csv  (m3ag_s4 — crawled sites)
        checkpoints/search_hits.csv         (m3ag_s3 — snippet claims, one witness each)
        checkpoints/verified_emails.csv     (m3ag_s11 — SMTP/DNS verdicts)
Output: exports/agriculteurs/agriculteurs_<dept>_<version>.xlsx  and .csv

Column contract = Sam's sample, EXACT order, then V1's appended columns in
their V1 order, then the new ones — nothing an existing reader relies on
moves. Provenance is always written (`source_*`), never implied.

Precedence (measured on M2, kept):
  phone   owner-declared (agencebio) > osm > bienvenue_ferme > pagesjaunes >
          places > site/confirme > corroborated snippet. A snippet number
          is dialable ONLY when a second independent witness names it
          (another host, or any listed source); otherwise it ships in
          `telephone_piste`, never in `telephone_final`.
  e-mail  agencebio > crawled own site (valide/confirme first) > bienvenue_ferme
          > osm > pagesjaunes > places > snippet (name-carrying). Anything
          verified `invalide` is withheld outright.
  site    agencebio > places > osm > pagesjaunes > bienvenue_ferme > crawl-valide

Usage:
    python scripts/m3ag_s9_export.py --departement 63 --version v2
"""

import argparse
import csv
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m1_s8_export import ILLEGAL_XML                       # noqa: E402
from m2lib_contact import normalize_fr_phone, is_surtaxe   # noqa: E402
from m3ag_lib import (CHECK_DIR, OUT_DIR, read_csv, name_tokens, root_domain,  # noqa: E402
                      is_aggregator)

COLUMNS = ["raisonSociale", "siret", "gerant", "telephone", "telephoneCommerciale",
           "codeNAF", "siteWebs", "categories", "productions",
           "adresse", "codePostal", "ville", "lat", "lon",
           "places_phone", "places_website", "places_name", "error",
           "telephone_final", "website_final",
           # appended in V1:
           "email", "numeroBio", "activites", "flag_hors_agri",
           "pj_phone", "pj_mobile", "pj_name", "pj_method",
           "source_telephone", "source_website",
           # appended in V2:
           "email_final", "source_email", "email_statut", "emails_autres",
           "telephone_piste", "osm_phone", "osm_email", "osm_website",
           "baf_phone", "baf_email", "baf_website", "baf_contact",
           "site_confiance", "facebook", "instagram"]

PHONE_RANK = ["agencebio", "osm", "bienvenue_ferme", "pagesjaunes", "places",
              "site/confirme", "corrobore"]
EMAIL_RANK = ["agencebio", "site/confirme", "site/probable", "bienvenue_ferme",
              "osm", "pagesjaunes", "places", "site/non verifie", "snippet"]
SITE_RANK = ["agencebio", "places", "osm", "pagesjaunes", "bienvenue_ferme", "crawl"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s9")


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v if v is not None else ""))


def nphone(p: str) -> str:
    return normalize_fr_phone(p or "") or ""


def rank(order: list, key: str) -> int:
    return order.index(key) if key in order else len(order)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departement", default="63")
    ap.add_argument("--version", default="v2")
    args = ap.parse_args()
    dept = args.departement

    ours_path = CHECK_DIR / f"operateurs_{dept}.csv"
    if not ours_path.exists():
        sys.exit(f"{ours_path} missing — run m3ag_s2_transform.py first.")
    with ours_path.open(encoding="utf-8-sig", newline="") as fh:
        ops = list(csv.DictReader(fh))

    # ---- sources, all keyed by row_id --------------------------------------
    matches = defaultdict(lambda: defaultdict(list))        # rid -> source -> [rows]
    for m in read_csv(CHECK_DIR / f"matched_{dept}.csv"):
        matches[m["row_id"]][m["source"]].append(m)
    site_contacts = defaultdict(list)
    for r in read_csv(CHECK_DIR / "site_contacts.csv"):
        if r.get("dept", dept) == dept:
            site_contacts[r["row_id"]].append(r)
    last_verdict = {}
    for r in read_csv(CHECK_DIR / "site_verdicts.csv"):
        if r.get("dept", dept) == dept:
            last_verdict[(r["row_id"], r["domain"])] = r      # redo runs append: last wins
    site_verdicts = defaultdict(list)
    for r in last_verdict.values():
        if r["verdict"] == "valide":
            site_verdicts[r["row_id"]].append(r)
    hits = defaultdict(list)
    for r in read_csv(CHECK_DIR / "search_hits.csv"):
        hits[r["row_id"]].append(r)
    verified = {r["email"].lower(): r["verdict"] for r in read_csv(CHECK_DIR / "verified_emails.csv")}

    rows = []
    stats = Counter()
    for op in ops:
        rid = op["siret"] or f"NB{op['numeroBio']}"
        ms = matches.get(rid, {})
        toks = name_tokens(op["raisonSociale"], op.get("gerant", ""))

        # ---------------- phones: (phone, source) candidates ----------------
        cands = []          # dialable
        for p in (op["telephone"], op["telephoneCommerciale"]):
            if nphone(p):
                cands.append((nphone(p), "agencebio"))
        for src in ("osm", "bienvenue_ferme", "pagesjaunes", "places"):
            for m in ms.get(src, []):
                for p in (m.get("phone"), m.get("mobile")):
                    if nphone(p):
                        cands.append((nphone(p), src))
        for c in site_contacts.get(rid, []):
            if nphone(c.get("phone")) and c.get("confiance") == "confirme":
                cands.append((nphone(c["phone"]), "site/confirme"))
        # snippet claims: witness = host root; dialable only with 2 witnesses
        witnesses = defaultdict(set)
        for h in hits.get(rid, []):
            for p in (h.get("phones") or "").split("|"):
                if nphone(p) and not is_surtaxe(nphone(p)):
                    witnesses[nphone(p)].add(root_domain(h["host"]))
        listed = {p for p, _ in cands}
        pistes = []
        for p, ws in witnesses.items():
            if p in listed:
                continue                      # already dialable from a source
            if len(ws) >= 2:
                cands.append((p, f"corrobore({'+'.join(sorted(ws)[:3])})"))
            else:
                pistes.append(f"{p} ({next(iter(ws))})")
        cands.sort(key=lambda x: rank(PHONE_RANK, x[1].split("(")[0]))
        tel_final, tel_src = (cands[0] if cands else ("", ""))
        # a listed phone also named by a snippet is worth recording as support
        if tel_final and witnesses.get(tel_final):
            stats["phone corroborated by a snippet"] += 1

        # ---------------- e-mails --------------------------------------------
        ecands = []         # (email, source)
        if op["email"]:
            ecands.append((op["email"].lower().strip(), "agencebio"))
        for c in site_contacts.get(rid, []):
            e = (c.get("email") or "").lower().strip()
            if e:
                conf = c.get("confiance") or "non verifie"
                ecands.append((e, f"site/{conf}"))
        for src in ("bienvenue_ferme", "osm", "pagesjaunes", "places"):
            for m in ms.get(src, []):
                e = (m.get("email") or "").lower().strip()
                if e:
                    ecands.append((e, src))
        for h in hits.get(rid, []):
            for e in (h.get("emails") or "").split("|"):
                if e.strip():
                    ecands.append((e.strip().lower(), "snippet"))
        seen_e, uniq = set(), []
        for e, s in sorted(ecands, key=lambda x: rank(EMAIL_RANK, x[1])):
            if e in seen_e:
                continue
            if verified.get(e) == "invalide":
                stats["e-mail withheld: invalide"] += 1
                continue
            if is_aggregator(e.partition("@")[2]) and e.partition("@")[2] not in ("gmail.com",):
                stats["e-mail withheld: aggregator domain"] += 1
                continue
            seen_e.add(e)
            uniq.append((e, s))
        # within the crawled-site tier prefer an address carrying the farm's name
        def named(e):
            flat = e.partition("@")[0].replace(".", "").replace("-", "").replace("_", "")
            return any(t.lower() in flat for t in toks if len(t) >= 4)
        uniq.sort(key=lambda x: (rank(EMAIL_RANK, x[1]), 0 if named(x[0]) else 1))
        email_final, email_src = (uniq[0] if uniq else ("", ""))
        email_statut = verified.get(email_final, "") if email_final else ""
        if email_final and email_src == "agencebio" and not email_statut:
            email_statut = "declare"
        emails_autres = "|".join(e for e, _ in uniq[1:4])

        # ---------------- sites --------------------------------------------
        scands = []
        if op["siteWebs"]:
            scands.append((op["siteWebs"].split("; ")[0], "agencebio"))
        for src in ("places", "osm", "pagesjaunes", "bienvenue_ferme"):
            for m in ms.get(src, []):
                if m.get("website") and not is_aggregator(m["website"]):
                    scands.append((m["website"], src))
        for v in site_verdicts.get(rid, []):
            if v["source"].startswith("search"):
                scands.append((f"https://{v['domain']}", "crawl"))
        scands.sort(key=lambda x: rank(SITE_RANK, x[1]))
        site_final, site_src = (scands[0] if scands else ("", ""))
        conf = ""
        for c in site_contacts.get(rid, []):
            if site_final and c["domain"] in site_final:
                conf = c.get("confiance") or conf
        for v in site_verdicts.get(rid, []):
            if site_final and v["domain"] in site_final and not conf:
                conf = "probable"

        # ---------------- socials --------------------------------------------
        fb = ig = ""
        for c in site_contacts.get(rid, []):
            fb = fb or c.get("facebook", "")
            ig = ig or c.get("instagram", "")
        for h in hits.get(rid, []):
            if h["kind"] == "social" and h["geo"] == "cp":
                if "facebook.com" in h["host"] and not fb:
                    fb = h["url"]
                if "instagram.com" in h["host"] and not ig:
                    ig = h["url"]

        pj =(ms.get("pagesjaunes") or [{}])[0]
        osm = (ms.get("osm") or [{}])[0]
        baf = (ms.get("bienvenue_ferme") or [{}])[0]
        pl = (ms.get("places") or [{}])[0]

        row = {c: "" for c in COLUMNS}
        for c in ("raisonSociale", "siret", "gerant", "telephone",
                  "telephoneCommerciale", "codeNAF", "siteWebs", "categories",
                  "productions", "adresse", "codePostal", "ville", "lat", "lon",
                  "email", "numeroBio", "activites", "flag_hors_agri"):
            row[c] = clean(op.get(c, ""))
        row.update({
            "places_phone": clean(pl.get("phone", "")), "places_website": clean(pl.get("website", "")),
            "places_name": clean(pl.get("listing_name", "")),
            "telephone_final": tel_final, "website_final": clean(site_final),
            "pj_phone": clean(pj.get("phone", "")), "pj_mobile": clean(pj.get("mobile", "")),
            "pj_name": clean(pj.get("listing_name", "")), "pj_method": clean(pj.get("method", "")),
            "source_telephone": tel_src, "source_website": site_src,
            "email_final": email_final, "source_email": email_src,
            "email_statut": email_statut, "emails_autres": emails_autres,
            "telephone_piste": " | ".join(pistes[:3]),
            "osm_phone": clean(osm.get("phone", "")), "osm_email": clean(osm.get("email", "")),
            "osm_website": clean(osm.get("website", "")),
            "baf_phone": clean(baf.get("phone", "")), "baf_email": clean(baf.get("email", "")),
            "baf_website": clean(baf.get("website", "")), "baf_contact": clean(baf.get("listing_name", "")),
            "site_confiance": conf, "facebook": clean(fb), "instagram": clean(ig),
        })
        rows.append(row)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"agriculteurs_{dept}_{args.version}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    try:
        from openpyxl import Workbook
    except ImportError:
        sys.exit("openpyxl required: pip install openpyxl")
    xlsx_path = OUT_DIR / f"agriculteurs_{dept}_{args.version}.xlsx"
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Agriculteurs")
    ws.append(COLUMNS)
    for r in rows:
        ws.append([str(r[c]) if c in ("siret", "codePostal") and r[c] else r[c] for c in COLUMNS])
    wb.save(xlsx_path)

    n = len(rows)
    tel = sum(1 for r in rows if r["telephone_final"])
    mail = sum(1 for r in rows if r["email_final"])
    site = sum(1 for r in rows if r["website_final"])
    reach = sum(1 for r in rows if r["telephone_final"] or r["email_final"])
    reach_site = sum(1 for r in rows if r["telephone_final"] or r["email_final"] or r["website_final"])
    piste = sum(1 for r in rows if r["telephone_piste"])
    log.info("─" * 62)
    log.info(f"[{dept}] {n} rows -> {xlsx_path.name} + .csv")
    log.info(f"  telephone_final   {tel:>5}  ({tel/n:.1%})  by source: "
             f"{dict(Counter(r['source_telephone'].split('(')[0] for r in rows if r['telephone_final']))}")
    log.info(f"  email_final       {mail:>5}  ({mail/n:.1%})  by source: "
             f"{dict(Counter(r['source_email'] for r in rows if r['email_final']))}")
    log.info(f"  website_final     {site:>5}  ({site/n:.1%})")
    log.info(f"  joignables        {reach:>5}  ({reach/n:.1%})  (phone OR email)   "
             f"| incl. site-only {reach_site} ({reach_site/n:.1%})")
    log.info(f"  telephone_piste   {piste:>5}  one-witness claims, NOT dialled")
    for k, v in stats.most_common():
        log.info(f"  {k:<36} {v}")


if __name__ == "__main__":
    main()
