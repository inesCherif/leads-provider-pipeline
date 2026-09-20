"""
M7-S8 — Match harvested listings to the producteurs population (03 / 63)
=========================================================================
Copy of `m6_s8_match.py` (itself from m5 / m3ag / m2). The non-negotiables
are unchanged:

  * AMBIGUITY IS REJECTION — a listing that fits two businesses equally well
    is dropped, not guessed.
  * NO MATCH WITHOUT LOCATION AGREEMENT — a name alone never carries contact
    data onto a row.

Rungs, strongest first: siret_exact → siren_unique → geo_name (≤150 m + name
≥ 0.62) → geo_only (≤40 m, unique) → addr_street → addr_name → addr_recent
→ name_commune → name_fuzzy.

What is specific to M7:
  * Sources = the m7_s5* harvests (acheteralasource, producteur.direct,
    fermes-locales, jours-de-marche, bonfromager) + the M3AG and M6 harvests
    read IN PLACE (Pages Jaunes 03+63 list every farmer, OSM, bienvenue-à-
    la-ferme, Agence Bio). Nothing is copied.
  * Every listing is re-tested against `m7_lib.listing_excluded` here, so a
    wine estate or a pork farm that slipped a harvest (rules were tightened
    while the first harvests ran) can never reach a match file.
  * The listing's description / category / contact person / productions
    travel with the match (Sam asked for the activity CONTEXT).
  * UNMATCHED listings that carry a phone or an e-mail are written to
    `unmatched_<dept>.csv` with the reject reason — the `Sans SIRET` tab
    (Ines 2026-09-11). Listings whose name is only generic words or whose
    location is unknown are NOT kept there.
  * Dedup key (business, source, listing, kind).

Usage:
    python scripts/m7_s8_match.py --departement 63
    python scripts/m7_s8_match.py --departement 63 --report-rejects
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
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m7_lib import (CHECK_DIR, INHERITED_AGRI, INHERITED_ELEVEURS, M7_STOPWORDS,   # noqa: E402
                    LISTING_FIELDS, listing_excluded, EXCLUDED_LOOSE_RE, NON_PRODUCER_RE)
from france_lib import in_dept                                                     # noqa: E402

SOURCES = [
    (CHECK_DIR / "aas_listings.csv",            "acheteralasource",  ";"),
    (CHECK_DIR / "pd_listings.csv",             "producteur_direct", ";"),
    (CHECK_DIR / "fl_listings.csv",             "fermes_locales",    ";"),
    (CHECK_DIR / "jdm_listings.csv",            "jours_de_marche",   ";"),
    (CHECK_DIR / "bf_listings.csv",             "bonfromager",       ";"),
    (CHECK_DIR / "dnf_listings.csv",            "denosfermes63",     ";"),
    (CHECK_DIR / "bp06_listings.csv",           "biopaca06",         ";"),   # Agribio 06 map, ODbL (2026-09-20)
    (CHECK_DIR / "pj_listings.csv",             "pagesjaunes",       ";"),
    (INHERITED_ELEVEURS / "pj_listings.csv",    "pagesjaunes",       ";"),
    (INHERITED_AGRI / "pj_listings.csv",        "pagesjaunes",       ";"),
    (INHERITED_AGRI / "osm_listings.csv",       "osm",               ";"),
    (INHERITED_AGRI / "baf_listings.csv",       "bienvenue_ferme",   ";"),
    (INHERITED_ELEVEURS / "agencebio_listings.csv", "agencebio",     ";"),
    (CHECK_DIR / "provider_agri.csv",           "provider",          ";"),   # m7_s19 — witness, Probable only
    (CHECK_DIR / "db_claims.csv",               None,                ";"),   # m7_s19 — source per row
    (CHECK_DIR / "social_emails.csv",           "social_fb",         ";"),   # m7_s18 — public Facebook pages
]
SOCIAL_CP: dict = {}      # siret -> postcode of OUR population, set in main() before load_listings
M7_SOURCES = {"acheteralasource", "producteur_direct", "fermes_locales", "jours_de_marche", "bonfromager", "denosfermes63", "biopaca06"}
PJ_SEEN: set = set()
# db_claims sources that never become a witness here (same rule as m6_s8)
CLAIM_SKIP = {"deliverable", "validator"}
# Pages Jaunes prints its own category on every card. The M7 trade-slug run
# (m7_s7, 2026-09-11) showed PJ answering an unknown slug with a free-text
# search (nurses at "Les Moulins", épiceries fines for `huilerie`), so a PJ
# card is a witness here only when its category is agricultural / food
# producing. A PJ row with no category (older harvests) is kept as before.
PJ_CATEGORY_OK_RE = re.compile(
    r"AGRICOL|AGRICULT|EXPLOITATION|ELEVAGE|ÉLEVAGE|ELEVEUR|ÉLEVEUR|FERMIER|FERME\b|VENTE DIRECTE|"
    r"MARA[IÎ]CH|ARBORICULT|FRUITS|L[EÉ]GUMES|P[EÉ]PINI|HORTICULT|JARDINERIE|V[EÉ]G[EÉ]TAUX|"
    r"LAITI|LAITERIE|FROMAG|MINOTERIE|MEUNERIE|MOULIN|HUILES|HUILERIE|PISCICULT|AQUACULT|"
    r"APICULT|MIEL|C[EÉ]R[EÉ]ALES|SEMENCES|PLANTES|AROMATIQ|VITICULT|COOP[EÉ]RATIVE AGRICOLE|"
    r"PRODUCTEUR|PRODUITS LAITIERS|VOLAILLE|VIANDE|ABATTOIR|CONSERVE|JUS DE FRUITS|AGROALIMENTAIRE|"
    r"PAINS|BOULANGERIE|CHARCUTERIE|VINS", re.I)   # the last four reach listing_excluded, which decides
WEBSITE_OK_VERDICTS = {"valide", "valid"}


def claim_source(r: dict) -> str | None:
    """Source label of a db_claims row, or None when it is not a witness."""
    src = (r.get("source") or "").strip()
    base = src.split("(")[0]
    if not base or base in CLAIM_SKIP:
        return None
    if base.startswith("client_file"):
        return "provider"
    if r.get("kind") == "website" and (r.get("verdict") or "") not in WEBSITE_OK_VERDICTS:
        return None
    if r.get("kind") == "email" and (r.get("verdict") or "") in ("invalid", "invalide", "malformed"):
        return None
    return src

GEO_STRICT_M = 40.0
GEO_TIGHT_M  = 150.0
FUZZY_MIN    = 0.62
ADDR_MIN     = 0.6
RECENT_GAP_DAYS = 365

STOPWORDS = {w.lower() for w in M7_STOPWORDS} | {"d", "l", "ea"}

FIELDNAMES = ["row_id", "siret", "raisonSociale", "ville", "codePostal",
              "source", "method", "distance_m", "listing_name",
              "phone", "mobile", "email", "website", "listing_id", "listing_url", "date", "detail",
              "verdict", "contact_name", "description", "categorie", "productions"]
UNMATCHED_FIELDS = ["source", "reject"] + LISTING_FIELDS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m7_s8")


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
    "ST": "SAINT", "STE": "SAINTE", "R": "RUE", "VOIE": "VOIE", "LD": "LIEU-DIT",
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


def days(d: str) -> int:
    try:
        import datetime
        return datetime.date(int(d[:4]), int(d[5:7]), int(d[8:10])).toordinal()
    except Exception:
        return 0


def load_listings(dept: str) -> tuple[list[dict], Counter]:
    out, dropped = [], Counter()
    for p, label, delim in SOURCES:
        if not p.exists():
            log.info(f"(skip) {p.parent.parent.name}/{p.name} not present — source '{label}' not harvested")
            continue
        n = 0
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=delim):
                if label == "social_fb":
                    # a page read for one of OUR SIRETs (the Sans SIRET pages are read by m7_s9)
                    if r.get("siret", "").startswith("U") or r["siret"] not in SOCIAL_CP:
                        continue
                    r["postcode"], r["name"] = SOCIAL_CP[r["siret"]], r.get("raison_sociale", "")
                    r["url"], r["listing_id"] = r.get("page_url", ""), r.get("page_url", "")
                cp = (r.get("postcode") or r.get("codePostal") or "").strip()
                if not (in_dept(cp, dept) or (not cp and (r.get("dept") or "") == dept)):
                    continue
                if label == "pagesjaunes" and r.get("listing_id"):
                    if r["listing_id"] in PJ_SEEN:
                        continue            # same PJ listing present in two trees
                    PJ_SEEN.add(r["listing_id"])
                src = label if label is not None else claim_source(r)
                if src is None:
                    continue
                if label == "provider":
                    r["categorie"] = r.get("label", "")     # the provider's Activité label faces the principle
                if label == "pagesjaunes":
                    r["categorie"] = r.get("category", "")  # PJ's own category faces the principle too
                    if r["categorie"] and not PJ_CATEGORY_OK_RE.search(r["categorie"]):
                        dropped["pagesjaunes: category not agricultural"] += 1
                        continue
                why = listing_excluded(r.get("name", ""), r.get("categorie", ""), r.get("website", ""),
                                       r.get("email", ""), r.get("description", ""))
                if why:
                    dropped[f"{src}: {why.split(':')[0]}"] += 1
                    continue
                r["_source"] = src
                out.append(r)
                n += 1
        log.info(f"loaded {p.parent.parent.name}/{p.name}: source '{label or 'per row'}' ({n} rows in dept {dept})")
    return out, dropped


def detail_of(L: dict, src: str) -> str:
    if src == "agencebio":
        return f"productions={L.get('productions','')[:200]}; activites={L.get('activites','')}; dirigeant={L.get('dirigeant','')}"
    if L.get("kind") in ("phone", "email", "website"):      # db_claims row
        return f"kind={L.get('kind','')}; verdict={L.get('verdict','')}; dialable={L.get('is_dialable','')}; numero_bio={L.get('numero_bio','')}"
    if src == "social_fb":
        return f"via={L.get('found_via','')}; page_address={L.get('page_address','')[:120]}"
    if src == "provider":
        return (f"label={L.get('label','')}; dirigeant={L.get('dirigeant','')}; naf={L.get('naf','')}; "
                f"email_verified={L.get('email_verified','')}; sirene_etat={L.get('sirene_etat','')}; "
                f"siren={L.get('siren','')}; file={L.get('source_file','')}")
    if src == "bienvenue_ferme":
        return f"alt_name={L.get('alt_name','')}"
    if src == "osm":
        return f"kind={L.get('kind','')}; operator={L.get('operator','')}"
    if src == "pagesjaunes":
        return f"category={L.get('category','')}"
    if src in M7_SOURCES:
        return f"categorie={L.get('categorie','')[:120]}; productions={L.get('productions','')[:120]}"
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Match listings to the M7 producteurs population")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--report-rejects", action="store_true")
    args = ap.parse_args()
    dept = args.departement

    ours_path = CHECK_DIR / f"operateurs_{dept}.csv"
    out_path  = CHECK_DIR / f"matched_{dept}.csv"
    unm_path  = CHECK_DIR / f"unmatched_{dept}.csv"
    if not ours_path.exists():
        sys.exit(f"{ours_path} not found — run m7_s2_transform.py first.")
    with ours_path.open(encoding="utf-8-sig", newline="") as fh:
        ours = list(csv.DictReader(fh))
    SOCIAL_CP.update({r["siret"]: r["codePostal"] for r in ours if r["siret"]})
    listings, dropped = load_listings(dept)
    if not listings:
        sys.exit("No listing files found.")

    by_id, by_siret, by_siren = {}, {}, defaultdict(list)
    by_commune, by_cp = defaultdict(list), defaultdict(list)
    for r in ours:
        r["_id"] = r["siret"] or f"X{r['siren']}"
        by_id[r["_id"]] = r
        if r["siret"]:
            by_siret[r["siret"]] = r
        if r.get("siren"):
            by_siren[r["siren"]].append(r)
        r["_tokens"] = tokens(f"{r['raisonSociale']} {r.get('denomination_legale','')} {r['gerant']}")
        r["_tok_alt"] = [tk for tk in (tokens(r["raisonSociale"]), tokens(r.get("denomination_legale", "")),
                                       tokens(r["gerant"])) if tk]
        r["_lat"], r["_lon"] = fnum(r["lat"]), fnum(r["lon"])
        r["_house"], r["_street"] = addr_parse(r.get("adresse", ""))
        r["_created"] = days(r.get("date_creation", ""))
        by_commune[norm(r["ville"])].append(r)
        by_cp[r["codePostal"]].append(r)

    accepted: dict[tuple, dict] = {}
    unmatched: list[dict] = []
    rejects = Counter()
    method_counts = Counter()
    METHOD_RANK = {"siret_exact": 0, "siren_unique": 1, "geo_only": 2, "geo_name": 3,
                   "addr_street": 4, "addr_name": 5, "addr_recent": 6,
                   "name_commune": 7, "name_fuzzy": 8}

    def reject(L: dict, src: str, why: str, keep: bool = True) -> None:
        rejects[why] += 1
        if not (keep and src in M7_SOURCES and ((L.get("phone") or "").strip() or (L.get("email") or "").strip())):
            return
        # no NAF can vouch for an unmatched row: the LOOSE principle applies
        blob = " ".join(L.get(k, "") for k in ("name", "categorie", "productions", "description"))
        if EXCLUDED_LOOSE_RE.search(blob):
            rejects["unmatched dropped on principle (loose)"] += 1
            return
        if NON_PRODUCER_RE.search(f"{L.get('name', '')} {L.get('categorie', '')}"):
            rejects["unmatched dropped: registrant is no producer (tyres, landscaping, transport…)"] += 1
            return
        unmatched.append({"source": src, "reject": why, **{k: L.get(k, "") for k in LISTING_FIELDS}})

    for L in listings:
        src = L["_source"]
        name = L.get("name") or ""
        lat, lon = fnum(L.get("lat")), fnum(L.get("lon"))
        cp = (L.get("postcode") or "").strip()[:5]
        commune = norm(L.get("city") or "")
        if not cp:
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
        if len(s) < 9 and len(digits(L.get("siren", ""))) == 9:
            s = digits(L.get("siren", ""))
        if cand is None and len(s) >= 9:
            sib = by_siren.get(s[:9], [])
            if len(sib) == 1:
                cand = ("siren_unique", sib[0]["_id"], None)
            elif len(sib) > 1 and cp:
                same_cp = [r for r in sib if r["codePostal"] == cp]
                if len(same_cp) == 1:
                    cand = ("siren_unique", same_cp[0]["_id"], None)

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
                        reject(L, src, "ambiguous: 2+ businesses match by geo+name")
                        continue
                    d, r = named_hits[0]
                    cand = ("geo_name", r["_id"], d)
                else:
                    very_near = [(d, r) for d, r in near if d <= GEO_STRICT_M]
                    if len({r["_id"] for _, r in very_near}) == 1:
                        d, r = very_near[0]
                        cand = ("geo_only", r["_id"], d)
                    elif len(very_near) > 1:
                        reject(L, src, "ambiguous: 2+ businesses within 40 m")
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
                    uniq = {r["_id"]: r for r in hits}
                    nm = tokens(name)
                    named = [r for r in uniq.values()
                             if nm and name_score(nm, r) >= FUZZY_MIN]
                    if len({r["_id"] for r in named}) == 1:
                        cand = ("addr_name", named[0]["_id"], None)
                    else:
                        dated = sorted(uniq.values(), key=lambda r: -r["_created"])
                        if dated[0]["_created"] and (len(dated) < 2 or dated[0]["_created"] - dated[1]["_created"] >= RECENT_GAP_DAYS):
                            cand = ("addr_recent", dated[0]["_id"], None)
                        else:
                            reject(L, src, "ambiguous: same address, no name evidence, same age")
                            continue

        # the contact PERSON (alt_name) is a second name to try: a sole
        # trader is registered under the person's name, not the farm's
        listing_names = [n for n in (name, L.get("alt_name") or L.get("dirigeant") or L.get("contact_nom") or "") if n]
        if cand is None and listing_names:
            pools = [p for p in (by_cp.get(cp), by_commune.get(commune)) if p]
            if not pools:
                reject(L, src, "no location on listing — name alone is not enough", keep=False)
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
                reject(L, src, why, keep=not why.startswith("listing name"))
                continue

        if cand is None:
            reject(L, src, "no acceptable match")
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
            "listing_url": L.get("url") or L.get("detail_url") or "",
            "date": L.get("date_creation") or L.get("check_date") or "",
            "detail": detail_of(L, src),
            "verdict": L.get("verdict") or "",
            "contact_name": L.get("alt_name") or L.get("dirigeant") or "",
            "description": (L.get("description") or "")[:1500],
            "categorie": L.get("categorie") or "",
            "productions": L.get("productions") or "",
        }
        kind = "".join(k[0] for k in ("phone", "mobile", "email", "website") if payload[k])
        key = (rid, src, row["listing_id"], kind)
        prev = accepted.get(key)
        if prev is None or METHOD_RANK[method] < METHOD_RANK[prev["method"]]:
            accepted[key] = row
        else:
            rejects["duplicate: business already matched from this source"] += 1

    rows = sorted(accepted.values(), key=lambda r: (r["codePostal"], r["row_id"], r["source"]))
    for r in rows:
        method_counts[f"{r['source']}/{r['method']}"] += 1

    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    # one unmatched row per (source, listing_id)
    seen_u, urows = set(), []
    for u in sorted(unmatched, key=lambda x: (x["postcode"], x["name"])):
        k = (u["source"], u["listing_id"])
        if k not in seen_u:
            seen_u.add(k)
            urows.append(u)
    with unm_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=UNMATCHED_FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(urows)

    n_ops = len({r["row_id"] for r in rows})
    log.info("-" * 62)
    log.info(f"listings considered      {len(listings):>6}   dropped on principle at load: {dict(dropped)}")
    log.info(f"accepted matches         {len(rows):>6}  covering {n_ops} businesses "
             f"({n_ops/len(ours):.1%} of {len(ours)})")
    for k, v in sorted(method_counts.items()):
        log.info(f"    {k:<36} {v:>5}")
    log.info(f"  phones on matches      {sum(1 for r in rows if r['phone'] or r['mobile']):>6}  "
             f"businesses with a phone: {len({r['row_id'] for r in rows if r['phone'] or r['mobile']})}")
    log.info(f"  e-mails on matches     {sum(1 for r in rows if r['email']):>6}  "
             f"businesses with an e-mail: {len({r['row_id'] for r in rows if r['email']})}")
    log.info(f"  websites on matches    {sum(1 for r in rows if r['website']):>6}  "
             f"businesses with a site: {len({r['row_id'] for r in rows if r['website']})}")
    log.info(f"  contact person         {len({r['row_id'] for r in rows if r['contact_name']}):>6}  businesses")
    log.info(f"  description            {len({r['row_id'] for r in rows if r['description']}):>6}  businesses")
    log.info(f"rejected                 {sum(rejects.values()):>6}   unmatched kept for the Sans SIRET tab: {len(urows)} "
             f"(phone {sum(1 for u in urows if u['phone'])}, e-mail {sum(1 for u in urows if u['email'])})")
    if args.report_rejects:
        for k, v in rejects.most_common():
            log.info(f"    {k:<52} {v:>5}")
    log.info(f"written={len(rows)} -> {out_path}   unmatched={len(urows)} -> {unm_path}")


if __name__ == "__main__":
    main()
