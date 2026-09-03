"""
M3AG-S8 — Match harvested listings (PJ, OSM, …) to Agence Bio operators
=======================================================================
Adapted 2026-09-02 from the proven `m2_s7_match.py`. Same non-negotiables:

  * AMBIGUITY IS REJECTION — a listing that fits two operators equally well
    is dropped, not guessed.
  * NO MATCH WITHOUT LOCATION AGREEMENT — a name alone never carries contact
    data onto a row (on agriculture-M1 this rule stopped 4 stranger e-mails).

Rungs, strongest first (m2's order kept):
  1. siret_exact   — the listing carries one of our SIRETs.
  2. geo_name      — listing has lat/lon within GEO_TIGHT_M and names agree
                     (OSM later; PJ prints no coordinates).
  3. geo_only      — within GEO_STRICT_M even if names differ.
  4. addr_street   — same house number + street tokens + CP, NO name test
                     (PJ prints the trade name, the registry the legal one).
                     Siblings at one address: the listing name may single one
                     out (addr_name); a nameless tie is REJECTED here because
                     operateurs_*.csv carries no registration date to break
                     it with (m2's recency rule needs dates we don't have).
  5. name_commune / name_fuzzy — name agreement inside the CP/commune pool.

Sector twists vs m2:
  * The operator's GERANT is scored as an alternative name: PJ lists farmers
    as people ("René Prononce") while the registry may hold "EARL PRONONCE".
  * Farm stopwords (gaec, earl, ferme, domaine…) replace bakery ones.
  * Operators without a SIRET (3%) are still matchable — rows are keyed by
    siret when present, else "NB<numeroBio>".

Usage:
    python scripts/m3ag_s8_match.py --departement 63
    python scripts/m3ag_s8_match.py --departement 63 --report-rejects
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
CHECK_DIR = PROJECT_ROOT / "exports" / "agriculteurs" / "checkpoints"

SOURCES = [
    ("pj_listings.csv",     "pagesjaunes",     ";"),
    ("osm_listings.csv",    "osm",             ";"),
    ("baf_listings.csv",    "bienvenue_ferme", ";"),   # m3ag_s10
    ("places_listings.csv", "places",          ";"),   # m3ag_s5
]

GEO_STRICT_M = 40.0
GEO_TIGHT_M  = 150.0
FUZZY_MIN    = 0.62
ADDR_MIN     = 0.6

STOPWORDS = {
    "gaec", "earl", "scea", "sarl", "sas", "sasu", "eurl", "sci", "snc", "ets",
    "exploitation", "agricole", "ferme", "domaine", "elevage", "earl", "ea",
    "la", "le", "les", "de", "du", "des", "d", "l", "et", "au", "aux", "chez",
    "bio", "biologique", "monsieur", "madame", "m", "mme",
}

FIELDNAMES = ["row_id", "siret", "raisonSociale", "ville", "codePostal",
              "source", "method", "distance_m", "listing_name",
              "phone", "mobile", "email", "website", "listing_id"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m3ag_s8")


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
    best = ratio(nm, r["_tokens"])
    for tk in r.get("_tok_alt", ()):
        s = ratio(nm, tk)
        if s > best:
            best = s
    return best


WAY_TYPES = {
    "AV": "AVENUE", "AVE": "AVENUE", "AVN": "AVENUE",
    "BD": "BOULEVARD", "BLD": "BOULEVARD", "BLVD": "BOULEVARD", "BOUL": "BOULEVARD",
    "CRS": "COURS", "CHE": "CHEMIN", "CHEM": "CHEMIN", "CH": "CHEMIN",
    "PL": "PLACE", "RTE": "ROUTE", "IMP": "IMPASSE", "ALL": "ALLEE",
    "TRA": "TRAVERSE", "TRAV": "TRAVERSE", "SQ": "SQUARE", "QU": "QUAI",
    "MTE": "MONTEE", "PAS": "PASSAGE", "RES": "RESIDENCE", "LOT": "LOTISSEMENT",
    "ST": "SAINT", "STE": "SAINTE", "R": "RUE", "VOIE": "VOIE",
}
ADDR_NOISE = {"BIS", "TER", "QUATER"}


def addr_parse(raw: str) -> tuple:
    s = re.sub(r"\b\d{5}\b.*$", " ", raw or "")
    s = norm(s)
    if not s:
        return "", set()
    parts = s.split()
    m = re.match(r"^(\d+)", parts[0])
    if not m:
        return "", set()
    house = m.group(1)
    out = set()
    for t in parts[1:]:
        if t.isdigit() or t in ADDR_NOISE:
            continue
        t = WAY_TYPES.get(t, t)
        if len(t) > 1:
            out.add(t.lower())
    return house, out


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
    for fname, label, delim in SOURCES:
        p = CHECK_DIR / fname
        if not p.exists():
            log.info(f"(skip) {fname} not present yet — source '{label}' not harvested")
            continue
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=delim):
                r["_source"] = label
                out.append(r)
        log.info(f"loaded {fname}: source '{label}'")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Match listings to Agence Bio operators")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--report-rejects", action="store_true")
    args = ap.parse_args()
    dept = args.departement

    ours_path = CHECK_DIR / f"operateurs_{dept}.csv"
    out_path  = CHECK_DIR / f"matched_{dept}.csv"
    if not ours_path.exists():
        sys.exit(f"{ours_path} not found — run m3ag_s2_transform.py first.")
    with ours_path.open(encoding="utf-8-sig", newline="") as fh:
        ours = list(csv.DictReader(fh))
    listings = [L for L in load_listings()
                if (L.get("postcode") or L.get("codePostal") or "").startswith(dept)
                or not (L.get("postcode") or "")]
    if not listings:
        sys.exit("No listing files found. Run m3ag_s7_pagesjaunes.py first.")

    by_id = {}
    by_siret = {}
    by_commune = defaultdict(list)
    by_cp = defaultdict(list)
    for r in ours:
        r["_id"] = r["siret"] or f"NB{r['numeroBio']}"
        by_id[r["_id"]] = r
        if r["siret"]:
            by_siret[r["siret"]] = r
        # raisonSociale AND gerant scored separately (the trade-vs-legal-name
        # lesson): PJ lists "René Prononce", the registry "EARL PRONONCE".
        r["_tokens"] = tokens(f"{r['raisonSociale']} {r['gerant']}")
        r["_tok_alt"] = [tk for tk in (tokens(r["raisonSociale"]),
                                       tokens(r["gerant"])) if tk]
        r["_lat"], r["_lon"] = fnum(r["lat"]), fnum(r["lon"])
        r["_house"], r["_street"] = addr_parse(r.get("adresse", ""))
        by_commune[norm(r["ville"])].append(r)
        by_cp[r["codePostal"]].append(r)

    accepted: dict[tuple, dict] = {}
    rejects = Counter()
    method_counts = Counter()
    METHOD_RANK = {"siret_exact": 0, "geo_only": 1, "geo_name": 2,
                   "addr_street": 3, "addr_name": 4, "name_commune": 5,
                   "name_fuzzy": 6}

    for L in listings:
        src = L["_source"]
        name = L.get("name") or ""
        lat, lon = fnum(L.get("lat")), fnum(L.get("lon"))
        cp = (L.get("postcode") or "").strip()[:5]
        commune = norm(L.get("city") or "")
        if not cp:
            # Rows harvested before the CP parser went dept-agnostic: recover
            # the location from the raw address string.
            mm = re.search(r"\b(\d{5})\b\s*([A-Za-zÀ-ÿ' \-]*)", L.get("address") or "")
            if mm:
                cp = mm.group(1)
                commune = commune or norm(mm.group(2))
        payload = {
            "phone":   (L.get("phone") or "").strip(),
            "mobile":  (L.get("mobile") or "").strip(),
            "email":   (L.get("email") or "").strip(),
            "website": (L.get("website") or "").strip(),
        }
        if not any(payload.values()):
            rejects["no contact data in listing"] += 1
            continue

        cand = None      # (method, row_id, distance)

        s = digits(L.get("siret", ""))
        if len(s) == 14 and s in by_siret:
            cand = ("siret_exact", by_siret[s]["_id"], None)

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
                    if len({r["_id"] for _, r in named_hits}) > 1 and \
                       abs(named_hits[0][0] - named_hits[1][0]) < 5:
                        rejects["ambiguous: 2+ operators match by geo+name"] += 1
                        continue
                    d, r = named_hits[0]
                    cand = ("geo_name", r["_id"], d)
                else:
                    very_near = [(d, r) for d, r in near if d <= GEO_STRICT_M]
                    if len({r["_id"] for _, r in very_near}) == 1:
                        d, r = very_near[0]
                        cand = ("geo_only", r["_id"], d)
                    elif len(very_near) > 1:
                        rejects["ambiguous: 2+ operators within 40 m"] += 1
                        continue

        addr_raw = (L.get("address") or "").strip()
        if cand is None and addr_raw and cp:
            l_house, l_street = addr_parse(addr_raw)
            if l_house and l_street:
                hits = [r for r in by_cp.get(cp, [])
                        if r["_house"] == l_house
                        and ratio(l_street, r["_street"]) >= ADDR_MIN]
                ids = {r["_id"] for r in hits}
                if len(ids) == 1:
                    cand = ("addr_street", hits[0]["_id"], None)
                elif len(ids) > 1:
                    # Siblings at one address: the listing name may single one
                    # out. No registration dates in this CSV, so a nameless
                    # tie is rejected (m2's recency tie-break needs data we
                    # don't carry yet).
                    uniq = {r["_id"]: r for r in hits}
                    nm = tokens(name)
                    named = [r for r in uniq.values()
                             if nm and name_score(nm, r) >= FUZZY_MIN]
                    if len({r["_id"] for r in named}) == 1:
                        cand = ("addr_name", named[0]["_id"], None)
                    else:
                        rejects["ambiguous: same address, no name evidence"] += 1
                        continue

        # A directory may print a contact PERSON next to the farm name
        # (bienvenue-a-la-ferme: "Hélène et René Coste"); both are tried,
        # the farm name first.
        listing_names = [n for n in (name, L.get("alt_name") or "") if n]
        if cand is None and listing_names:
            pools = [p for p in (by_cp.get(cp), by_commune.get(commune)) if p]
            if not pools:
                rejects["no location on listing — name alone is not enough"] += 1
                continue
            why = ""
            for lname in listing_names:
                nm = tokens(lname)
                if not nm:
                    why = why or "listing name is only generic words"
                    continue
                for pool in pools:
                    exact = [r for r in pool if r["_tokens"] == nm or nm in r["_tok_alt"]]
                    if len({r["_id"] for r in exact}) == 1:
                        cand = ("name_commune", exact[0]["_id"], None)
                        break
                    if len(exact) > 1:
                        why = "ambiguous: same name twice in the commune"
                        continue
                    scored = sorted(((name_score(nm, r), r) for r in pool),
                                    key=lambda x: -x[0])
                    if scored and scored[0][0] >= FUZZY_MIN:
                        if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.02:
                            why = "ambiguous: two equally-good fuzzy names"
                            continue
                        cand = ("name_fuzzy", scored[0][1]["_id"], None)
                        break
                if cand is not None:
                    break
            if cand is None and why:
                rejects[why] += 1
                continue

        if cand is None:
            rejects["no acceptable match"] += 1
            continue

        method, rid, dist = cand
        r = by_id[rid]
        row = {
            "row_id": rid, "siret": r["siret"],
            "raisonSociale": r["raisonSociale"], "ville": r["ville"],
            "codePostal": r["codePostal"], "source": src, "method": method,
            "distance_m": f"{dist:.0f}" if dist is not None else "",
            "listing_name": name, **payload,
            "listing_id": L.get("osm_id") or L.get("listing_id") or "",
        }
        key = (rid, src)
        prev = accepted.get(key)
        if prev is None or METHOD_RANK[method] < METHOD_RANK[prev["method"]]:
            accepted[key] = row
        else:
            rejects["duplicate: operator already matched from this source"] += 1

    rows = sorted(accepted.values(), key=lambda r: (r["codePostal"], r["row_id"], r["source"]))
    for r in rows:
        method_counts[f"{r['source']}/{r['method']}"] += 1

    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    n_ops = len({r["row_id"] for r in rows})
    had_phone = {r["_id"] for r in ours if r["telephone"] or r["telephoneCommerciale"]}
    gain = len({r["row_id"] for r in rows if r["phone"] or r["mobile"]} - had_phone)
    log.info("─" * 62)
    log.info(f"listings considered      {len(listings):>6}")
    log.info(f"accepted matches         {len(rows):>6}  covering {n_ops} operators "
             f"({n_ops/len(ours):.1%} of {len(ours)})")
    for k, v in sorted(method_counts.items()):
        log.info(f"    {k:<28} {v:>5}")
    log.info(f"  phones on matches      {sum(1 for r in rows if r['phone'] or r['mobile']):>6}")
    log.info(f"  NEW phone coverage     {gain:>6} operators that had none")
    log.info(f"  websites on matches    {sum(1 for r in rows if r['website']):>6}")
    log.info(f"rejected                 {sum(rejects.values()):>6}")
    if args.report_rejects:
        for k, v in rejects.most_common():
            log.info(f"    {k:<52} {v:>5}")
    else:
        log.info("    (--report-rejects to see why)")
    log.info(f"written={len(rows)} -> {out_path}")


if __name__ == "__main__":
    main()
