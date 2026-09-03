"""
m3ag_lib — shared paths, operator adapter and agri vocabulary for m3ag_s3…s12
============================================================================
The M2 (boulangerie) libraries are sector-neutral in their LOGIC but carry
bakery vocabulary and dept-13 paths in their CONSTANTS. This module is the
agri-bio counterpart: every m3ag script imports its paths, its word lists
and the operator adapter from here so they cannot drift apart.

Design rules carried over from M2 (measured there, kept here):
  * search_quota.json stays SHARED with the boulangerie tree — Tavily/ddgs
    credits are account-wide; a second counter would double-spend a free tier.
  * geo gate per RESULT, never on a merged blob (a same-named farm in another
    dept passes a merged test).
  * a name token must appear in the result title/url — directory pages
    titled "Agriculteurs à Ambert" pass the geo gate for every farm in town.

Usage:
    python scripts/m3ag_lib.py --selftest
"""

from __future__ import annotations

import csv
import re
import sys
import unicodedata
import urllib.parse
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

CHECK_DIR = PROJECT_ROOT / "exports" / "agriculteurs" / "checkpoints"
OUT_DIR = PROJECT_ROOT / "exports" / "agriculteurs"
# Shared on purpose — see module docstring.
QUOTA_PATH = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints" / "search_quota.json"

# Legal forms + farm generics: a title match on one of these is not a match
# on THIS farm ("GAEC" and "FERME" repeat across every commune).
AGRI_STOPWORDS = {
    "GAEC", "EARL", "SCEA", "SCEV", "SCA", "SARL", "SAS", "SASU", "EURL", "SCI",
    "SNC", "EI", "EIRL", "ETS", "STE", "SOCIETE", "EXPLOITATION", "AGRICOLE",
    "FERME", "DOMAINE", "ELEVAGE", "JARDIN", "JARDINS", "VERGER", "VERGERS",
    "BIO", "BIOLOGIQUE", "BIOLOGIQUES", "PRODUCTEUR", "PRODUCTEURS",
    "MARAICHER", "MARAICHAGE", "CHEVRERIE", "BERGERIE", "MIELLERIE",
    "LA", "LE", "LES", "DE", "DU", "DES", "ET", "AU", "AUX", "CHEZ", "EN",
    "MONSIEUR", "MADAME", "MR", "MME", "M",
}

# Vocabulary that says "this page is a farm's own page" — the agri
# counterpart of m2lib_validate.BAKERY_WORDS (used through score_fn=).
AGRI_WORDS = (
    "agriculture biologique", "certifié ab", "certifie ab", "bio", "ferme",
    "exploitation", "maraîch", "maraich", "élevage", "elevage", "verger",
    "fromage", "chèvre", "chevre", "brebis", "vache", "volaille", "œufs",
    "oeufs", "miel", "apicult", "vente directe", "vente à la ferme",
    "vente a la ferme", "amap", "producteur", "cueillette", "légumes",
    "legumes", "fruits", "céréales", "cereales", "vigne", "vin", "viticult",
    "pâturage", "paturage", "troupeau", "safran", "plantes", "aromatiques",
    "ppam", "tisane", "confiture", "jus de pomme", "panier", "marché",
    "marche", "ferme pédagogique", "gîte", "gite", "agneau", "bœuf", "boeuf",
    "porc", "lait", "yaourt", "biodynamie", "demeter", "nature & progrès",
)

# Hosts whose pages are never the farm's own site. Universal aggregators
# come from m2_s8 (the list was learned the hard way); the agri directories
# are added here. Substring match on the host.
from m2_s8_websites import AGGREGATORS as _M2_AGGREGATORS  # noqa: E402

AGRI_AGGREGATORS = tuple(_M2_AGGREGATORS) + (
    # learned from the 2026-09-03 Tavily pilot: registry mirrors, obituaries,
    # certificates, the departmental council, local press
    "lagazettefrance", "lesentreprises.com", "docubiz", "simplifia",
    "avis-de-deces", "libramemoria", "dansnoscoeurs", "cuisine.eco",
    "ecocert", "puy-de-dome.fr", "allier.fr", "agroimmo", "rcf.fr",
    "humanite.fr", "copainsdavant", "producteurs-vins", "hachette-vins",
    "vinatis", "idealwine", "raisin.digital", "mareehaute", "trustfolio",
    "infonet", "dataprospects", "localbiz", "leguichetdesformalites",
    "societeinfo", "data-prospection", "lavieduvillage", "annuaire-entreprises",
    "verif.com", "kbis", "greffe", "rne.", "inpi.fr", "opendatasoft",
    "lamontagne.fr", "la-montagne", "leprogres", "lepopulaire", "lejdc",
    "bfmtv", "france3", "francebleu", "ici.fr", "20minutes", "ouest-france",

    "agencebio", "bienvenue-a-la-ferme", "marches-producteurs",
    "jours-de-marche", "annuaire-mairie", "118712", "118000", "kelbio",
    "bio-et-local", "laruchequiditoui", "producteurs-locaux", "lesproducteurs",
    "mon-producteur", "produits-locaux", "locavor", "cagette", "pourdebon",
    "bienmanger", "annuaire-des-producteurs", "bioconsomacteurs", "reseau-amap",
    "amap-auvergne", "auvergnebio", "bio63", "chambres-agriculture",
    "puy-de-dome.chambres-agriculture", "allier.chambres-agriculture",
    "societe.com", "pappers", "infogreffe", "verif.com", "manageo", "kompass",
    "annuaire-entreprises", "entreprises.lefigaro", "b-reputation", "rubypayeur",
    "pagesjaunes", "pagespro", "hoodspot", "justacote", "cylex", "infobel",
    "mappy", "yelp", "tripadvisor", "petitfute", "lannuaire", "telephone-annuaire",
    "facebook", "instagram", "linkedin", "twitter", "x.com", "youtube", "tiktok",
    "pinterest", "google.", "bing.com", "wikipedia", "wikimedia", "openstreetmap",
    "leboncoin", "labelleadresse", "gralon", "aladom", "trouver-un-", "horaires",
    "nomao", "entreprises-", "dirigeant.", "bfmbusiness", "lesechos",
    "lamontagne.fr", "la-montagne", "lesbio", "bioaddict", "mesproducteurs",
    "naturabio", "monmarche", "fermedirect", "acheteralasource", "fournisseurs",
    "data.gouv", "insee", "agriculture.gouv", "inpi", "bodacc", "legifrance",
    "annuaire.", "annuaires", "sirene", "score3", "societe-", "corporama",
    "creditsafe", "ellisphere", "fiches-", "companies", "opendatasoft",
)

BAD_TLD = (".gouv.fr", ".gov", ".edu", ".ru", ".cn", ".xyz", ".top", ".click", ".work")

SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ---------------------------------------------------------------- helpers
def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s).upper()
    return " ".join(s.split())


def name_tokens(*names: str) -> set:
    """Distinctive tokens of a farm's names (raisonSociale, gerant…)."""
    toks = set()
    for n in names:
        toks |= {t for t in norm(n).split() if len(t) >= 3 and t not in AGRI_STOPWORDS}
    return toks


def geo_pass(cp: str, commune: str, blob: str) -> str:
    """'cp' | 'commune' | '' — the geo gate, applied to ONE result's text."""
    if cp and cp in blob:
        return "cp"
    cu = norm(commune)
    if cu and cu in norm(blob):
        return "commune"
    return ""


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url or "").netloc.lower().replace("www.", "")


def root_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_aggregator(url_or_host: str) -> bool:
    h = host_of(url_or_host) if "://" in (url_or_host or "") else (url_or_host or "").lower()
    if not h:
        return True
    if h.endswith(BAD_TLD):
        return True
    return any(a in h for a in AGRI_AGGREGATORS)


def is_social(url_or_host: str) -> bool:
    h = host_of(url_or_host) if "://" in (url_or_host or "") else (url_or_host or "").lower()
    return any(s in h for s in SOCIAL_HOSTS)


def agri_score(text: str) -> int:
    t = (text or "").lower()
    return sum(1 for w in AGRI_WORDS if w in t)


# ---------------------------------------------------------------- operators
def row_id(op: dict) -> str:
    return op.get("siret") or f"NB{op.get('numeroBio', '')}"


def load_operators(dept: str) -> list[dict]:
    """operateurs_<dept>.csv rows plus adapter keys (prefixed '_')."""
    p = CHECK_DIR / f"operateurs_{dept}.csv"
    if not p.exists():
        sys.exit(f"{p} missing — run m3ag_s2_transform.py --departements {dept} first.")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        ops = list(csv.DictReader(fh))
    for op in ops:
        op["_id"] = row_id(op)
        op["_has_phone"] = bool(op.get("telephone") or op.get("telephoneCommerciale"))
        op["_has_email"] = bool(op.get("email"))
        op["_has_site"] = bool(op.get("siteWebs"))
        op["_tokens"] = name_tokens(op.get("raisonSociale", ""), op.get("gerant", ""))
    return ops


def load_matches(dept: str) -> dict[str, list[dict]]:
    """matched_<dept>.csv grouped by row_id (several sources per operator)."""
    p = CHECK_DIR / f"matched_{dept}.csv"
    out: dict[str, list[dict]] = {}
    for m in read_csv(p):
        out.setdefault(m["row_id"], []).append(m)
    return out


# ---------------------------------------------------------------- csv i/o
def read_csv(path: Path, delim: str = ";") -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=delim))


def append_rows(path: Path, fieldnames: list[str], rows: list[dict], delim: str = ";") -> None:
    """Flush per call — the rule that saved three scrapes in this repo."""
    if not rows:
        return
    new = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=delim,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
        fh.flush()


def load_done(path: Path) -> set:
    return set(path.read_text(encoding="utf-8").split()) if path.exists() else set()


def mark_done(path: Path, key: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(key + "\n")


# ---------------------------------------------------------------- selftest
def _selftest() -> None:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += 0 if ok else 1
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))

    check("stopwords drop legal forms", name_tokens("GAEC DES BRUYERES", "René Prononce"),
          {"BRUYERES", "RENE", "PRONONCE"})
    check("accents folded", name_tokens("Chèvrerie du Père Noël"), {"PERE", "NOEL"})
    check("geo gate by cp", geo_pass("63600", "Ambert", "Ferme X 63600 Ambert"), "cp")
    check("geo gate by commune", geo_pass("63600", "Saint-Amant-Roche-Savine",
                                          "a saint amant roche savine"), "commune")
    check("geo gate fails elsewhere", geo_pass("63600", "Ambert", "Ferme X 03000 Moulins"), "")
    check("aggregator pj", is_aggregator("https://www.pagesjaunes.fr/pros/1"), True)
    check("aggregator agencebio", is_aggregator("https://annuaire.agencebio.org/x"), True)
    check("bad tld", is_aggregator("https://ferme.xyz/"), True)
    check("own site passes", is_aggregator("https://www.fermedesbruyeres.fr/"), False)
    check("social", is_social("https://www.facebook.com/fermedesbruyeres"), True)
    check("agri score", agri_score("Vente directe de fromages de chèvre bio à la ferme") >= 3, True)
    check("host_of", host_of("https://www.Ferme-X.fr/contact"), "ferme-x.fr")
    check("root", root_domain("shop.ferme-x.fr"), "ferme-x.fr")
    print(f"selftest: {fails} failure(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    print(__doc__)
