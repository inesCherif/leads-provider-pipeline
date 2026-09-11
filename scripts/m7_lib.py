"""
m7_lib — shared paths, population adapter and vocabulary for m7_s* (PRODUCTEURS)
===============================================================================
Sector 7 (M7): the agriculture family of départements 03 and 63 that M6
(éleveurs, NAF 01.4x) does not cover — vignerons, maraîchers, arboriculteurs,
fromagers / laiteries, transformation à la ferme… — built 2026-09-11 after
Sam's e-mail: "segmenter par activité, récupérer les mails, le nom des
décideurs, le contexte", 03 + 63 first, then all of France. Same shape as
`m6_lib` / `m3ag_lib` so the sector-neutral M3AG scripts can be wrapped by
overriding their module globals (the `m6_s7_pagesjaunes.py` pattern).

Rules carried over (measured in M2 / M3AG / M5 / M6, kept):
  * search_quota.json stays SHARED with the boulangerie tree.
  * geo gate per RESULT, never on a merged blob.
  * a name token must appear in the result title/url.
  * `operateurs_<dept>.csv` is COMMA-delimited (the m3ag_s2 contract),
    every harvest checkpoint is ';'.
  * a directory is a WITNESS, never a farm's own site: every host harvested
    by an m7_s5* script is also in `m3ag_lib.AGRI_AGGREGATORS`.

What is specific to M7:
  * NAF_SCOPE = agriculture minus livestock (m6_lib.NAF_SCOPE), each code
    TAGGED with a `sous_segment` (Ines 2026-09-11: one file per dept, one
    row per SIRET, a Sous-segment column; several tags allowed).
  * LISTING_FIELDS = the canonical listing schema of m3ag_s10_baf plus
    description / productions / categorie / siret, because Sam asked for
    the activity CONTEXT and some directories print the SIRET.
  * INHERITED_AGRI / INHERITED_ELEVEURS: the M3AG and M6 harvests (Pages
    Jaunes 03+63, OSM, bienvenue-à-la-ferme, Agence Bio) are read IN PLACE.

Usage:
    python scripts/m7_lib.py --selftest
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# Re-exported so wrapped m3ag / m6 scripts can be pointed at one module.
from m3ag_lib import (                                   # noqa: E402,F401
    norm, geo_pass, host_of, root_domain, is_social, is_junk_witness,
    read_csv, append_rows, load_done, mark_done, agri_score,
    JUNK_WITNESS, JUNK_MAILBOX, BAD_TLD, COMMON_FIRST_NAMES, SOCIAL_HOSTS,
    EMAIL_RE, AGRI_AGGREGATORS, AGRI_STOPWORDS,
)
from m6_lib import (                                     # noqa: E402,F401
    is_public, phone_digits, PUBLIC_NAME_RE, ELEVAGE_GENERICS,
)
from maha_lib import SENT_DIR as MAHA_SENT_DIR, load_sent, sent_files   # noqa: E402,F401

SECTOR = "producteurs"
CHECK_DIR = PROJECT_ROOT / "exports" / SECTOR / "checkpoints"
OUT_DIR = PROJECT_ROOT / "exports" / SECTOR
# Shared on purpose — see module docstring.
QUOTA_PATH = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints" / "search_quota.json"
# Earlier sectors' harvests, read in place (never copied, never written to).
INHERITED_AGRI = PROJECT_ROOT / "exports" / "agriculteurs" / "checkpoints"
INHERITED_ELEVEURS = PROJECT_ROOT / "exports" / "eleveurs" / "checkpoints"

DEPARTEMENTS = ("03", "63")

# NAF code -> (official label, sous_segment tag). Livestock (01.4x) is M6.
# Codes with no French metropolitan relevance (riz, canne, tropicaux,
# agrumes) are left out; brasseries (11.05Z) are not agriculture.
NAF_SCOPE = {
    "01.11Z": ("Culture de céréales, légumineuses et graines oléagineuses", "grandes cultures"),
    "01.13Z": ("Culture de légumes, de melons, de racines et de tubercules", "maraîcher"),
    "01.15Z": ("Culture du tabac", "cultures"),
    "01.16Z": ("Culture de plantes à fibres", "cultures"),
    "01.19Z": ("Autres cultures non permanentes", "cultures"),
    "01.21Z": ("Culture de la vigne", "vigneron"),
    "01.24Z": ("Culture de fruits à pépins et à noyau", "arboriculteur"),
    "01.25Z": ("Culture d'autres fruits d'arbres ou d'arbustes et de fruits à coque", "arboriculteur"),
    "01.26Z": ("Culture de fruits oléagineux", "arboriculteur"),
    "01.28Z": ("Culture de plantes à épices, aromatiques, médicinales et pharmaceutiques", "plantes aromatiques"),
    "01.29Z": ("Autres cultures permanentes", "cultures"),
    "01.30Z": ("Reproduction de plantes", "pépiniériste"),
    "01.61Z": ("Activités de soutien aux cultures", "travaux agricoles"),
    "01.62Z": ("Activités de soutien à la production animale", "travaux agricoles"),
    "01.63Z": ("Traitement primaire des récoltes", "transformation"),
    "01.64Z": ("Traitement des semences", "transformation"),
    "03.22Z": ("Aquaculture en eau douce", "aquaculture"),
    "10.11Z": ("Transformation et conservation de la viande de boucherie", "transformation viande"),
    "10.12Z": ("Transformation et conservation de la viande de volaille", "transformation viande"),
    "10.13A": ("Préparation industrielle de produits à base de viande", "transformation viande"),
    "10.13B": ("Charcuterie", "transformation viande"),
    "10.32Z": ("Préparation de jus de fruits et légumes", "transformation fruits-légumes"),
    "10.39A": ("Autre transformation et conservation de légumes", "transformation fruits-légumes"),
    "10.39B": ("Transformation et conservation de fruits", "transformation fruits-légumes"),
    "10.41A": ("Fabrication d'huiles et graisses brutes", "huilerie"),
    "10.51A": ("Fabrication de lait liquide et de produits frais", "laiterie"),
    "10.51B": ("Fabrication de beurre", "laiterie"),
    "10.51C": ("Fabrication de fromage", "fromager"),
    "10.51D": ("Fabrication d'autres produits laitiers", "laiterie"),
    "10.61A": ("Meunerie", "meunerie"),
    "10.61B": ("Autres activités du travail des grains", "meunerie"),
    "11.02A": ("Fabrication de vins effervescents", "vigneron"),
    "11.02B": ("Vinification", "vigneron"),
    "11.03Z": ("Fabrication de cidre et de vins de fruits", "cidre"),
}
NAF_LABELS = {k: v[0] for k, v in NAF_SCOPE.items()}
NAF_TAG = {k: v[1] for k, v in NAF_SCOPE.items()}

# What a directory's own category word means in our vocabulary. Applied to
# the `categorie` column of every listing (producteur.direct prints
# "Éleveur", acheteralasource "Fromages et produits laitiers", VinUp
# "Producteur viticulteur"…). Every matching rule contributes a tag.
CATEGORY_RULES = (
    ("vigneron",            re.compile(r"VITICULT|VIGNERON|VIGNOBLE|VINIFI|DOMAINE VITICOLE|\bVINS?\b", re.I)),
    ("fromager",            re.compile(r"FROMAG", re.I)),
    ("laiterie",            re.compile(r"LAITIER|LAITERIE|PRODUITS LAITIERS|YAOURT|BEURRE", re.I)),
    ("éleveur",             re.compile(r"[EÉ]LEV(EUR|AGE)|VIANDE|VOLAILLE|[OŒ]UFS?\b|BOVIN|OVIN|CAPRIN|PORC", re.I)),
    ("apiculteur",          re.compile(r"APICULT|MIEL", re.I)),
    ("maraîcher",           re.compile(r"MARA[IÎ]CH|L[EÉ]GUMES?", re.I)),
    ("arboriculteur",       re.compile(r"ARBORICULT|FRUITS?\b|VERGER", re.I)),
    ("plantes aromatiques", re.compile(r"AROMATIQ|PPAM|PLANTES M[EÉ]DICINALES|TISANE|SAFRAN", re.I)),
    ("pépiniériste",        re.compile(r"P[EÉ]PINI|HORTICULT|FLEURS?\b", re.I)),
    ("huilerie",            re.compile(r"OL[EÉ]ICULT|HUILE", re.I)),
    ("cidre",               re.compile(r"CIDRE|POMM[EÉ]", re.I)),
    ("meunerie",            re.compile(r"MEUNERIE|MOULIN|FARINE", re.I)),
    ("transformation",      re.compile(r"ARTISAN|TRANSFORM|CONSERVE|CONFITURE|JUS\b", re.I)),
    ("boulanger",           re.compile(r"BOULANG|PAIN\b", re.I)),
    ("brasseur",            re.compile(r"BRASS|BI[EÈ]RE", re.I)),
)

# Canonical listing schema (m3ag_s10_baf.FIELDNAMES) + 4 M7 columns.
LISTING_FIELDS = ["listing_id", "dept", "name", "alt_name", "phone", "mobile", "email",
                  "website", "city", "postcode", "address", "url",
                  "description", "productions", "categorie", "siret"]

PHONE_SOURCE_FR = {
    "agencebio": "déclaré par l'exploitant (Agence Bio)",
    "osm": "OpenStreetMap",
    "bienvenue_ferme": "fiche Bienvenue à la ferme",
    "jours_de_marche": "fiche Jours de marché",
    "acheteralasource": "fiche Acheter à la source",
    "producteur_direct": "fiche producteur.direct",
    "fermes_locales": "fiche Fermes locales",
    "vinup": "fiche VinUp",
    "vignerons_indep": "fiche Vignerons Indépendants",
    "bonfromager": "fiche Bon Fromager",
    "denosfermes63": "annuaire De nos fermes 63",
    "pagesjaunes": "Pages Jaunes",
    "site/mentions_legales": "mentions légales du site de l'exploitation",
    "site/confirme": "site web de l'exploitation",
    "corrobore": "2 sources indépendantes concordantes",
    "provider": "fichier fournisseur (non vérifié)",
}

M7_STOPWORDS = set(AGRI_STOPWORDS) | ELEVAGE_GENERICS | {
    "VIGNERON", "VIGNERONS", "VIGNOBLE", "VIGNOBLES", "VIGNE", "VIGNES", "CAVE", "CAVES",
    "CHATEAU", "CLOS", "VINS", "VIN", "FROMAGERIE", "FROMAGES", "FROMAGE", "LAITERIE",
    "MOULIN", "VERGERS", "PEPINIERE", "PEPINIERES", "HORTICULTURE", "SERRES",
    "CEREALES", "CULTURE", "CULTURES", "SAINT", "POURCAIN", "SIOULE",
}


# ---------------------------------------------------------------- helpers
def name_tokens(*names: str) -> set:
    """Distinctive tokens of a farm's names (enseigne, raison sociale, gérant…)."""
    toks = set()
    for n in names:
        toks |= {t for t in norm(n).split() if len(t) >= 3 and t not in M7_STOPWORDS}
    return toks


def strong_tokens(toks: set) -> set:
    return {t for t in toks if len(t) >= 4 and t not in COMMON_FIRST_NAMES}


def is_aggregator(url_or_host: str) -> bool:
    h = host_of(url_or_host) if "://" in (url_or_host or "") else (url_or_host or "").lower()
    if not h:
        return True
    if h.endswith(BAD_TLD):
        return True
    return any(a in h for a in AGRI_AGGREGATORS)


def tag_from_naf(naf: str) -> str:
    return NAF_TAG.get((naf or "").strip(), "hors scope")


def tags_from_category(text: str) -> list[str]:
    """Every sous-segment a directory category / product list points to."""
    out = []
    for tag, rx in CATEGORY_RULES:
        if rx.search(text or "") and tag not in out:
            out.append(tag)
    return out


def merge_tags(*groups) -> str:
    """'|'-joined, ordered, deduplicated sous-segment tags."""
    seen: list[str] = []
    for g in groups:
        items = g.split("|") if isinstance(g, str) else (g or [])
        for t in items:
            t = (t or "").strip()
            if t and t != "hors scope" and t not in seen:
                seen.append(t)
    return "|".join(seen)


def dept_of_cp(cp: str) -> str:
    cp = re.sub(r"\D", "", cp or "")
    if len(cp) != 5:
        return ""
    if cp.startswith("200") or cp.startswith("201"):
        return "2A"
    if cp.startswith("202") or cp.startswith("206"):
        return "2B"
    return cp[:2]


# ---------------------------------------------------------------- population
def row_id(op: dict) -> str:
    return op.get("siret") or f"X{op.get('siren', '')}"


def load_operators(dept: str) -> list[dict]:
    """operateurs_<dept>.csv rows plus adapter keys (prefixed '_')."""
    p = CHECK_DIR / f"operateurs_{dept}.csv"
    if not p.exists():
        sys.exit(f"{p} missing — run m7_s2_transform.py --departements {dept} first.")
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

    check("tag vigne", tag_from_naf("01.21Z"), "vigneron")
    check("tag vinification", tag_from_naf("11.02B"), "vigneron")
    check("tag fromage", tag_from_naf("10.51C"), "fromager")
    check("tag livestock is M6", tag_from_naf("01.42Z"), "hors scope")
    check("category producteur.direct", tags_from_category("Éleveur"), ["éleveur"])
    check("category acheteralasource", tags_from_category("Fromages et produits laitiers, Légumes"),
          ["fromager", "laiterie", "maraîcher"])
    check("category vinup", tags_from_category("Producteur viticulteur"), ["vigneron"])
    check("category empty", tags_from_category(""), [])
    check("merge tags", merge_tags("vigneron", ["fromager", "vigneron"], "hors scope"), "vigneron|fromager")
    check("stopwords drop wine generics", name_tokens("DOMAINE DES BERIOLES", "Vignoble de Saint-Pourçain"),
          {"BERIOLES"})
    check("dept of cp", dept_of_cp("03500"), "03")
    check("dept of corsica", dept_of_cp("20200"), "2B")
    check("dept of bad cp", dept_of_cp("3500"), "")
    check("aggregator acheteralasource", is_aggregator("https://www.acheteralasource.com/producteur/1"), True)
    check("own site passes", is_aggregator("https://lesberioles.com/"), False)
    check("public stays", is_public("7210", "COMMUNE DE MURAT"), True)
    check("listing fields count", len(LISTING_FIELDS), 16)
    print(f"selftest: {fails} failure(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    print(__doc__)
