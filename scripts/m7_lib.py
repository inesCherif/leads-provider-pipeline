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
  * EXCLUDED_NAF / EXCLUDED_RE — Ines's principle (2026-09-11, overrides
    Sam's list): NO ALCOHOL (vigne 01.21Z, vinification 11.02A/B, cidre
    11.03Z, brasserie 11.05Z, every "vins" category, the wine directories)
    and NO PORK-SPECIFIC code (charcuterie 10.13B, porcins 01.46Z). Never
    pulled, never merged into Sam's file, never tagged. The m7_s12 gate
    fails the build if one of them reaches the xlsx.
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

# Excluded on principle (see docstring). Kept as data so every script can
# assert against it: m7_s1 refuses to pull them, m7_s9 --with-eleveurs
# drops M6 rows carrying them, m7_s12 fails on them.
EXCLUDED_NAF = frozenset({"01.21Z", "11.02A", "11.02B", "11.03Z", "11.05Z", "10.13B", "01.46Z"})
# Strict form (harvest + population): word-bounded, so "La Cave aux
# Fromages" (a cheese cave), "BRASSAC" (a commune) and "Bonnichon" pass.
EXCLUDED_RE = re.compile(
    r"VITICULT|VIGNERON|VIGNOBLE|VINIFI|\bVINS?\b|\bCAVES?\s+(?:COOP|VITI|[AÀ]\s+VINS?|DES?\s+VINS?|DU\s+VIN)|"
    r"\bCIDRE|\bBRASSERIE|\bBRASSEUR|\bBI[EÈ]RES?\b|CHARCUT|\bPORCS?\b|PORCIN|COCHON|"
    # spirits (LA RHUMERIE DIVANA reached the 03 Sans SIRET tab on 2026-09-11; a lavender
    # "distillerie" is PPAM, so DISTILL stays in the loose form only)
    r"\bRHUM\b|RHUMERIE|SPIRITUEU|LIQUEUR|WHISK|EAUX?[- ]DE[- ]VIE|\bALCOOL",
    re.I)
# Loose form (the Sans SIRET tab only, where no NAF can vouch for the row):
# any wine / cellar / beer / pork word anywhere in name, category or text.
EXCLUDED_LOOSE_RE = re.compile(
    r"VITICULT|VIGNERON|VIGNOBLE|VINIFI|\bVINS?\b|\bVIGNES?\b|\bCAVES?\b|CIDRE|BRASSERIE|BRASSEUR|BI[EÈ]RE|"
    r"CHARCUT|\bPORCS?\b|PORCIN|COCHON|SPIRITUEU|DISTILLERIE|LIQUEUR|\bALCOOL|\bRHUM|WHISK|EAUX?[- ]DE[- ]VIE", re.I)
EXCLUDED_HOST_WORDS = {"vin", "vins", "vigne", "vignes", "vignoble", "vignobles", "vigneron", "vignerons",
                       "cave", "caves", "chateau", "biere", "bieres", "brasserie", "brasseur", "charcuterie",
                       "charcutier", "porc", "porcs", "cochon", "cochons", "distillerie", "spiritueux",
                       "rhum", "rhumerie", "whisky", "liqueur", "liqueurs", "alcool"}
# A directory registrant that is no producer at all (measured 2026-09-11 on producteur.direct:
# a tyre shop tagged "Arboriculteur", a landscaper tagged "Maraîcher", a logistics firm, a
# house builder, a paving franchise). Applied to the Sans SIRET tab only — a registry row has
# its NAF to vouch for it. Word-bounded; "Maison Delherme" (a producer) must pass.
NON_PRODUCER_RE = re.compile(
    r"\bPNEUS?\b|PAYSAGIST|\bPAYSAGES\b|LOGISTIQUE|\bTRANSPORTS?\b|IMMOBILI|ASSURANCE|\bGARAGE\b|COIFFURE|"
    r"PLOMBERIE|[ÉE]LECTRICIT[ÉE]|MENUISERIE|CARRELAGE|TERRASSEMENT|\bALL[ÉE]ES\b|B[ÂA]TIMENT|CONSTRUCTION|"
    r"NETTOYAGE|INFORMATIQUE|\bTAXI\b|AMBULANCE|PHARMACIE|INFIRMI|DENTISTE|AVOCAT|NOTAIRE|\bBANQUE\b|"
    r"SUPERMARCH|HYPERMARCH|\bTABAC\b|\bHOME CONCEPTION\b|EUROTYRE", re.I)

# NAF code -> (official label, sous_segment tag). Livestock (01.4x) is M6.
# Codes with no French metropolitan relevance (riz, canne, tropicaux,
# agrumes) are left out.
NAF_SCOPE = {
    "01.11Z": ("Culture de céréales, légumineuses et graines oléagineuses", "grandes cultures"),
    "01.13Z": ("Culture de légumes, de melons, de racines et de tubercules", "maraîcher"),
    "01.15Z": ("Culture du tabac", "cultures"),
    "01.16Z": ("Culture de plantes à fibres", "cultures"),
    "01.19Z": ("Autres cultures non permanentes", "cultures"),
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
}
NAF_LABELS = {k: v[0] for k, v in NAF_SCOPE.items()}
NAF_TAG = {k: v[1] for k, v in NAF_SCOPE.items()}
assert not (EXCLUDED_NAF & set(NAF_SCOPE)), "an excluded NAF code is in NAF_SCOPE"

# What a directory's own category word means in our vocabulary. Applied to
# the `categorie` column of every listing (producteur.direct prints
# "Éleveur", acheteralasource "Fromages et produits laitiers", VinUp
# "Producteur viticulteur"…). Every matching rule contributes a tag. Wine,
# cider, beer and charcuterie words are NOT rules: they never become a tag
# (EXCLUDED_RE), the listing simply carries no tag from them.
CATEGORY_RULES = (
    ("fromager",            re.compile(r"FROMAG", re.I)),
    ("laiterie",            re.compile(r"LAITIER|LAITERIE|PRODUITS LAITIERS|YAOURT|BEURRE", re.I)),
    ("éleveur",             re.compile(r"[EÉ]LEV(EUR|AGE)|VIANDE|VOLAILLE|[OŒ]UFS?\b|BOVIN|OVIN|CAPRIN|PORC", re.I)),
    ("apiculteur",          re.compile(r"APICULT|MIEL", re.I)),
    ("maraîcher",           re.compile(r"MARA[IÎ]CH|L[EÉ]GUMES?", re.I)),
    ("arboriculteur",       re.compile(r"ARBORICULT|FRUITS?\b|VERGER", re.I)),
    ("plantes aromatiques", re.compile(r"AROMATIQ|PPAM|PLANTES M[EÉ]DICINALES|TISANE|SAFRAN", re.I)),
    ("pépiniériste",        re.compile(r"P[EÉ]PINI|HORTICULT|FLEURS?\b", re.I)),
    ("huilerie",            re.compile(r"OL[EÉ]ICULT|HUILE", re.I)),
    ("meunerie",            re.compile(r"MEUNERIE|MOULIN|FARINE", re.I)),
    ("transformation",      re.compile(r"ARTISAN|TRANSFORM|CONSERVE|CONFITURE|JUS\b", re.I)),
    ("boulanger",           re.compile(r"BOULANG|PAIN\b", re.I)),
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
    "bonfromager": "fiche Bon Fromager",
    "denosfermes63": "annuaire De nos fermes 63 (Conseil départemental)",
    "artisans_vegetal": "fiche Les Artisans du Végétal",
    "annuairefrancais": "annuaire annuairefrancais.fr",
    "pagesjaunes": "Pages Jaunes",
    "site/mentions_legales": "mentions légales du site de l'exploitation",
    "site/confirme": "site web de l'exploitation",
    "corrobore": "2 sources indépendantes concordantes",
    "provider": "fichier fournisseur (non vérifié)",
    "social_fb": "page Facebook de l'exploitation",
}

M7_STOPWORDS = set(AGRI_STOPWORDS) | ELEVAGE_GENERICS | {
    "FROMAGERIE", "FROMAGES", "FROMAGE", "LAITERIE", "MOULIN", "VERGERS",
    "PEPINIERE", "PEPINIERES", "HORTICULTURE", "SERRES", "CEREALES", "CULTURE",
    "CULTURES", "SAINT", "SIOULE",
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
    naf = (naf or "").strip()
    if naf in EXCLUDED_NAF:
        return "exclu"
    return NAF_TAG.get(naf, "hors scope")


def is_excluded(naf: str = "", *texts: str) -> bool:
    """True when a NAF or a name / category / product text hits the principle."""
    if (naf or "").strip() in EXCLUDED_NAF:
        return True
    return any(EXCLUDED_RE.search(t or "") for t in texts)


def tags_from_category(text: str) -> list[str]:
    """Every sous-segment a directory category / product list points to.
    Wine / cider / beer / charcuterie words produce nothing."""
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
            if t and t not in ("hors scope", "exclu") and t not in seen:
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


# Excluded words that are ALSO ordinary French family names. A trade word
# (CHARCUT, VIGNOBLE, VITICULT, BRASSERIE) never is — nobody is called
# Charcuterie. Measured across France 2026-09-19: the name test dropped 598
# rows, and VIGNERON (117) and BRASSEUR (37) were the two that kept catching
# farmers: DAVID BRASSEUR 01.62Z, PIERRE BRASSEUR 01.42Z, MONIQUE BRASSEUR
# 01.11Z, A. VIGNERON 01.19Z.
SURNAME_WORDS = frozenset({"VIGNERON", "VIGNERONS", "BRASSEUR", "BRASSEURS",
                           "PINOT", "COCHON", "VIGNE", "VIGNES", "CAVE", "CAVES"})
# The codes that make a wine / cider / beer / pork match a fact about the
# ACTIVITY rather than an accident of spelling.
WINE_PORK_NAF_RE = re.compile(r"^(01\.21|11\.0|10\.13B|01\.46)")


def name_hit_is_surname(word: str, naf: str) -> bool:
    """Ines's rule, 2026-09-19: keep the row when the excluded word is one that
    is also a family name AND the NAF is not wine / cider / beer / pork.

    The principle is about what a business DOES, and the NAF states that; the
    name is only how it is spelled. So a Monsieur Brasseur registered under
    01.11Z (céréales) is a prospect, and a SAS carrying 11.05Z is not, whatever
    either of them is called. Applying it as a rule rather than a list of 92
    SIRETs means it also covers the next département and the next refresh."""
    return (word or "").upper() in SURNAME_WORDS and not WINE_PORK_NAF_RE.match((naf or "").strip())


PRINCIPLE_RESCUE_PATH = PROJECT_ROOT / "config" / "principle_rescue.csv"


def load_principle_rescue() -> dict[str, str]:
    """SIRETs Ines has reviewed and confirmed are legitimate although their
    NAME matches an excluded word — almost always a surname colliding with a
    trade word (Vigneron, Brasseur and Pinot are ordinary French names; dept 63
    holds A. VIGNERON and CAMILLE VIGNERON, both NAF 01.19Z).

    This rescues a row from the NAME test only. An excluded NAF code, an
    excluded sous-segment or a wine / pork contact domain is still refused —
    those are facts about the activity, not an accident of spelling."""
    out: dict[str, str] = {}
    if not PRINCIPLE_RESCUE_PATH.exists():
        return out
    for line in PRINCIPLE_RESCUE_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("siret;"):
            continue
        parts = line.split(";")
        siret = re.sub(r"\D", "", parts[0])
        if siret:
            out[siret] = parts[1] if len(parts) > 1 else ""
    return out


def host_hits(host_or_domain: str) -> str:
    """The excluded WORD found in a host / mail domain split on '-', '.'
    and digits ('sauvat-vins.com' -> 'vins'; 'chevrespoitevines.fr' -> '')."""
    for tok in re.split(r"[^a-z]+", (host_or_domain or "").lower()):
        if tok in EXCLUDED_HOST_WORDS:
            return tok
    return ""


# Strong words only for the free-text description: "vin" alone would drop a
# cheese farm that mentions a wine pairing.
EXCLUDED_DESC_RE = re.compile(r"vigneron|viticult|vignoble|domaine viticole|vinifi|brasserie artisanale|"
                              r"micro-?brasserie|charcuterie artisanale|élevage de porcs?|elevage de porcs?|"
                              r"cochons? fermier", re.I)


def listing_excluded(name: str, categorie: str, website: str = "", email: str = "",
                     description: str = "") -> str:
    """Why a directory listing is skipped at harvest ('' = kept): its NAME,
    its own WEBSITE host, its E-MAIL domain or a strong DESCRIPTION word
    hits the principle (a brewery, `sauvat-vins.com`,
    `alexandre@vins-arbogast.fr`, "vigneron indépendant"), or its category
    is ONLY about excluded products. A mixed farm (cheese + a wine line)
    still passes: its in-scope tags are non-empty. Returns the reason so
    the harvest log says which field fired."""
    m = EXCLUDED_RE.search(name or "")
    if m:
        return f"nom:{m.group(0)}"
    m = EXCLUDED_DESC_RE.search(description or "")
    if m:
        return f"description:{m.group(0)}"
    host = host_of(website) if website else ""
    if host and host_hits(host):
        return f"site:{host}"
    dom = (email or "").rsplit("@", 1)[-1].lower() if "@" in (email or "") else ""
    if dom and host_hits(dom):
        return f"email:{dom}"
    m = EXCLUDED_RE.search(categorie or "")
    if m and not tags_from_category(categorie):
        return f"categorie:{m.group(0)}"
    return ""


# ---------------------------------------------------------------- harvest helpers
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PAGE_DELAY = 1.2
LISTING_DELAY = 0.8


def make_session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    return s


def get(sess, url: str, log=None, timeout: int = 40) -> str:
    """3 attempts, 3/6/9 s backoff; '' on failure (caller never marks done)."""
    import time
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return ""
            if log:
                log.warning(f"HTTP {r.status_code} {url}")
        except Exception as exc:
            if log:
                log.warning(f"{type(exc).__name__} {url}")
        time.sleep(3 * (attempt + 1))
    return ""


def strip(s: str) -> str:
    import html as htmlmod
    return " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def first_phone_pair(phones: list[str]) -> tuple[str, str]:
    """(phone, mobile): first number, then the first DIFFERENT one."""
    phones = [p for p in phones if p]
    phone = phones[0] if phones else ""
    mobile = next((p for p in phones[1:] if p != phone), "")
    return phone, mobile


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

    check("tag vigne is excluded", tag_from_naf("01.21Z"), "exclu")
    check("tag vinification is excluded", tag_from_naf("11.02B"), "exclu")
    check("tag charcuterie is excluded", tag_from_naf("10.13B"), "exclu")
    check("tag porcins is excluded", tag_from_naf("01.46Z"), "exclu")
    check("tag fromage", tag_from_naf("10.51C"), "fromager")
    check("tag livestock is M6", tag_from_naf("01.42Z"), "hors scope")
    check("excluded by naf", is_excluded("11.03Z"), True)
    check("excluded by text", is_excluded("", "GAEC X", "Vins et charcuterie"), True)
    check("not excluded", is_excluded("10.51C", "GAEC DU BOIS JOLI", "Fromages"), False)
    check("category producteur.direct", tags_from_category("Éleveur"), ["éleveur"])
    check("category acheteralasource", tags_from_category("Fromages et produits laitiers, Légumes"),
          ["fromager", "laiterie", "maraîcher"])
    check("category wine gives no tag", tags_from_category("Producteur viticulteur, vins, cidre, bière"), [])
    check("category charcuterie gives no tag", tags_from_category("Charcuterie"), [])
    check("category empty", tags_from_category(""), [])
    check("listing: brewery skipped", listing_excluded("Bières Le Plan B", "Boissons"), "nom:Bières")
    check("listing: cheese cave passes", listing_excluded("La Cave aux Fromages de Pierre", "Fromagerie"), "")
    check("listing: wine coop cave skipped", listing_excluded("CAVE COOPERATIVE SAINT VERNY", ""), "nom:CAVE COOP")
    check("listing: Brassac commune passes", listing_excluded("GAEC DE BRASSAC", "Fromages"), "")
    check("listing: poitevines host passes", listing_excluded("Aurelien Robert", "Fromager", "https://chevrespoitevines.fr"), "")
    check("loose: cave anywhere", bool(EXCLUDED_LOOSE_RE.search("La Cave aux Fromages")), True)
    check("listing: wine-only category skipped", listing_excluded("Domaine X", "Vins"), "categorie:Vins")
    check("listing: mixed farm passes", listing_excluded("Ferme des Volcans", "Fromages - Vins"), "")
    check("listing: plain farm passes", listing_excluded("GAEC DES OLIVIERS", "Fromages"), "")
    check("listing: wine host skipped", listing_excluded("SCEA SAUVAT", "Fruits - Vins", "https://www.sauvat-vins.com/"), "site:sauvat-vins.com")
    check("listing: farm host passes", listing_excluded("GAEC X", "Fromages", "http://chevreriedesoliviers.fr"), "")
    check("listing: wine e-mail domain skipped", listing_excluded("DOMAINE ARBOGAST", "", "", "alexandre@vins-arbogast.fr"), "email:vins-arbogast.fr")
    check("listing: rum shop skipped", listing_excluded("LA RHUMERIE DIVANA", ""), "nom:RHUMERIE")
    check("listing: lavender distillery passes (strict)", listing_excluded("Distillerie des Lavandes", "Plantes aromatiques"), "")
    check("loose: distillerie anywhere", bool(EXCLUDED_LOOSE_RE.search("Distillerie des Lavandes")), True)
    check("loose: rhum anywhere", bool(EXCLUDED_LOOSE_RE.search("Rhum arrangé maison")), True)
    check("non-producer: tyre shop", bool(NON_PRODUCER_RE.search("LD PNEUS 03 - Eurotyre Arboriculteur")), True)
    check("non-producer: landscaper", bool(NON_PRODUCER_RE.search("Dereure Paysages Maraîcher")), True)
    check("non-producer: Maison Delherme passes", bool(NON_PRODUCER_RE.search("Maison Delherme")), False)
    check("non-producer: transport in a farm name passes", bool(NON_PRODUCER_RE.search("GAEC DES TRANSPORTEURS")), False)
    check("listing: orange mailbox passes", listing_excluded("Les Biquettes", "", "", "biquettes@orange.fr"), "")
    check("listing: vigneron in description skipped",
          listing_excluded("Domaine EDEL", "", "", "edel@online.fr", "Vigneron indépendant en Alsace"), "description:Vigneron")
    check("listing: 'vin' alone in description passes",
          listing_excluded("Ferme X", "Fromages", "", "", "nos fromages s'accordent avec un vin blanc"), "")
    check("phone pair", first_phone_pair(["04 70 45 40 83", "04 70 45 40 83", "06 63 70 05 04"]),
          ("04 70 45 40 83", "06 63 70 05 04"))
    check("merge tags", merge_tags("maraîcher", ["fromager", "maraîcher"], "hors scope", "exclu"),
          "maraîcher|fromager")
    check("stopwords drop generics", name_tokens("FROMAGERIE DU BOIS JOLI", "Ferme de Saint-Nectaire"),
          {"JOLI", "NECTAIRE"})
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
