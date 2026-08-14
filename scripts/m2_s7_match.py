"""
M2-S7 — Match external listings (OSM, Pages Jaunes, …) to our SIRET rows
=========================================================================
Takes every listing checkpoint written by the harvesters and decides which
company row, if any, it describes. Emits

    exports/boulangerie/checkpoints/matched.csv

with one row per accepted (listing -> siret) pair, carrying the phone, email,
website and facebook the listing supplied, plus the method that accepted it.

The discipline is copied from m1_s9k_match_listings.py, because it was paid
for: on agriculture, 9 listings matched a business name exactly while the
commune disagreed, and 4 of those would have written a STRANGER'S EMAIL into
the client's file. The rules, strongest first:

  1. siret_exact     — the listing carries ref:FR:SIRET and it is one of ours.
                       No location test needed; the identifier IS the location.
  2. geo_name        — coordinates within GEO_TIGHT_M and the names agree.
  3. geo_only        — coordinates within GEO_STRICT_M (very close) even if
                       the names differ: a rename is common, a second bakery
                       30 m away is not.
  4. name_commune    — exact normalised name AND same commune (or same CP).
  5. name_fuzzy      — token-overlap ratio >= FUZZY_MIN AND same commune/CP.

Two invariants, both non-negotiable:
  * AMBIGUITY IS REJECTION. A listing that fits two different SIRETs equally
    well is dropped, not guessed. Same for a SIRET claimed by two listings at
    the same strength — the stronger method wins, a tie loses.
  * NO MATCH WITHOUT LOCATION AGREEMENT (rule 1 excepted, where the SIRET is
    itself the proof). A name alone never carries contact data onto a row.

Usage:
    python scripts/m2_s7_match.py
    python scripts/m2_s7_match.py --report-rejects   # show what was dropped and why
"""

import argparse
import csv
import logging
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
OUT_PATH  = CHECK_DIR / "matched.csv"

# Listing sources: (filename, source label). Missing files are skipped with a
# notice — this script must run before every harvester exists.
SOURCES = [
    ("osm_listings.csv",    "osm"),
    ("pj_listings.csv",     "pagesjaunes"),
    ("places_listings.csv", "serper_places"),   # m2_s13 Google Maps sweep
]

GEO_STRICT_M = 40.0    # same point, names may differ (rename/rebrand)
GEO_TIGHT_M  = 150.0   # same street corner, names must agree
FUZZY_MIN    = 0.62    # token-overlap ratio for name_fuzzy

# Words that carry no identity: every bakery is a "boulangerie". Leaving them
# in makes "BOULANGERIE MARTIN" and "BOULANGERIE DURAND" look 50% similar.
STOPWORDS = {
    "boulangerie", "patisserie", "boulangeries", "patisseries", "boulanger",
    "patissier", "la", "le", "les", "de", "du", "des", "d", "l", "et", "au",
    "aux", "chez", "sarl", "sas", "sasu", "eurl", "snc", "sci", "ets", "maison",
    "fournil", "artisan", "artisanale", "artisanal",
}

FIELDNAMES = ["siret", "siren", "raison_sociale", "commune", "code_postal",
              "source", "method", "distance_m", "listing_name",
              "phone", "email", "website", "facebook", "listing_id"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s7")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s).upper()
    return " ".join(s.split())


def tokens(s: str) -> set:
    return {t for t in norm(s).lower().split() if t and t not in STOPWORDS and len(t) > 1}


def ratio(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def name_score(nm: set, r: dict) -> float:
    """Best overlap against the merged bag OR either name alone."""
    best = ratio(nm, r["_tokens"])
    for tk in r.get("_tok_alt", ()):
        s = ratio(nm, tk)
        if s > best:
            best = s
    return best


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def load_listings() -> list[dict]:
    out = []
    for fname, label in SOURCES:
        p = CHECK_DIR / fname
        if not p.exists():
            log.info(f"(skip) {fname} not present yet — source '{label}' not harvested")
            continue
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                r["_source"] = label
                out.append(r)
        log.info(f"loaded {fname}: source '{label}'")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Match listings to SIRET rows")
    ap.add_argument("--report-rejects", action="store_true")
    args = ap.parse_args()

    if not OURS_PATH.exists():
        sys.exit(f"{OURS_PATH} not found — run scripts/m2_s2_transform.py first.")
    with OURS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        ours = list(csv.DictReader(fh, delimiter=";"))
    listings = load_listings()
    if not listings:
        sys.exit("No listing files found. Run scripts/m2_s5_osm.py first.")

    by_siret = {r["siret"]: r for r in ours}
    by_commune = defaultdict(list)
    by_cp = defaultdict(list)
    for r in ours:
        # Merged bag for exact comparison, but ALSO each name on its own:
        # a directory prints the TRADE name ("Le Bar a Pain") while the
        # registry holds the LEGAL one ("BOULANGERIE PATISSERIE DUPONT ET
        # FILS"). Merging them dilutes the score — the trade name matches
        # perfectly yet lands at 2/4 = 0.50 and is rejected. Scoring the
        # names separately and keeping the best is what a human would do.
        r["_tokens"] = tokens(f"{r['raison_sociale']} {r['enseigne']}")
        r["_tok_alt"] = [tk for tk in (tokens(r["raison_sociale"]),
                                       tokens(r["enseigne"])) if tk]
        r["_lat"], r["_lon"] = fnum(r["latitude"]), fnum(r["longitude"])
        by_commune[norm(r["commune"])].append(r)
        by_cp[r["code_postal"]].append(r)

    accepted: dict[tuple, dict] = {}    # (siret, source) -> best row
    rejects = Counter()
    method_counts = Counter()
    METHOD_RANK = {"siret_exact": 0, "geo_only": 1, "geo_name": 2,
                   "name_commune": 3, "name_fuzzy": 4}

    for L in listings:
        src = L["_source"]
        name = L.get("name") or L.get("listing_name") or ""
        lat, lon = fnum(L.get("lat")), fnum(L.get("lon"))
        cp = (L.get("postcode") or L.get("code_postal") or "").strip()[:5]
        commune = norm(L.get("city") or L.get("commune") or "")
        payload = {
            "phone":    (L.get("phone") or "").strip(),
            "email":    (L.get("email") or "").strip(),
            "website":  (L.get("website") or "").strip(),
            "facebook": (L.get("facebook") or "").strip(),
        }
        if not any(payload.values()):
            rejects["no contact data in listing"] += 1
            continue

        cand = None      # (method, siret, distance)

        # 1. SIRET tag — the identifier is its own proof of location.
        s = digits(L.get("siret", ""))
        if len(s) == 14 and s in by_siret:
            cand = ("siret_exact", s, None)

        # 2/3. Geographic. Scan only rows in the same commune or CP when we
        # have one; otherwise the whole set (1.7k rows, cheap).
        if cand is None and lat is not None and lon is not None:
            pool = by_commune.get(commune) or by_cp.get(cp) or ours
            near = []
            for r in pool:
                if r["_lat"] is None:
                    continue
                d = haversine_m(lat, lon, r["_lat"], r["_lon"])
                if d <= GEO_TIGHT_M:
                    near.append((d, r))
            near.sort(key=lambda x: x[0])
            if near:
                nm = tokens(name)
                named_hits = [(d, r) for d, r in near if name_score(nm, r) >= FUZZY_MIN]
                if named_hits:
                    # Ambiguity: two different companies both close AND named alike.
                    if len({r["siret"] for _, r in named_hits}) > 1 and \
                       abs(named_hits[0][0] - named_hits[1][0]) < 5:
                        rejects["ambiguous: 2+ companies match by geo+name"] += 1
                        continue
                    d, r = named_hits[0]
                    cand = ("geo_name", r["siret"], d)
                else:
                    very_near = [(d, r) for d, r in near if d <= GEO_STRICT_M]
                    if len({r["siret"] for _, r in very_near}) == 1:
                        d, r = very_near[0]
                        cand = ("geo_only", r["siret"], d)
                    elif len(very_near) > 1:
                        rejects["ambiguous: 2+ companies within 40 m"] += 1
                        continue

        # 4/5. Name, but ONLY with location agreement.
        if cand is None and name:
            # TIGHTEST POOL FIRST, then widen. A directory prints the city as
            # "Marseille", so the commune pool holds ~618 rows and honest
            # listings die as "two equally-good fuzzy names"; the postcode
            # pool is one arrondissement. But the two postcodes legitimately
            # disagree — a company's registered address is often a different
            # arrondissement from its shop — so the commune pool must remain
            # the fallback, or correct matches are lost (measured: LE TRIANON,
            # listed 13001, registered elsewhere in Marseille).
            pools = [p for p in (by_cp.get(cp), by_commune.get(commune)) if p]
            if not pools:
                rejects["no location on listing — name alone is not enough"] += 1
                continue
            nm = tokens(name)
            if not nm:
                rejects["listing name is only generic words"] += 1
                continue
            why = ""
            for pool in pools:
                exact = [r for r in pool if r["_tokens"] == nm or nm in r["_tok_alt"]]
                if len({r["siret"] for r in exact}) == 1:
                    cand = ("name_commune", exact[0]["siret"], None)
                    break
                if len(exact) > 1:
                    why = "ambiguous: same name twice in the commune"
                    continue          # a wider pool cannot disambiguate this
                scored = sorted(((name_score(nm, r), r) for r in pool),
                                key=lambda x: -x[0])
                if scored and scored[0][0] >= FUZZY_MIN:
                    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.02:
                        why = "ambiguous: two equally-good fuzzy names"
                        continue
                    cand = ("name_fuzzy", scored[0][1]["siret"], None)
                    break
            if cand is None and why:
                rejects[why] += 1
                continue

        if cand is None:
            rejects["no acceptable match"] += 1
            continue

        method, siret, dist = cand
        r = by_siret[siret]
        row = {
            "siret": siret, "siren": r["siren"],
            "raison_sociale": r["raison_sociale"], "commune": r["commune"],
            "code_postal": r["code_postal"], "source": src, "method": method,
            "distance_m": f"{dist:.0f}" if dist is not None else "",
            "listing_name": name, **payload,
            "listing_id": L.get("osm_id") or L.get("listing_id") or "",
        }
        key = (siret, src)
        prev = accepted.get(key)
        if prev is None or METHOD_RANK[method] < METHOD_RANK[prev["method"]]:
            accepted[key] = row
        else:
            rejects["duplicate: same siret already matched from this source"] += 1

    rows = sorted(accepted.values(), key=lambda r: (r["code_postal"], r["siret"], r["source"]))
    for r in rows:
        method_counts[f"{r['source']}/{r['method']}"] += 1

    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    n_biz = len({r["siret"] for r in rows})
    log.info("─" * 62)
    log.info(f"listings considered      {len(listings):>6}")
    log.info(f"accepted matches         {len(rows):>6}  covering {n_biz} distinct businesses "
             f"({n_biz/len(ours):.1%} of {len(ours)})")
    for k, v in sorted(method_counts.items()):
        log.info(f"    {k:<28} {v:>5}")
    log.info(f"  new phones             {sum(1 for r in rows if r['phone']):>6}")
    log.info(f"  new EMAILS             {sum(1 for r in rows if r['email']):>6}")
    log.info(f"  new websites           {sum(1 for r in rows if r['website']):>6}")
    log.info(f"  facebook pages         {sum(1 for r in rows if r['facebook']):>6}")
    log.info(f"rejected                 {sum(rejects.values()):>6}")
    if args.report_rejects:
        for k, v in rejects.most_common():
            log.info(f"    {k:<52} {v:>5}")
    else:
        log.info("    (--report-rejects to see why)")
    log.info(f"written={len(rows)} -> {OUT_PATH}")


if __name__ == "__main__":
    main()
