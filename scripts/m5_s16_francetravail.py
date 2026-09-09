"""
M5-S16 — France Travail job offers: the annonce Maha named (alive proof + owner-declared contact)
===============================================================================================
A lodging that publishes a job offer is open, hiring, and has typed its own
phone or e-mail into the offer. The Offres d'emploi v2 API (francetravail.io,
free developer account, OAuth2 client credentials) returns every ACTIVE offer
with entreprise.nom / entreprise.url, contact.telephone / contact.courriel,
lieuTravail (CP, commune, lat/lon), dateCreation, codeNAF and the origin URL.

Queries per département: codeNAF (55.20Z, 55.30Z) then keyword sweeps
(camping, gîte, chambres d'hôtes, village vacances, hôtellerie de plein air).
The API only lists offers that are active NOW (`publieeDepuis` accepts 1/3/7/
14/31 days at most), so this script APPENDS: run it monthly and the file
accumulates the season (peaks February–May). Offer id is the dedup key.

Credentials in .env: FT_CLIENT_ID, FT_CLIENT_SECRET. Never printed.

Output: exports/hebergement/checkpoints/ft_offres.csv  (';')
        dept, listing_id (offer id), name, phone, email, website, address,
        postcode, city, lat, lon, naf, intitule, type_contrat, date_creation,
        date_actualisation, contact_nom, url, query

Usage:
    python scripts/m5_s16_francetravail.py --pilot      # one query, prints fill rates
    python scripts/m5_s16_francetravail.py
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone   # noqa: E402
from m5_lib import CHECK_DIR, DEPARTEMENTS, read_csv, append_rows   # noqa: E402

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"
OUT_PATH = CHECK_DIR / "ft_offres.csv"
NAFS = ("55.20Z", "55.30Z")
KEYWORDS = ("camping", "gîte", "chambres d'hôtes", "village vacances", "hôtellerie de plein air",
            "hébergement touristique", "résidence de tourisme")
PAGE = 150
MAX_START = 1000            # the API caps range at 1000-1149
DELAY = 0.35

FIELDS = ["dept", "listing_id", "name", "phone", "mobile", "email", "website", "address", "postcode", "city",
          "lat", "lon", "naf", "intitule", "type_contrat", "date_creation", "date_actualisation",
          "contact_nom", "url", "query", "harvested_at"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m5_s16")


def env(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not v:
        sys.exit(f"{key} missing from .env")
    return v


def token() -> str:
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": env("FT_CLIENT_ID"),
                                   "client_secret": env("FT_CLIENT_SECRET"), "scope": SCOPE}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"token refused: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]} — "
                 "check FT_CLIENT_ID/FT_CLIENT_SECRET and that the app has the Offres d'emploi v2 API.")


def search(tok: str, params: dict):
    """Yield offers for one query, paging by range until the API's cap."""
    start = 0
    while start <= MAX_START:
        p = {**params, "range": f"{start}-{start + PAGE - 1}"}
        req = urllib.request.Request(SEARCH_URL + "?" + urllib.parse.urlencode(p),
                                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                data = json.loads(resp.read() or b"{}")
                content_range = resp.headers.get("Content-Range", "")
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return
            if e.code == 429:
                time.sleep(2)
                continue
            log.warning(f"HTTP {e.code} on {p}: {e.read().decode('utf-8', 'replace')[:150]}")
            return
        results = data.get("resultats") or []
        yield from results
        total = 0
        if "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[1])
            except ValueError:
                total = 0
        if len(results) < PAGE or status == 200 or (total and start + PAGE >= total):
            return
        start += PAGE
        time.sleep(DELAY)


def row_of(o: dict, dept: str, query: str) -> dict:
    ent, ct, lieu, orig = o.get("entreprise") or {}, o.get("contact") or {}, o.get("lieuTravail") or {}, o.get("origineOffre") or {}
    phone = normalize_fr_phone(ct.get("telephone") or "") or ""
    cp = (lieu.get("codePostal") or "")
    return {
        "dept": dept, "listing_id": f"FT{o.get('id')}", "name": (ent.get("nom") or "").strip(),
        "phone": phone if not phone.startswith(("06", "07")) else "", "mobile": phone if phone.startswith(("06", "07")) else "",
        "email": (ct.get("courriel") or "").strip().lower(), "website": (ent.get("url") or "").strip(),
        "address": (ct.get("coordonnees1") or "").strip(), "postcode": cp,
        "city": (lieu.get("libelle") or "").split(" - ", 1)[-1].strip(),
        "lat": lieu.get("latitude") or "", "lon": lieu.get("longitude") or "",
        "naf": o.get("codeNAF") or "", "intitule": (o.get("intitule") or "").strip(),
        "type_contrat": o.get("typeContrat") or "", "date_creation": (o.get("dateCreation") or "")[:10],
        "date_actualisation": (o.get("dateActualisation") or "")[:10],
        "contact_nom": (ct.get("nom") or "").strip(),
        "url": orig.get("urlOrigine") or f"https://candidat.francetravail.fr/offres/recherche/detail/{o.get('id')}",
        "query": query, "harvested_at": time.strftime("%Y-%m-%d"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default=",".join(DEPARTEMENTS))
    ap.add_argument("--pilot", action="store_true", help="one query (dept × 55.30Z), print fill rates")
    args = ap.parse_args()
    depts = [d.strip() for d in args.departements.split(",") if d.strip()]

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    existing = {r["listing_id"] for r in read_csv(OUT_PATH)}
    tok = token()
    log.info(f"token OK; {len(existing)} offers already on disk")
    stats = Counter()
    seen_run = set()
    for dept in depts:
        queries = [("naf", {"departement": dept, "codeNAF": ",".join(NAFS)})]
        if not args.pilot:
            queries += [(f"mot:{k}", {"departement": dept, "motsCles": k}) for k in KEYWORDS]
        for label, params in queries:
            rows, n_seen = [], 0
            for o in search(tok, params):
                n_seen += 1
                r = row_of(o, dept, label)
                if r["listing_id"] in seen_run:
                    continue
                seen_run.add(r["listing_id"])
                stats["offers"] += 1
                stats["with phone"] += bool(r["phone"] or r["mobile"])
                stats["with e-mail"] += bool(r["email"])
                stats["with website"] += bool(r["website"])
                stats["with company name"] += bool(r["name"])
                if r["listing_id"] not in existing:
                    rows.append(r)
                    existing.add(r["listing_id"])
            append_rows(OUT_PATH, FIELDS, rows)
            log.info(f"[{dept}] {label:<28} offers {n_seen:>4}  new on disk {len(rows):>4}")
            time.sleep(DELAY)
        if args.pilot:
            break
    log.info("─" * 62)
    n = max(1, stats["offers"])
    log.info(f"distinct offers this run {stats['offers']}: phone {stats['with phone']} ({stats['with phone']/n:.0%}), "
             f"e-mail {stats['with e-mail']} ({stats['with e-mail']/n:.0%}), website {stats['with website']}, "
             f"company named {stats['with company name']}")
    allrows = read_csv(OUT_PATH)
    log.info(f"file now holds {len(allrows)} offers; by dept {dict(Counter(r['dept'] for r in allrows))}; "
             f"by NAF {dict(Counter(r['naf'] for r in allrows).most_common(6))}")


if __name__ == "__main__":
    main()
