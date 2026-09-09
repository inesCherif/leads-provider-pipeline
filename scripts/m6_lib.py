"""
m6_lib — shared paths, population adapter and livestock vocabulary for m6_s*
===========================================================================
Sector 6 (M6): ÉLEVEURS (livestock farmers), départements 03 and 63, built
2026-09-09 for Maha's call sheet at her request (phones first, e-mails
second, nothing she already received). Same shape as `m5_lib` / `m3ag_lib`
so the sector-neutral M3AG scripts can be wrapped by overriding their module
globals (the `m5_s7_pagesjaunes.py` pattern).

Rules carried over (measured in M2 / M3AG / M5, kept):
  * search_quota.json stays SHARED with the boulangerie tree — credits are
    account-wide; a second counter would double-spend a free tier.
  * geo gate per RESULT, never on a merged blob.
  * a name token must appear in the result title/url.
  * the population adapter `operateurs_<dept>.csv` is COMMA-delimited (the
    m3ag_s2 contract), every harvest checkpoint is ';'.

What is specific to M6:
  * NAF_SCOPE = the nine livestock codes (01.41Z … 01.50Z), all included and
    TAGGED (Ines 2026-09-09): `type_elevage` from the NAF.
  * PET_RE flags dog / cat breeders (NAF 01.49Z covers them; M1 measured
    1,714 "eleveur chien chat" rows in the provider file). Flagged, never
    dropped from OUR file; excluded from Maha's sheet.
  * INHERITED_DIR: the M3AG harvests (Pages Jaunes dept 63, OSM,
    bienvenue-à-la-ferme, crawl, SMTP verdicts) are read IN PLACE — PJ lists
    every farmer, not only bio ones, so they are witnesses for M6 too.
  * SENT_FILES: the four téléop files Maha already holds. Every SIRET and
    every phone in them is excluded from the M6 call sheet (Maha: no
    duplicates across the files she receives).

Usage:
    python scripts/m6_lib.py --selftest
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
    EMAIL_RE, AGRI_AGGREGATORS, AGRI_STOPWORDS,
)

SECTOR = "eleveurs"
CHECK_DIR = PROJECT_ROOT / "exports" / SECTOR / "checkpoints"
OUT_DIR = PROJECT_ROOT / "exports" / SECTOR
# Shared on purpose — see module docstring.
QUOTA_PATH = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints" / "search_quota.json"
# M3AG harvests, read in place (never copied, never written to from M6).
INHERITED_DIR = PROJECT_ROOT / "exports" / "agriculteurs" / "checkpoints"
# Files Maha already holds — exclusion list for the call sheet: every xlsx
# in exports/maha_sent/ (any sector, any version). See scripts/maha_lib.py.
from maha_lib import SENT_DIR as MAHA_SENT_DIR, load_sent, sent_files   # noqa: E402,F401

DEPARTEMENTS = ("03", "63")
NAF_SCOPE = {"01.41Z", "01.42Z", "01.43Z", "01.44Z", "01.45Z", "01.46Z", "01.47Z", "01.49Z", "01.50Z"}
NAF_LABELS = {
    "01.41Z": "Élevage de vaches laitières",
    "01.42Z": "Élevage d'autres bovins et de buffles",
    "01.43Z": "Élevage de chevaux et d'autres équidés",
    "01.44Z": "Élevage de chameaux et d'autres camélidés",
    "01.45Z": "Élevage d'ovins et de caprins",
    "01.46Z": "Élevage de porcins",
    "01.47Z": "Élevage de volailles",
    "01.49Z": "Élevage d'autres animaux",
    "01.50Z": "Culture et élevage associés",
}
ELEVAGE_TYPE = {
    "01.41Z": "bovins lait",
    "01.42Z": "bovins viande",
    "01.43Z": "équins",
    "01.44Z": "autres animaux",
    "01.45Z": "ovins-caprins",
    "01.46Z": "porcins",
    "01.47Z": "volailles",
    "01.49Z": "autres animaux",
    "01.50Z": "polyculture-élevage",
}

# Livestock generics repeat across every commune: a title match on one of
# these is not a match on THIS farm. Union with the M3AG list (legal forms,
# farm generics, geography of 03/63).
ELEVAGE_GENERICS = {
    "ELEVAGES", "ELEVEUR", "ELEVEURS", "BOVIN", "BOVINS", "OVIN", "OVINS", "CAPRIN",
    "CAPRINS", "PORCIN", "PORCINS", "PORC", "PORCS", "VOLAILLE", "VOLAILLES",
    "CHEVAL", "CHEVAUX", "HARAS", "ECURIE", "ECURIES", "EQUESTRE", "PONEY", "PONEYS",
    "LAIT", "LAITIER", "LAITIERE", "LAITIERS", "LAITIERES", "VACHE", "VACHES",
    "MOUTON", "MOUTONS", "BREBIS", "CHEVRE", "CHEVRES", "AGNEAU", "AGNEAUX",
    "POULE", "POULES", "POULET", "POULETS", "OEUF", "OEUFS", "VIANDE", "VIANDES",
    "PRAIRIE", "PRAIRIES", "PATURAGE", "HERBE", "TROUPEAU", "TERRE", "TERRES",
    "AGRI", "AGRO", "SALERS", "CHAROLAIS", "CHAROLAISE", "AUBRAC", "LIMOUSINE",
    "MONTBELIARDE", "HOLSTEIN", "ABONDANCE",
    # geography of 03/63 the M5 hand-check added on top of the M3AG list
    "MASSIF", "CENTRAL", "CHAINE", "PUYS", "SIOULE", "DORE", "CEZALLIER",
    "MONTAGNE", "MONTAGNES", "PLATEAU", "VALLEE", "MONTS", "BOIS",
}
M6_STOPWORDS = set(AGRI_STOPWORDS) | ELEVAGE_GENERICS

# Vocabulary that says "this page is a farm's own page" (for crawl scoring).
ELEVAGE_WORDS = (
    "élevage", "elevage", "éleveur", "eleveur", "ferme", "exploitation", "troupeau",
    "vaches", "vache", "bovin", "bovins", "brebis", "chèvre", "chevre", "chèvres",
    "moutons", "agneau", "porc", "porcs", "cochon", "volaille", "volailles", "poules",
    "poulet", "œufs", "oeufs", "lait", "laitier", "fromage", "viande", "vente directe",
    "vente à la ferme", "pâturage", "paturage", "prairie", "herbe", "salers",
    "charolais", "aubrac", "limousine", "montbéliarde", "cheval", "chevaux", "haras",
    "poulain", "gaec", "earl", "agriculteur", "agricole", "bio", "aop", "label rouge",
)

# Dog / cat breeders and pet trades: NAF 01.49Z covers them. A hit on the
# NAME flags the row (flag_animaux_compagnie); nothing is dropped from OUR
# file, the row simply never reaches Maha's sheet.
PET_RE = re.compile(
    r"\b(CHIEN|CHIENS|CHIOT|CHIOTS|CHAT|CHATS|CHATON|CHATONS|CANIN|CANINE|CANINS|"
    r"FELIN|FELINE|FELINS|CHENIL|CHATTERIE|PENSION\s+CANINE|TOILETTAGE|DRESSAGE|"
    r"EDUCATEUR\s+CANIN|COMPORTEMENTALISTE|NAC|RONGEUR|RONGEURS|REPTILE|REPTILES|"
    r"AQUARIUM|AQUARIOPHILIE|ANIMALERIE|"
    r"BERGER\s+(ALLEMAND|AUSTRALIEN|BELGE|BLANC|MALINOIS|DES\s+PYRENEES|SUISSE)|"
    r"LABRADOR|GOLDEN\s+RETRIEVER|RETRIEVER|BOULEDOGUE|BULLDOG|CAVALIER\s+KING|"
    r"SPITZ|TECKEL|BEAUCERON|MALINOIS|HUSKY|CANICHE|BICHON|CHIHUAHUA|BOXER|"
    r"ROTTWEILER|DOBERMAN|SHIBA|AKITA|BORDER\s+COLLIE|COCKER|EPAGNEUL|SETTER|"
    r"BEAGLE|JACK\s+RUSSELL|YORKSHIRE|SHIH\s+TZU|CARLIN|DALMATIEN|SAMOYEDE|"
    r"STAFFORDSHIRE|STAFFIE|AMSTAFF|CANE\s+CORSO|DOGUE|MASTIFF|LEONBERG|TERRE\s+NEUVE|"
    r"BENGAL|MAINE\s+COON|RAGDOLL|SPHYNX|PERSAN|SIAMOIS|BRITISH\s+SHORTHAIR|"
    r"SACRE\s+DE\s+BIRMANIE|NORVEGIEN|SIBERIEN|ABYSSIN|CHARTREUX)\b", re.I)

# Public-law operators: communes, lycées agricoles, research stations,
# chambres d'agriculture. Flagged (Maha calls a farmer, not an institution).
PUBLIC_NAME_RE = re.compile(
    r"\b(COMMUNE\s+DE|MAIRIE|COMMUNAUTE\s+DE\s+COMMUNES|COMMUNAUTE\s+D\W?AGGLOMERATION|"
    r"SIVOM|SIVU|SYNDICAT|REGIE|CCAS|DEPARTEMENT\s+D|CONSEIL\s+DEPARTEMENTAL|"
    r"ETABLISSEMENT\s+PUBLIC|EPCI|COLLECTIVITE|LYCEE|EPLEFPA|EPL\b|LEGTA|CFPPA|"
    r"INRAE|INRA\b|CHAMBRE\s+D\W?AGRICULTURE|MFR\b|MAISON\s+FAMILIALE|"
    r"CENTRE\s+HOSPITALIER|HOPITAL|ASSOCIATION|AMICALE|FEDERATION|COMICE)", re.I)

PHONE_SOURCE_FR = {
    "agencebio": "déclaré par l'exploitant (Agence Bio)",
    "osm": "OpenStreetMap",
    "bienvenue_ferme": "fiche Bienvenue à la ferme",
    "pagesjaunes": "Pages Jaunes",
    "site/mentions_legales": "mentions légales du site de l'exploitation",
    "site/confirme": "site web de l'exploitation",
    "corrobore": "2 sources indépendantes concordantes",
    "google_panel": "fiche Google de l'exploitation",
    "provider": "fichier fournisseur (non vérifié)",
}


# ---------------------------------------------------------------- helpers
def name_tokens(*names: str) -> set:
    """Distinctive tokens of a farm's names (enseigne, raison sociale, gérant…)."""
    toks = set()
    for n in names:
        toks |= {t for t in norm(n).split() if len(t) >= 3 and t not in M6_STOPWORDS}
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
    return any(a in h for a in AGRI_AGGREGATORS)


def elevage_score(text: str) -> int:
    t = (text or "").lower()
    return sum(1 for w in ELEVAGE_WORDS if w in t)


def classify_type(naf: str, *names: str) -> str:
    """bovins lait | bovins viande | équins | ovins-caprins | porcins | volailles |
    autres animaux (or apiculture / escargots / gibier / lapins from the name) |
    polyculture-élevage | hors scope."""
    typ = ELEVAGE_TYPE.get((naf or "").strip(), "hors scope")
    if typ == "autres animaux":
        blob = " ".join(n for n in names if n)
        for sub, rx in SUBTYPE_49_RULES:
            if rx.search(blob):
                return sub
    return typ


# Provider rows often carry no NAF but an Activité label ("ELEVAGE DE VACHES
# LAITIERES", "eleveur"): map the label to the same type vocabulary. Order
# matters: goats are "laitières" too, so ovins-caprins is tested before lait.
LABEL_TYPE_RULES = (
    ("ovins-caprins",       re.compile(r"OVIN|CAPRIN|MOUTON|BREBIS|CH[EÈ]VRE", re.I)),
    ("porcins",             re.compile(r"PORC", re.I)),
    ("volailles",           re.compile(r"VOLAILLE|POULE|POULET|CANARD|OIE\b|AVICOL", re.I)),
    ("équins",              re.compile(r"CHEVA|[EÉ]QUID|HARAS|[EÉ]QUIN", re.I)),
    ("polyculture-élevage", re.compile(r"POLYCULTURE|CULTURE ET [EÉ]LEVAGE|CULTURES ET [EÉ]LEVAGE", re.I)),
    ("bovins lait",         re.compile(r"LAITI|\bLAIT\b", re.I)),
    ("bovins viande",       re.compile(r"BOVIN|VACHE|BUFFLE|VEAU|B[OŒ]EUF", re.I)),
)


def type_from_label(label: str) -> str:
    for typ, rx in LABEL_TYPE_RULES:
        if rx.search(label or ""):
            return typ
    return "élevage (non précisé)" if label else "non précisé"


# Kennel / cattery affixes are English by convention ("ELEVAGE OF SHAGGY
# LOVE", "OF THE GOLDEN DREAM", found by the V1 hand-check, dept 63). Applied
# to NAF 01.49Z only — a horse stud or a farm keeps its French name.
PET_49_RE = re.compile(
    r"\b(OF|THE|LOVE|DREAM|DREAMS|STAR|STARS|ANGEL|ANGELS|PARADISE|KINGDOM|LAND|HOUSE|HILL|HILLS|"
    r"GARDEN|LITTLE|SWEET|HAPPY|MOON|SUN|SPIRIT|LEGEND|DIAMOND|PRINCE|PRINCESS|KING|QUEEN|ROYAL|"
    r"MAGIC|WOLF|WOLVES|HEART|SOUL|BEAUTY|FOREVER|BABY|BABIES|FAMILY|FRIENDS|LADY|LORD|BLUE|"
    r"GOLD|GOLDEN|SILVER|BLACK|WHITE|RED|SHAGGY|FLUFFY|PUPPY|PUPPIES|KITTEN|KITTENS|CATTERY|KENNEL|"
    r"PET|PETS|ROYAUME|DER|DIE|VON|PRINZEN|TWEED|DIAMONDS)\b|'S\b",
    re.I)

# NAF 01.49Z mixes beekeepers, snail farms and kennels: sub-type from the name
# so Maha can filter (the V1 hand-check showed RUCHER / MIEL rows next to
# "ANGEL OF THE MOON").
SUBTYPE_49_RULES = (
    ("apiculture", re.compile(r"RUCHER|MIEL|ABEILLE|APICULT|\bAPI\b|APIBOUGNAT", re.I)),
    ("escargots",  re.compile(r"ESCARGOT|HELICI", re.I)),
    ("gibier",     re.compile(r"GIBIER|FAISAN|PERDRI", re.I)),
    ("lapins",     re.compile(r"LAPIN|CUNICOLE", re.I)),
)


def is_pet_trade(*names: str, naf: str = "") -> bool:
    if any(PET_RE.search(n or "") for n in names):
        return True
    if (naf or "").strip() == "01.49Z":
        return any(PET_49_RE.search(n or "") for n in names)
    return False


def is_public(nature_juridique: str, *names: str) -> bool:
    nj = (nature_juridique or "").strip()
    if nj.startswith("7"):
        return True
    return any(PUBLIC_NAME_RE.search(n or "") for n in names)


def phone_digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    if d.startswith("33") and len(d) == 11:
        d = "0" + d[2:]
    return d if len(d) == 10 else ""


# ---------------------------------------------------------------- population
def row_id(op: dict) -> str:
    return op.get("siret") or f"X{op.get('siren', '')}"


def load_operators(dept: str) -> list[dict]:
    """operateurs_<dept>.csv rows plus adapter keys (prefixed '_')."""
    p = CHECK_DIR / f"operateurs_{dept}.csv"
    if not p.exists():
        sys.exit(f"{p} missing — run m6_s2_transform.py --departements {dept} first.")
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

    check("stopwords drop generics", name_tokens("GAEC DES VACHES ROUGES", "Elevage de la Sioule"),
          {"ROUGES"})
    check("accents folded", name_tokens("Ferme du Père Noël"), {"PERE", "NOEL"})
    check("type bovins lait", classify_type("01.41Z"), "bovins lait")
    check("type polyculture", classify_type("01.50Z"), "polyculture-élevage")
    check("type équins", classify_type("01.43Z"), "équins")
    check("type autres", classify_type("01.49Z"), "autres animaux")
    check("type apiculture from name", classify_type("01.49Z", "LES RUCHERS DE L'AIGUILLON"), "apiculture")
    check("type escargots from name", classify_type("01.49Z", "LES ESCARGOTS DE BABETH"), "escargots")
    check("pet: possessive kennel", is_pet_trade("BETTY'S DIAMONDS", naf="01.49Z"), True)
    check("pet: german kennel", is_pet_trade("DIE PRINZEN SCHWARZ DER KUPP", naf="01.49Z"), True)
    check("type hors scope", classify_type("01.11Z"), "hors scope")
    check("pet: chenil", is_pet_trade("CHENIL DES VOLCANS"), True)
    check("pet: breed", is_pet_trade("ELEVAGE DU BERGER AUSTRALIEN DU SANCY"), True)
    check("pet: chat", is_pet_trade("LES CHATS DE LA COMBE"), True)
    check("pet: farmer named Berger", is_pet_trade("GAEC BERGER FRERES"), False)
    check("pet: farm stays", is_pet_trade("GAEC DE LA VALLEE VERTE"), False)
    check("pet: english kennel affix on 01.49Z", is_pet_trade("ELEVAGE OF SHAGGY LOVE", naf="01.49Z"), True)
    check("pet: english word on a cattle farm is not a kennel", is_pet_trade("GAEC OF SANCY", naf="01.42Z"), False)
    check("pet: bees stay", is_pet_trade("LES RUCHERS DU SANCY", naf="01.49Z"), False)
    check("public by nature", is_public("7210", "COMMUNE DE MURAT"), True)
    check("public lycée", is_public("9220", "LYCEE AGRICOLE DE MARMILHAT"), True)
    check("private gaec", is_public("6533", "GAEC DES VOLCANS"), False)
    check("aggregator pj", is_aggregator("https://www.pagesjaunes.fr/pros/1"), True)
    check("own site passes", is_aggregator("https://www.ferme-des-volcans.fr/"), False)
    check("elevage score", elevage_score("Élevage de vaches Salers, vente directe de viande à la ferme") >= 4, True)
    check("geo gate by cp", geo_pass("63790", "Murol", "GAEC X 63790 Murol"), "cp")
    check("phone digits intl", phone_digits("+33 4 73 12 34 56"), "0473123456")
    check("phone digits bad", phone_digits("04 73 12"), "")
    print(f"selftest: {fails} failure(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    print(__doc__)
