"""
m5_lib — shared paths, population adapter and lodging vocabulary for m5_s*
=========================================================================
Sector 5 (M5): gîtes + campings, départements 03 and 63, built for Maha's
call sheet with her "annonces" method (a dated public trace proves the
business is alive). Same shape as `m3ag_lib` so the M3AG scripts that are
sector-neutral can be wrapped by overriding their module globals with the
objects defined here (the `m3ag_s11_verify.py` pattern).

Rules carried over (measured in M2/M3AG, kept):
  * search_quota.json stays SHARED with the boulangerie tree — credits are
    account-wide; a second counter would double-spend a free tier.
  * geo gate per RESULT, never on a merged blob.
  * a name token must appear in the result title/url.
  * the population adapter `operateurs_<dept>.csv` is COMMA-delimited (the
    m3ag_s2 contract), every harvest checkpoint is ';'.

Usage:
    python scripts/m5_lib.py --selftest
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# Re-exported so wrapped m3ag scripts can be pointed at one module.
from m3ag_lib import (                                   # noqa: E402,F401
    norm, geo_pass, host_of, root_domain, is_social, is_junk_witness,
    read_csv, append_rows, load_done, mark_done,
    JUNK_WITNESS, JUNK_MAILBOX, BAD_TLD, COMMON_FIRST_NAMES, SOCIAL_HOSTS,
    EMAIL_RE, AGRI_AGGREGATORS,
)

SECTOR = "hebergement"
CHECK_DIR = PROJECT_ROOT / "exports" / SECTOR / "checkpoints"
OUT_DIR = PROJECT_ROOT / "exports" / SECTOR
# Shared on purpose — see module docstring.
QUOTA_PATH = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints" / "search_quota.json"

DEPARTEMENTS = ("03", "63")
NAF_SCOPE = {"55.20Z", "55.30Z"}
NAF_LABELS = {
    "55.20Z": "Hébergement touristique et autre hébergement de courte durée",
    "55.30Z": "Terrains de camping et parcs pour caravanes ou véhicules de loisirs",
}

# Legal forms + lodging generics + geography: a title match on one of these
# is not a match on THIS business ("CAMPING", "GITE", "DOMAINE" repeat across
# every commune; the geography list is the M3AG one, learned by hand-check).
HEB_STOPWORDS = {
    "SARL", "SAS", "SASU", "EURL", "SCI", "SNC", "EI", "EIRL", "ETS", "STE",
    "SOCIETE", "ASSOCIATION", "ASS", "GAEC", "EARL", "SCEA", "SA", "SEM",
    "CAMPING", "CAMPINGS", "CARAVANING", "GITE", "GITES", "CHAMBRE", "CHAMBRES",
    "HOTE", "HOTES", "MEUBLE", "MEUBLES", "LOCATION", "LOCATIONS", "VACANCES",
    "RESIDENCE", "RESIDENCES", "VILLAGE", "DOMAINE", "FERME", "MAISON",
    "HEBERGEMENT", "HEBERGEMENTS", "TOURISME", "TOURISTIQUE", "LOISIRS",
    "PLEIN", "AIR", "PARC", "NATURE", "LAC", "ETANG", "MOULIN", "CHATEAU",
    "LA", "LE", "LES", "DE", "DU", "DES", "ET", "AU", "AUX", "CHEZ", "EN", "SUR",
    "MONSIEUR", "MADAME", "MR", "MME", "M",
    "PUY", "DOME", "AUVERGNE", "ALLIER", "CLERMONT", "FERRAND", "LIMAGNE",
    "SANCY", "LIVRADOIS", "FOREZ", "COMBRAILLES", "BOURBONNAIS", "VOLCANS",
    "VICHY", "MOULINS", "MONTLUCON", "AMBERT", "ISSOIRE", "THIERS", "RIOM",
    "MASSIF", "CENTRAL", "CHAINE", "PUYS", "SIOULE", "DORE", "CEZALLIER",
}

# Vocabulary that says "this page is a lodging's own page" — the M5
# counterpart of m2lib_validate.BAKERY_WORDS (used through score_fn=).
HEB_WORDS = (
    "camping", "emplacement", "emplacements", "mobil-home", "mobil home",
    "mobilhome", "chalet", "chalets", "tente", "caravane", "camping-car",
    "gîte", "gite", "gîtes", "gites", "chambre d'hôtes", "chambres d'hôtes",
    "chambre d'hotes", "chambres d'hotes", "table d'hôtes", "meublé",
    "location de vacances", "location saisonnière", "hébergement", "hebergement",
    "nuitée", "nuitées", "nuit", "séjour", "sejour", "tarifs", "tarif",
    "réservation", "reservation", "réserver", "disponibilités", "disponibilites",
    "capacité", "personnes", "couchages", "piscine", "sanitaires", "épis",
    "epis", "étoiles", "etoiles", "gîtes de france", "clévacances", "clevacances",
    "accueil", "arrivée", "départ", "taxe de séjour", "animaux acceptés",
)

# Hosts whose pages are never the lodging's own site: M3AG's list (registry
# mirrors, press, generic directories) + booking platforms and tourism
# directories. Substring match on the host.
HEB_AGGREGATORS = tuple(AGRI_AGGREGATORS) + (
    "booking.", "airbnb", "abritel", "homeaway", "vrbo", "expedia", "hotels.com",
    "trivago", "kayak", "hostelworld", "gites-de-france", "gitesdefrance",
    "clevacances", "campingfrance", "camping-and-co",
    "campings.com", "toocamp", "tohapi", "flowercampings", "flower-campings",
    "huttopia", "capfun", "homair", "yellohvillage", "sandaya", "vacanceselect",
    "campez-couvert", "acsi", "eurocampings", "camping.info", "campercontact",
    "park4night", "pitchup", "auvergne-destination", "auvergne-tourisme",
    "auvergnerhonealpes-tourisme", "allier-tourisme", "allier-auvergne-tourisme",
    "puy-de-dome-tourisme", "planetpuydedome", "sancy.com", "vulcania",
    "apidae", "datatourisme", "tourisme-", "-tourisme", "office-tourisme",
    "officedetourisme", "gite.com", "gites.fr", "gite01", "amivac",
    "likibu", "holidu", "tripadvisor", "petitfute", "leboncoin", "seloger",
    "logic-immo", "bienici", "pap.fr", "francetravail", "pole-emploi",
    "indeed", "hellowork", "jobijoba", "meteociel", "meteofrance",
    "atout-france", "classement.atout", "annuaire-mairie",
    "bodacc", "infogreffe", "pappers", "societe.com",
)

# Type of lodging, from the name when the NAF cannot tell (55.20Z covers
# gîtes, meublés, chambres d'hôtes, résidences and villages de vacances).
TYPE_RULES = (
    ("camping",           re.compile(r"\b(CAMPING|CARAVAN|PLEIN AIR|HPA|AIRE NATURELLE)", re.I)),
    ("chambres d'hôtes",  re.compile(r"CHAMBRE\w*\s+D\W?\s*H[OÔ]TE", re.I)),
    ("village vacances",  re.compile(r"\b(VILLAGE\w*\s+(DE\s+)?VACANCES|VVF|CENTRE\s+DE\s+VACANCES|COLONIE|VILLAGE\s+CLUB)", re.I)),
    ("résidence",         re.compile(r"\bR[EÉ]SIDENCE", re.I)),
    ("gîte",              re.compile(r"\bG[IÎ]TE", re.I)),
    ("meublé",            re.compile(r"\b(MEUBL[EÉ]|LOCATION|APPART|STUDIO|CHALET|LODGE|ROULOTTE|YOURTE|CABANE)", re.I)),
)

# Public-law operators: communes, EPCI, régies, syndicats. Phase 1 ships
# private operators only (Ines 2026-09-09); these rows stay in the CSV,
# flagged, for phase 2.
PUBLIC_NAME_RE = re.compile(
    r"\b(COMMUNE\s+DE|MAIRIE|COMMUNAUTE\s+DE\s+COMMUNES|COMMUNAUTE\s+D\W?AGGLOMERATION|"
    r"SIVOM|SIVU|SYNDICAT|REGIE|CCAS|DEPARTEMENT\s+D|CONSEIL\s+DEPARTEMENTAL|"
    r"ETABLISSEMENT\s+PUBLIC|EPCI|COLLECTIVITE)", re.I)

PHONE_SOURCE_FR = {
    "osm": "OpenStreetMap",
    "pagesjaunes": "Pages Jaunes",
    "site/mentions_legales": "mentions légales du site de l'établissement",
    "site/confirme": "site web de l'établissement",
    "corrobore": "2 sites indépendants concordants",
    "france_travail": "déclaré par l'établissement (offre d'emploi France Travail)",
    "google_panel": "fiche Google de l'établissement",
    "provider": "fichier fournisseur (non vérifié)",
}


# ---------------------------------------------------------------- helpers
def name_tokens(*names: str) -> set:
    """Distinctive tokens of a lodging's names (enseigne, raison sociale, gérant…)."""
    toks = set()
    for n in names:
        toks |= {t for t in norm(n).split() if len(t) >= 3 and t not in HEB_STOPWORDS}
    return toks


def strong_tokens(toks: set) -> set:
    """Tokens that can identify a business on their own (no first names)."""
    return {t for t in toks if len(t) >= 4 and t not in COMMON_FIRST_NAMES}


def is_aggregator(url_or_host: str) -> bool:
    h = host_of(url_or_host) if "://" in (url_or_host or "") else (url_or_host or "").lower()
    if not h:
        return True
    if h.endswith(BAD_TLD):
        return True
    return any(a in h for a in HEB_AGGREGATORS)


def heb_score(text: str) -> int:
    t = (text or "").lower()
    return sum(1 for w in HEB_WORDS if w in t)


def classify_type(naf: str, *names: str) -> str:
    """camping | gîte | chambres d'hôtes | résidence | village vacances | meublé | non typé."""
    if naf == "55.30Z":
        return "camping"
    blob = " ".join(n for n in names if n)
    for label, rx in TYPE_RULES:
        if rx.search(blob):
            return label
    return "non typé"


def is_public(nature_juridique: str, *names: str) -> bool:
    nj = (nature_juridique or "").strip()
    if nj.startswith("7"):
        return True
    return any(PUBLIC_NAME_RE.search(n or "") for n in names)


# ---------------------------------------------------------------- population
def row_id(op: dict) -> str:
    return op.get("siret") or f"X{op.get('siren', '')}"


def load_operators(dept: str) -> list[dict]:
    """operateurs_<dept>.csv rows plus adapter keys (prefixed '_')."""
    p = CHECK_DIR / f"operateurs_{dept}.csv"
    if not p.exists():
        sys.exit(f"{p} missing — run m5_s2_transform.py --departements {dept} first.")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        ops = list(csv.DictReader(fh))
    for op in ops:
        op["_id"] = row_id(op)
        op["_has_phone"] = bool(op.get("telephone") or op.get("telephoneCommerciale"))
        op["_has_email"] = bool(op.get("email"))
        op["_has_site"] = bool(op.get("siteWebs"))
        op["_tokens"] = name_tokens(op.get("raisonSociale", ""),
                                    op.get("denomination_legale", ""),
                                    op.get("gerant", ""))
    return ops


def load_matches(dept: str) -> dict[str, list[dict]]:
    """matched_<dept>.csv grouped by row_id (several sources per business)."""
    p = CHECK_DIR / f"matched_{dept}.csv"
    out: dict[str, list[dict]] = {}
    for m in read_csv(p):
        out.setdefault(m["row_id"], []).append(m)
    return out


# ---------------------------------------------------------------- selftest
def _selftest() -> None:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += 0 if ok else 1
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))

    check("stopwords drop generics", name_tokens("CAMPING LES BRUYERES", "Camping de la Sioule"),
          {"BRUYERES"})
    check("accents folded", name_tokens("Gîte du Père Noël"), {"PERE", "NOEL"})
    check("type camping by naf", classify_type("55.30Z", "SARL DUPONT"), "camping")
    check("type chambres d'hôtes", classify_type("55.20Z", "LES CHAMBRES D'HÔTES DU LAC"), "chambres d'hôtes")
    check("type gîte", classify_type("55.20Z", "GITE DE LA FONTAINE"), "gîte")
    check("type village", classify_type("55.20Z", "VVF VILLAGES"), "village vacances")
    check("type camping by name", classify_type("55.20Z", "CAMPING DE L'ETANG"), "camping")
    check("type unknown", classify_type("55.20Z", "MADAME MARTIN"), "non typé")
    check("public by nature", is_public("7210", "COMMUNE DE MURAT"), True)
    check("public by name", is_public("9220", "SYNDICAT MIXTE DU LAC"), True)
    check("private", is_public("5710", "CAMPING DES VOLCANS"), False)
    check("aggregator booking", is_aggregator("https://www.booking.com/hotel/fr/x.html"), True)
    check("aggregator gdf", is_aggregator("https://www.gites-de-france-puydedome.com/x"), True)
    check("aggregator pj", is_aggregator("https://www.pagesjaunes.fr/pros/1"), True)
    check("own site passes", is_aggregator("https://www.camping-les-volcans.fr/"), False)
    check("heb score", heb_score("Camping 3 étoiles, 80 emplacements, piscine, réservation en ligne") >= 4, True)
    check("geo gate by cp", geo_pass("63790", "Murol", "Camping X 63790 Murol"), "cp")
    print(f"selftest: {fails} failure(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    print(__doc__)
