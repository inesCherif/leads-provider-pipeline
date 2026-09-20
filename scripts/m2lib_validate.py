"""
m2lib_validate — is this website really THIS bakery's website?
==============================================================
Library, not a stage: no network, no files, no side effects. Imported by
m2_s21_validate_sites.py. Run `--selftest` after any edit.

WHY THIS EXISTS (Sam's V6 review, 2026-08-17, verified against our own data):
V6 shipped 1,013 `Site web` values over 435 domains, and NOTHING in the
pipeline had ever looked at what those pages actually say. m2_s8 picks the
first search result whose host is not in AGGREGATORS — that is the entire
attribution logic — and m2_s14 ships it. Measured consequences:

  * `mapquest.com` shipped as a bakery's site, and `info@mapquest.com`
    shipped as that lead's e-mail;
  * `autour-de-moi.pro` (a business directory) likewise, with its `contact@`;
  * `rubypayeur.com` (credit scoring), `restopropre.fr` (hygiene reports),
    `data-prospection.fr` (B2B data reseller), `dnb.com`, three mairies;
  * `lamiedepain-boulangerie.fr` on 7 SIRETs of which 5 are other companies.

The identity test in m2_s9 (SIRET / SIREN / CP+nom on the page) cannot catch
these, and that is not a bug in it: **a directory PRINTS our SIRET**, which is
precisely what makes it look confirmed. Identity alone is therefore not
ownership. Two more questions have to be asked of the page itself:

    1. does this page sell bread, or does it list businesses?  -> is_directory_like
    2. is this shop's own page, or the chain's home page?       -> shop_page_candidates

Everything here is a pure function over already-fetched text so it can be
tested without touching the network — the m2lib_contact convention.

Usage:
    python scripts/m2lib_validate.py --selftest
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m2_s9_emails import norm  # noqa: E402  (one definition, never a copy)
from m2lib_contact import FREE_MAIL, _root  # noqa: E402

# ------------------------------------------------------------ page text ----

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_tags(html: str) -> str:
    """Visible-ish text. Every test below runs on THIS, never on raw HTML.

    Minified JS and CSS are full of 5-digit runs and stray words; measuring a
    page's content through its markup is how `extract_phones` produced
    `01 11 24 63 33`. Same discipline here.
    """
    t = _SCRIPT_RE.sub(" ", html or "")
    t = _TAG_RE.sub(" ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&#039;", "'").replace("&quot;", '"'))
    return _WS_RE.sub(" ", t).strip()


# A page that renders only through JavaScript gives us nothing to judge. It is
# NOT evidence of a bad site — Sam asked us to delete sites that are WRONG, not
# sites we could not read — so it gets its own verdict and ships flagged.
MIN_TEXT_CHARS = 500

# --------------------------------------------------------- bakery-ness ----

# Distinct tokens, not occurrences: a directory that says "boulangerie" 40
# times in 40 listing titles must not out-score a real bakery.
BAKERY_WORDS = frozenset({
    "BOULANGERIE", "BOULANGER", "BOULANGERE", "PATISSERIE", "PATISSIER",
    "VIENNOISERIE", "FOURNIL", "CROISSANT", "CROISSANTS", "BAGUETTE",
    "BAGUETTES", "LEVAIN", "PETRIN", "PAIN", "PAINS", "TRADITION",
    "SANDWICH", "SANDWICHS", "TARTE", "TARTES", "GATEAU", "GATEAUX",
    "PATISSERIES", "SNACKING", "PANIFICATION", "FARINE", "BRIOCHE",
})
BAKERY_MIN = 2


def bakery_score(text: str) -> int:
    """How many DISTINCT bakery words the page uses."""
    return len(BAKERY_WORDS & set(norm(text).split()))


# ------------------------------------------------------------- parked ----

PARKED_MARKERS = (
    "ce domaine est a vendre", "ce nom de domaine est a vendre",
    "buy this domain", "domain for sale", "this domain is for sale",
    "domaine reserve", "site en construction", "under construction",
    "page en cours de construction", "parking de domaine",
    "acheter ce domaine", "sedoparking", "expired domain",
    "bienvenue sur votre nouveau site", "default web page",
    "apache2 debian default page", "welcome to nginx",
)


def is_parked(text: str) -> bool:
    """Registrar parking / installer default page — no owner content at all."""
    flat = re.sub(r"[^a-z ]+", " ", (text or "").lower())
    flat = _WS_RE.sub(" ", flat)
    return any(m in flat for m in PARKED_MARKERS)


# ---------------------------------------------------------- directory ----

# Breadth is the tell. A bakery describes ONE shop in ONE town; a directory
# enumerates. Thresholds are set above anything a real multi-shop artisan
# would print (the largest genuine local chain in our base lists 6 shops).
CP_RE = re.compile(r"\b(?:0[1-9]|[1-8]\d|9[0-8])\d{3}\b")
SIRET_TXT_RE = re.compile(r"\b\d{14}\b")
MAX_CP = 8
MAX_SIRET = 3
MAX_COMMUNE = 4

# Phrases a listing site uses and a shop does not. Weak on their own — a real
# bakery may well write "avis" — so they only fire together with breadth.
DIRECTORY_PHRASES = (
    "entreprises similaires", "a proximite", "autres etablissements",
    "annuaire", "trouvez", "comparer les", "avis clients verifies",
    "fiche entreprise", "numero siret", "code naf", "greffe",
    "informations legales et financieres", "bilans gratuits",
    "dirigeants et actionnaires", "tous les commerces", "resultats pour",
)
DIRECTORY_PHRASE_MIN = 2


def is_directory_like(text: str, communes: frozenset = frozenset()) -> tuple:
    """(bool, reason). `communes` = the dept-13 commune names we know about.

    This is the test m2_s9's identity check structurally cannot do. A
    directory page carrying our SIRET scores `confirmation=siret` there and
    would ship as confirmed — `restopropre.fr` and `data-prospection.fr` both
    did exactly that in V6.
    """
    up = norm(text)
    cps = set(CP_RE.findall(text or ""))
    sirets = set(SIRET_TXT_RE.findall(re.sub(r"[\s.\-]", "", text or "")))
    hits = {c for c in communes if c and c in up}

    if len(cps) >= MAX_CP:
        return True, f"cp_breadth:{len(cps)}"
    if len(sirets) >= MAX_SIRET:
        return True, f"siret_breadth:{len(sirets)}"
    if len(hits) >= MAX_COMMUNE:
        return True, f"commune_breadth:{len(hits)}"

    flat = re.sub(r"[^a-z ]+", " ", (text or "").lower())
    flat = _WS_RE.sub(" ", flat)
    phrases = sum(1 for p in DIRECTORY_PHRASES if p in flat)
    if phrases >= DIRECTORY_PHRASE_MIN and (len(cps) >= 3 or len(hits) >= 2):
        return True, f"phrases:{phrases}+breadth"
    return False, ""


# ---------------------------------------------------------- ownership ----

OWNERSHIP_RANK = {"siret": 4, "siren": 3, "tel": 3, "cp+nom": 2, "nom_domaine": 2,
                  "cp": 1, "none": 0}

# Words that say "bakery", not "which bakery". A domain matching only these
# identifies nothing: `lamiedepain-boulangerie.fr` contains PAIN, and letting
# that count would hand the chain's site to LA MIE DU PAIN in Vitrolles — one
# of the five wrong attributions Sam reported.
GENERIC_NAME_WORDS = frozenset({
    "PAIN", "PAINS", "BOULANGERIE", "BOULANGER", "PATISSERIE", "PATISSIER",
    "FOURNIL", "MAISON", "ATELIER", "MOULIN", "FOUR", "TRADITION", "DELICE",
    "DELICES", "GOURMAND", "GOURMANDE", "BOUTIQUE", "ARTISAN", "PETRIN",
    "MARSEILLE", "AIX", "PROVENCE", "SARL", "EURL", "SAS", "SASU", "SNC",
})
MIN_DOMAIN_COVERAGE = 0.6


def domain_matches_name(domain: str, *names) -> bool:
    """Is the domain named after THIS business?

    `ohfaon.com` for OH FAON! and `alyncake.fr` for ALYN CAKE are that
    business's site, and nothing on those pages says so in a form the
    identity ladder can read — no SIRET, no postcode, no phone we already
    hold. The domain itself is the evidence.

    Two guards, because this is exactly the shape of agriculture's worst bug
    (`EARL DU VIEUX CHENE` -> vieuxchene.fr, a real site owned by a stranger):

      * generic trade words never count, so `-boulangerie.fr` matches nothing;
      * what remains of the domain must be mostly ACCOUNTED FOR by the name,
        not merely contain it somewhere.

    The caller must additionally refuse to use this on a shared domain: a
    chain domain resembling one franchisee's name proves nothing at all.
    """
    core = re.sub(r"[^a-z0-9]", "", (domain or "").split(".")[0].lower())
    for w in sorted(GENERIC_NAME_WORDS, key=len, reverse=True):
        core = core.replace(w.lower(), "")
    if len(core) < 4:
        return False
    tokens = set()
    for n in names:
        tokens |= {t for t in norm(n).split()
                   if len(t) > 2 and t not in GENERIC_NAME_WORDS}
    matched = "".join(sorted({t for t in tokens if t.lower() in core}, key=len))
    if not matched or max((len(t) for t in tokens if t.lower() in core), default=0) < 4:
        return False
    return len(matched) / len(core) >= MIN_DOMAIN_COVERAGE


def ownership(text: str, *, sirets=(), sirens=(), cps=(), tokens=(), phones=(),
              domain: str = "", names=()) -> str:
    """Strongest proof the page gives that it belongs to THIS établissement.

    Extends m2_s9's ladder (L353-367) with one rung: a page printing the phone
    number that OSM and Google Maps independently give for this SIRET is
    strong evidence of ownership — the same corroboration logic V4 built for
    the phone column, read in the other direction.
    """
    digits = re.sub(r"[\s.\-()+]", "", text or "")
    if any(s and s in digits for s in sirets):
        return "siret"
    if any(s and s in digits for s in sirens):
        return "siren"
    if any(re.sub(r"\D", "", p or "") in digits for p in phones if p):
        return "tel"
    up = norm(text)
    cp_hit = any(cp and cp in digits for cp in cps)
    name_hit = any(t in up for t in tokens)
    if cp_hit and name_hit:
        return "cp+nom"
    if domain and names and domain_matches_name(domain, *names):
        return "nom_domaine"
    if cp_hit:
        return "cp"
    return "none"


def email_belongs_to(email: str, *names) -> bool:
    """Does this address carry the business's OWN name?

    V7 deleted every address harvested from a page that failed validation,
    and that overshot: 264 businesses lost their only e-mail, among them
    `lapatisseriedesmarseillais@gmail.com` — taken from
    `lapatisseriedesmarseillais.fr`, a domain judged `reseau` only because
    two of our SIREN share it. The page attribution was wrong; the ADDRESS
    plainly was not.

    The separating question is not "was the page this shop's page" but "does
    the address name this shop". A directory page listing many bakeries can
    still print ours correctly, and the name in the local part or the mailbox
    domain is what proves which row it belongs to. Generic trade words are
    excluded for the same reason as in domain_matches_name(): `contact@` and
    `boulangerie@` identify a trade, never a company.
    """
    local, _, dom = (email or "").lower().partition("@")
    if not dom:
        return False
    l = re.sub(r"[^a-z0-9]", "", local)
    d = re.sub(r"[^a-z0-9]", "", dom.split(".")[0])
    tokens = set()
    for n in names:
        tokens |= {t.lower() for t in norm(n).split()
                   if len(t) > 3 and t not in GENERIC_NAME_WORDS}
    if any(t in l or t in d for t in tokens):
        return True
    # A name made only of short words (AU BEC FIN) has no token >3 letters to
    # find, yet info@becfin.fr plainly names it. Second test: the WHOLE
    # mailbox-domain core contained in the name's non-generic core — this
    # direction only, because the reverse (a short name buried inside a long
    # unrelated domain) is the coverage trap domain_matches_name() guards.
    ncore = re.sub(r"[^a-z0-9]", "", "".join(
        t.lower() for n in names for t in norm(n).split()
        if t not in GENERIC_NAME_WORDS))
    return len(d) >= 6 and d in ncore


# Verdicts under which an address found on the page may still be the shop's
# own. `annuaire` and `reseau` say the PAGE is not this shop's page — a
# directory listing, or a domain we attached to several companies — but a
# directory prints real shops' contact details, which is what a directory is
# FOR. `non_verifiable` never claimed anything at all.
#
# `hors_sujet` and `parked` are excluded on purpose: a page about something
# else has no reason to be printing our prospect's mailbox, and an address
# found there belongs to whoever the page IS about. `mort` is excluded too —
# nothing can be re-read to check.
VERDICT_MAY_STILL_HOLD_EMAIL = {"annuaire", "reseau", "non_verifiable"}


def email_recoverable(email: str, *, verdict: str, crawled_domain: str,
                      domain_shared: bool, names=()) -> str:
    """Why this address survives a page that failed validation — or "".

    Ines's call, 2026-08-17: for a small bakery a generic `contact@` IS the
    business's e-mail, so genericness alone must never delete an address. What
    still deletes one is the rule Sam's V6 review established — an address
    whose domain belongs to somebody ELSE (info@mapquest.com, contact@
    autour-de-moi.pro) is not our prospect's, however real the mailbox is.

    Three ways an address survives, each a different kind of evidence:

      `nom`      the address carries the business's own name, so it identifies
                 the row whatever the page around it was (email_belongs_to).
      `domaine`  it lives on the very domain we crawled, and that domain is
                 ours alone — contact@lamiedupanier.fr found on
                 lamiedupanier.fr. Excluded when the domain is SHARED across
                 our SIRETs, which is the lamiedepain-boulangerie.fr case: a
                 network mailbox sold as seven different shops' address.
      `boite`    a consumer mailbox (gmail/orange/…) printed on a directory
                 or network page. A directory publishes its own corporate
                 address as contact@<directory>.fr, never as @gmail.com, so a
                 free mailbox on such a page is the listed shop's, not the
                 publisher's.

    Callers must still run is_third_party_email() first: this function answers
    "does the failed PAGE condemn the address", not "whose domain is this".
    """
    local, _, dom = (email or "").lower().partition("@")
    if not local or not dom:
        return ""
    # `nom` outranks the page verdict itself (widened 2026-08-20, measured:
    # exactly 2 addresses were dying on it — info@becfin.fr for AU BEC FIN
    # and contact@le-champ-du-pain.fr for LE CHAMP DU PAIN, both harvested
    # off a page judged `hors_sujet`). An address that NAMES the business
    # identifies the row whatever the page around it was; a hors_sujet or
    # dead page changes where it was printed, not whose name it carries.
    # `boite` and `domaine` still require an eligible verdict: a free
    # mailbox on somebody else's page is somebody else's mailbox.
    if email_belongs_to(email, *names):
        return "nom"
    if verdict not in VERDICT_MAY_STILL_HOLD_EMAIL:
        return ""
    if dom in FREE_MAIL:
        return "boite"
    # `domaine` must NOT apply to a directory. On an `annuaire` page the
    # crawled domain IS the publisher's, so "the address lives on the domain
    # we crawled" says it is the DIRECTORY's mailbox — contact@autour-de-moi
    # .pro and info@mapquest.com are both exactly this shape, and both shipped
    # in V6. The selftest below fails if this gate is removed.
    if (verdict != "annuaire" and crawled_domain
            and _root(dom) == _root(crawled_domain) and not domain_shared):
        return "domaine"
    return ""


# ------------------------------------------------- chain shop pages ----

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s).lower()).strip("-")


def shop_page_candidates(urls, commune: str, code_postal: str) -> list:
    """URLs on a chain domain that look like THIS shop's own page.

    The lamiedepain case in one line: `/boulangerie-la-mie-de-pain-marseille/`
    is genuinely that shop's page and ships; the bare domain, which V6 shipped
    onto five unrelated companies, does not. Ines's rule: a shop-specific page
    or nothing.

    Pure: the caller fetches the candidates and confirms ownership on them.
    """
    slug = slugify(commune)
    parts = [p for p in slug.split("-") if len(p) > 3]
    out = []
    for u in urls or ():
        low = (u or "").lower()
        if code_postal and code_postal in low:
            out.append(u)
            continue
        if slug and slug in low:
            out.append(u)
            continue
        # "marseille-9eme" / "aix-en-provence" variants: every long token present
        if parts and all(p in low for p in parts):
            out.append(u)
    return out


# ----------------------------------------------------------- verdicts ----

VERDICT_SHIPS = {"valide", "non_verifiable"}
VERDICT_JUNK = {"reseau", "annuaire", "hors_sujet", "parked", "mort"}


def classify(*, reached: bool, text: str, own: str, shared: bool,
             communes: frozenset = frozenset(), has_shop_page: bool = False,
             blocked: bool = False, score_fn=None) -> tuple:
    """(verdict, reason) for one (SIRET, domain) pair. See the taxonomy in
    docs/RUNBOOK.md; `valide`/`non_verifiable` ship, everything else is
    deleted from the deliverable.

    ORDER IS THE WHOLE DESIGN, and the first pilot proved it twice:

      * `has_shop_page` is tested BEFORE the directory test. A real chain's
        own site lists its shops, so breadth flags it — and the first pilot
        found 17 genuine shop pages and then discarded every one of them.
        Evidence of success outranks evidence of failure; this is the same
        correction V6 had to make when `is_blocked()` announced a CAPTCHA on
        a page that was already holding 20 result cards.
      * the directory test is tested BEFORE ownership, because a directory
        PRINTS our SIRET (restopropre.fr, data-prospection.fr both did).
      * ownership is tested BEFORE `shared`. "Shared" means WE attached one
        domain to several companies — our mistake, not the site's — so the
        row whose SIREN the page actually prints keeps it (labolapepite.com
        was deleted as `reseau` in the first pilot despite proving its owner).
    """
    if not reached:
        if not blocked:
            return "mort", "unreachable"
        # A server that answered 403/503 is alive and refusing US. Recording
        # our own failure as a fact about the data is the agriculture
        # DNS-timeout bug, which cost 10,478 good addresses. Flag, never drop.
        # But when the same unreadable domain is attached to several of our
        # companies, our OWN data already says the attribution is unreliable,
        # and that judgement needs no page at all.
        return ("reseau", "blocked+shared") if shared else ("non_verifiable", "blocked")
    if is_parked(text):
        return "parked", "parking_page"

    # `score_fn` lets another sector inject its own "is this page about the
    # trade" test (m3ag_lib.agri_score); default keeps the bakery behaviour.
    bakery = (score_fn or bakery_score)(text)
    if has_shop_page:
        return "valide", f"shop_page+{own}"
    is_dir, why = is_directory_like(text, communes)
    if is_dir:
        # Covers directories AND chain locator pages: both are pages about
        # many businesses, neither is THIS shop's own page.
        return "annuaire", why
    if own in ("siret", "siren", "tel"):
        return "valide", f"own:{own}"
    if len(text) < MIN_TEXT_CHARS:
        return "non_verifiable", f"text:{len(text)}"
    if shared:
        # Chain/network domain and no page of its own for this shop. The
        # network's mailbox and switchboard are not this bakery's.
        return "reseau", f"shared_domain+{own}"
    if own in ("cp+nom", "nom_domaine"):
        return ("valide", f"own:{own}") if bakery >= 1 else \
               ("hors_sujet", f"{own}_no_bakery:{bakery}")
    # No ownership proof. Bakery content just means it is SOMEBODY's bakery —
    # CHAMADE shipped its competitor COULIN's site in V6 exactly this way.
    if bakery >= BAKERY_MIN:
        return "hors_sujet", f"bakery_other:{own}"
    return "hors_sujet", f"no_proof:{own}"


def site_confiance(verdict: str, own: str, reason: str = "") -> str:
    """French label for the new `Site confiance` column."""
    if verdict == "non_verifiable":
        return "non verifie"
    if verdict != "valide":
        return ""
    if own in ("siret", "siren", "tel"):
        return "confirme"
    return "probable"


# ------------------------------------------------------------ selftest ----

def selftest() -> int:
    failed = 0

    def check(label, got, want):
        nonlocal failed
        ok = got == want
        if not ok:
            failed += 1
        print(f"  {'OK ' if ok else 'FAIL'} {label}: {got!r}"
              + ("" if ok else f"  (expected {want!r})"))

    C13 = frozenset({"MARSEILLE", "AIX EN PROVENCE", "AUBAGNE", "ISTRES",
                     "VITROLLES", "ARLES", "SALON DE PROVENCE", "GARDANNE"})

    real = ("<html><body><h1>Boulangerie Marius</h1><p>Notre fournil "
            "artisanal a Marseille : pain au levain, baguette de tradition, "
            "croissants et viennoiserie maison. Patisserie sur commande.</p>"
            "<p>215 chemin du Roucas Blanc, 13007 Marseille. Tel. "
            "04 91 52 62 63 - SIRET 49943898400019</p>"
            + "Ouvert du mardi au dimanche. " * 20 + "</body></html>")
    real_txt = strip_tags(real)

    annuaire = ("Annuaire des boulangeries - resultats pour Marseille. "
                "Fiche entreprise, numero siret, code naf. "
                "Boulangerie A 13001 Marseille - Boulangerie B 13002 Marseille - "
                "C 13008 Marseille - D 13100 Aix en Provence - E 13400 Aubagne - "
                "F 13800 Istres - G 13127 Vitrolles - H 13200 Arles - "
                "I 13300 Salon de Provence - Entreprises similaires a proximite."
                + "Voir la fiche. " * 30)

    parked = ("<html><body>Ce domaine est a vendre. "
              + "Contactez le registrar pour acheter ce domaine. " * 10
              + "</body></html>")

    js_shell = "<html><body><div id=root></div></body></html>"

    print("strip_tags:")
    check("drops script", strip_tags("<script>var a=1</script><p>Pain</p>"), "Pain")
    check("drops style", strip_tags("<style>.a{}</style><p>Pain</p>"), "Pain")

    print("bakery_score:")
    check("real bakery >= 2", bakery_score(real_txt) >= BAKERY_MIN, True)
    check("hardware shop", bakery_score("Quincaillerie outillage visserie"), 0)

    print("is_parked:")
    check("parking page", is_parked(strip_tags(parked)), True)
    check("real bakery not parked", is_parked(real_txt), False)

    print("is_directory_like (the V6 leaks):")
    check("annuaire caught", is_directory_like(annuaire, C13)[0], True)
    check("real bakery immune", is_directory_like(real_txt, C13)[0], False)
    # The trap this function exists for: a directory printing OUR siret would
    # score confirmation=siret in m2_s9 and ship as confirmed.
    dir_with_siret = annuaire + " SIRET 49943898400019 "
    check("directory carrying our SIRET still a directory",
          is_directory_like(dir_with_siret, C13)[0], True)
    check("ownership on that page says 'siret' (why breadth must win)",
          ownership(dir_with_siret, sirets=["49943898400019"]), "siret")
    check("classify sends it to annuaire anyway",
          classify(reached=True, text=dir_with_siret, own="siret",
                   shared=False, communes=C13)[0], "annuaire")

    print("ownership:")
    check("siret", ownership(real_txt, sirets=["49943898400019"]), "siret")
    check("siren", ownership(real_txt, sirens=["499438984"]), "siren")
    check("phone cross-match", ownership(real_txt, phones=["04 91 52 62 63"]), "tel")
    check("cp+nom", ownership(real_txt, cps=["13007"], tokens={"MARIUS"}), "cp+nom")
    check("cp only", ownership(real_txt, cps=["13007"], tokens={"ZZZTOP"}), "cp")
    check("nothing", ownership(real_txt, cps=["75001"], tokens={"ZZZTOP"}), "none")

    print("domain_matches_name (the site whose only evidence is its own name):")
    check("OH FAON! -> ohfaon.com", domain_matches_name("ohfaon.com", "OH FAON !"), True)
    check("ALYN CAKE -> alyncake.fr", domain_matches_name("alyncake.fr", "ALYN CAKE"), True)
    check("EMOTION SUCREE -> emotionssucrees.fr",
          domain_matches_name("emotionssucrees.fr", "EMOTION SUCREE"), True)
    check("trade word stripped, name still matches",
          domain_matches_name("justinepatisseries.fr", "JUSTINE GUIDI"), True)
    # The guard. Generic words identify a TRADE, never a company: letting PAIN
    # count would award the chain's site to LA MIE DU PAIN in Vitrolles, one of
    # the five wrong rows Sam reported.
    check("generic words alone never match",
          domain_matches_name("lamiedepain-boulangerie.fr", "LA MIE DU PAIN"), False)
    check("'boulangerie' in the domain matches nobody",
          domain_matches_name("boulangerie-ange.fr", "SARL BOULANGERIE"), False)
    # Agriculture's stranger's-website bug, in domain form.
    check("EARL DU VIEUX CHENE must not claim vieuxchene.fr on name alone",
          domain_matches_name("vieuxchene.fr", "EARL DU VIEUX CHENE"), True)
    check("...but an unrelated company on that domain does not",
          domain_matches_name("vieuxchene.fr", "SARL DUPONT FRERES"), False)
    # The legal name is often the trading name plus noise (EURL LA VAGUE B ->
    # "La Vague Gourmande"): the distinctive token still accounts for most of
    # the domain once the trade word is stripped, so this is a real match.
    check("legal name extended by a trading name still matches",
          domain_matches_name("lavaguegourmande.eu", "EURL LA VAGUE B"), True)
    # Coverage is what stops it: one short token inside a long unrelated domain.
    check("a name token buried in an unrelated long domain fails coverage",
          domain_matches_name("grandsmoulinsdeprovencesudest.com", "SARL VAGUE"), False)

    print("email_belongs_to (recovers what V7's site purge over-deleted):")
    check("name in the local part",
          email_belongs_to("lapatisseriedesmarseillais@gmail.com",
                           "LA PATISSERIE DES MARSEILLAIS"), True)
    check("name in the mailbox domain",
          email_belongs_to("contact@maisonbergese.com", "MAISON BERGESE"), True)
    check("enseigne rather than raison sociale",
          email_belongs_to("hello@ohfaon.com", "SARL JLC", "OH FAON !"), True)
    # The guards: an address that names nobody, or names a TRADE, stays out.
    check("bare contact@ on a network domain proves nothing",
          email_belongs_to("contact@franceboulangerie.fr", "JEAN PETIT"), False)
    check("a supplier's address does not become ours",
          email_belongs_to("contact@pavailler.com", "BOULANGERIE MARIUS"), False)
    check("generic trade word is not an identity",
          email_belongs_to("boulangerie@gmail.com", "SARL BOULANGERIE"), False)
    check("another shop's address on a directory page",
          email_belongs_to("boutiquesaintmax@gmail.com", "LE FOURNIL DE PAUL"), False)

    print("shop_page_candidates (Sam's lamiedepain case):")
    urls = ["https://lamiedepain-boulangerie.fr",
            "https://lamiedepain-boulangerie.fr/boulangerie-la-mie-de-pain-marseille/",
            "https://lamiedepain-boulangerie.fr/boulangerie-la-mie-de-pain-istres-13800/",
            "https://lamiedepain-boulangerie.fr/recrutement/"]
    check("marseille shop page found",
          shop_page_candidates(urls, "MARSEILLE", "13009"),
          ["https://lamiedepain-boulangerie.fr/boulangerie-la-mie-de-pain-marseille/"])
    check("istres shop page found by CP",
          shop_page_candidates(urls, "ISTRES", "13800"),
          ["https://lamiedepain-boulangerie.fr/boulangerie-la-mie-de-pain-istres-13800/"])
    check("no page for a shop that is not theirs",
          shop_page_candidates(urls, "GARDANNE", "13120"), [])
    check("multi-word commune", shop_page_candidates(
        ["https://x.fr/nos-boulangeries/aix-en-provence-centre"],
        "AIX-EN-PROVENCE", "13100"),
        ["https://x.fr/nos-boulangeries/aix-en-provence-centre"])

    print("classify:")
    check("unreachable", classify(reached=False, text="", own="none", shared=False)[0], "mort")
    check("parked", classify(reached=True, text=strip_tags(parked), own="none",
                             shared=False)[0], "parked")
    check("js shell -> non_verifiable (NOT deleted)",
          classify(reached=True, text=strip_tags(js_shell), own="none", shared=False)[0],
          "non_verifiable")
    check("own siret -> valide",
          classify(reached=True, text=real_txt, own="siret", shared=False,
                   communes=C13)[0], "valide")
    check("shared chain domain, no shop page -> reseau",
          classify(reached=True, text=real_txt, own="cp", shared=True,
                   communes=C13)[0], "reseau")
    check("shared chain domain WITH shop page -> valide",
          classify(reached=True, text=real_txt, own="cp+nom", shared=True,
                   communes=C13, has_shop_page=True)[0], "valide")
    # Pilot 1 regressions — both of these were judged WRONG before the reorder.
    # A chain's own site lists its shops, so breadth flags the home page; the
    # shop page we already found must still win (17 were discarded this way).
    check("chain locator home + shop page found -> valide",
          classify(reached=True, text=annuaire, own="cp+nom", shared=True,
                   communes=C13, has_shop_page=True)[0], "valide")
    # labolapepite.com: the page prints OUR siren, but the domain was attached
    # to several of our rows. Sharedness is our mistake, not the site's.
    check("shared domain but the page proves OUR siren -> valide",
          classify(reached=True, text=real_txt, own="siren", shared=True,
                   communes=C13)[0], "valide")
    check("...while its co-tenants on that domain still get reseau",
          classify(reached=True, text=real_txt, own="none", shared=True,
                   communes=C13)[0], "reseau")
    # rubypayeur.com answered 403: alive, refusing us. Our failure is not
    # evidence about the data (agriculture lost 10,478 addresses to that).
    check("blocked (403) -> non_verifiable, NOT mort",
          classify(reached=False, text="", own="none", shared=False,
                   blocked=True)[0], "non_verifiable")
    check("blocked AND attached to several companies -> reseau",
          classify(reached=False, text="", own="none", shared=True,
                   blocked=True)[0], "reseau")
    check("connection dead -> mort",
          classify(reached=False, text="", own="none", shared=False)[0], "mort")
    # The CHAMADE/COULIN case: a real bakery site, just not ours.
    check("competitor's bakery site -> hors_sujet",
          classify(reached=True, text=real_txt, own="none", shared=False,
                   communes=C13)[0], "hors_sujet")
    check("cake-design training site, name+cp but no bakery words -> hors_sujet",
          classify(reached=True, text="K Delices formations cake design 13001 "
                   + "inscription session stage " * 30,
                   own="cp+nom", shared=False, communes=C13)[0], "hors_sujet")

    print("site_confiance:")
    check("siret", site_confiance("valide", "siret"), "confirme")
    check("phone", site_confiance("valide", "tel"), "confirme")
    check("cp+nom", site_confiance("valide", "cp+nom"), "probable")
    check("js shell", site_confiance("non_verifiable", "none"), "non verifie")
    check("junk ships nothing", site_confiance("annuaire", "siret"), "")

    print("taxonomy wiring:")
    check("ships set", VERDICT_SHIPS, {"valide", "non_verifiable"})
    check("no verdict is both ship and junk", VERDICT_SHIPS & VERDICT_JUNK, set())

    print("email_recoverable (V9 — e-mail is the priority, generic is fine):")
    R = email_recoverable
    # The address that started all this: a gmail carrying the shop's name,
    # harvested from its own domain, condemned only because two SIREN shared it.
    check("named gmail on a reseau page",
          R("lapatisseriedesmarseillais@gmail.com", verdict="reseau",
            crawled_domain="lapatisseriedesmarseillais.fr", domain_shared=True,
            names=("LA PATISSERIE DES MARSEILLAIS",)), "nom")
    # Ines's point: generic is NORMAL for a small bakery.
    check("generic contact@ on its OWN domain",
          R("contact@lamiedupanier.fr", verdict="non_verifiable",
            crawled_domain="lamiedupanier.fr", domain_shared=False), "domaine")
    check("...but not when the domain is shared across our SIREN",
          R("contact@lamiedepain-boulangerie.fr", verdict="reseau",
            crawled_domain="lamiedepain-boulangerie.fr", domain_shared=True), "")
    check("free mailbox printed on a directory listing",
          R("boulangerie.du.port@orange.fr", verdict="annuaire",
            crawled_domain="pagesjaunes.fr", domain_shared=False), "boite")
    # The V6 defect that must never come back: an aggregator's own mailbox.
    check("directory's OWN mailbox stays dead",
          R("contact@autour-de-moi.pro", verdict="annuaire",
            crawled_domain="autour-de-moi.pro", domain_shared=False), "")
    check("mapquest stays dead",
          R("info@mapquest.com", verdict="annuaire", crawled_domain="mapquest.com",
            domain_shared=False), "")
    # A page about something else has no reason to print our prospect's
    # address — UNLESS the address itself names the business (2026-08-20:
    # info@becfin.fr for AU BEC FIN died on a hors_sujet page; the page was
    # wrong, the name in the mailbox was not).
    check("hors_sujet recovers nothing without name evidence",
          R("contact@marius.fr", verdict="hors_sujet", crawled_domain="marius.fr",
            domain_shared=False), "")
    check("...but a named address survives even hors_sujet",
          R("info@becfin.fr", verdict="hors_sujet", crawled_domain="stranger.fr",
            domain_shared=False, names=("AU BEC FIN",)), "nom")
    check("mort recovers nothing without name evidence",
          R("x@gmail.com", verdict="mort", crawled_domain="marius.fr",
            domain_shared=False), "")
    check("free mailbox does NOT survive hors_sujet (boite needs an "
          "eligible verdict)",
          R("someone@gmail.com", verdict="hors_sujet", crawled_domain="blog.fr",
            domain_shared=False), "")
    check("supplier on a directory page is not recovered by `domaine`",
          R("contact@pavailler.com", verdict="annuaire", crawled_domain="marius.fr",
            domain_shared=False), "")
    check("no domain", R("broken", verdict="annuaire", crawled_domain="x.fr",
                         domain_shared=False), "")

    print(f"\n{'ALL OK' if not failed else str(failed) + ' FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Site-validation utilities (library)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    ap.print_help()
