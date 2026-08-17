"""
M2-S8 — Discover bakery websites (search APIs)
================================================
The registry has no website field, and OSM only knew 109 of them. This finds
more, so that m2_s9 has domains to read e-mails off.

V2 history, kept because it is the reason this file looks like it does: the
first version scraped Brave's result page with Playwright — the only engine
that worked at all from this IP (Pages Jaunes/DDG lite/Mojeek 403, Bing
stale, Startpage useless) — and Brave cut it off with a CAPTCHA after ~85
queries. The V3 rewrite queries free-tier APIs through m2lib_search instead:
no browser, no fingerprint, a persistent quota counter that hard-stops at
the free tier. `--backend` picks the engine (tavily default, 1,000/month;
ddgs keyless fallback; serper_web shares the one-time Serper pool that
m2_s13 needs — use it last).

Precision measured against 6 businesses whose real site we already knew from
OSM: searching the RAISON SOCIALE found the right domain only 2/6, because a
bakery's legal name is not its shop name (`LEPRADPCH` really trades as
sylvaindepuichaffray.fr). So this script searches the ENSEIGNE first when one
exists, and — crucially — **never trusts the result**. Confirmation is
m2_s9's job: it fetches the page and demands the SIRET, or the postal code
plus a name token, before the domain counts. An unconfirmed domain ships as
`faible` and is never pattern-expanded.

That division of labour is the whole design: this step may be sloppy, because
the next step is strict.

V3 also mines the result SNIPPETS: a Google/Tavily snippet often carries the
shop's phone and its Facebook/Instagram page. A snippet phone is kept ONLY
when the snippet also shows our commune or postal code (`snippet_geo_ok`) —
otherwise it may belong to a same-named shop elsewhere — and it always ranks
below every listing-sourced phone at export time.

Crash-safety: one row appended and flushed per business, `sites_done.txt`
records finished SIRETs, so a re-run resumes and nothing is held in memory.

Usage:
    python scripts/m2_s8_websites.py --backend ddgs --limit 20     # pilot
    python scripts/m2_s8_websites.py --backend tavily --limit 20   # pilot
    python scripts/m2_s8_websites.py --backend tavily              # full run
"""

import argparse
import csv
import logging
import re
import sys
import unicodedata
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m2lib_search import search, quota_state, QuotaExceeded, SearchAuthError  # noqa: E402
from m2lib_contact import extract_phones, extract_social                      # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
MATCHED_PATH = CHECK_DIR / "matched.csv"
OUT_PATH  = CHECK_DIR / "discovered_sites.csv"
DONE_PATH = CHECK_DIR / "sites_done.txt"
# Separate ledger for --redo-siteless, so a second pass over the same
# businesses stays resumable without erasing the first pass's history.
REDO_DONE_PATH = CHECK_DIR / "sites_done_redo.txt"

# Directories, registry mirrors, delivery apps and social networks. None of
# them is the bakery's own site, and every one of them would pass a naive
# "the page mentions the business" confirmation because that is their content.
AGGREGATORS = (
    "pagesjaunes", "pagespro", "118712", "118000", "justacote", "yelp.",
    "tripadvisor", "petitfute", "mappy.", "google.", "facebook.", "instagram.",
    "linkedin.", "twitter.", "x.com", "tiktok.", "youtube.", "pinterest.",
    "ubereats", "deliveroo", "just-eat", "justeat", "thefork", "lafourchette",
    "societe.com", "pappers", "infogreffe", "verif.com", "manageo", "kompass",
    "lefigaro.fr", "bilansgratuits", "annuaire-entreprises", "sirene",
    "entreprises.", "b-reputation", "dirigeants.", "score3", "corporama",
    "restaurantguru", "love-spots", "villepratique", "boulangeriespatisseries",
    "boulangeries-patisseries", "franceboulangerie", "boulangerieautourdemoi",
    "cylex", "wikipedia", "leboncoin", "indeed", "hellowork", "wanted.jobs",
    "openstreetmap", "foursquare", "misterbandb", "resto.fr", "lesannonces",
    "amazon.", "doctolib", "avis-", "trustpilot", "brave.com", "michelin",
    "lefooding", "gaultmillau", "marmiton", "journaldesfemmes", "mesinfos",
    "madeinmarseille", "sortiraparis", "actu.fr", "laprovence",
    # added after the first pilot: every "hit" it produced was one of these
    "edecideur", "marseille-tourisme", "mapstr", "petitesaffiches", "figaro",
    "tourisme", "office-tourisme", "yellowpages", "nomao", "citiwaki",
    "topboulangerie", "resto-", "lannuaire", "annuaire", "guide-", "avis.",
    # added for the API backends
    "hoodspot", "infobel", "solocal", "fr.kompass", "horaires.", "snapchat.",
    "alentoor", "ville-data", "linternaute", "communes.com", "nosavis",
    # added 2026-08-13 after the tavily run: registry mirrors and legal-notice
    # sites it surfaces readily. Every one of them PRINTS the SIRET, so they
    # would sail through m2_s9's "page mentions the SIRET" confirmation.
    "societeinfo", "bodacc", "fichesociete", "annonces-legales",
    "controlessanitaires", "leguichetdesformalites", "dataprospects",
    "french-business-law", "localbiz.fr", "lavieduvillage", "e-pro.fr",
    "commerces-ouverts", "monemplacement", "thegoodarles", "myboulange.",
    "infonet.", "pple.fr", "datalegal", "telephone.city", "francetravail",
    "up.coop", "en-ligne.me", "buuyers", "petitscommerces", "latoque.fr",
    "eterritoire", "starofservice", "calameo", "le-site-de.", "credipro",
    "legaleo", "publicationannoncelegale", "deezer", "commerce-engage",
    "blog-aixty", "em-lyon", "bt-africa",
    # commune town-hall sites listing local shops (single-SIRET ones slip
    # past the reseau guard, and the mairie's mailbox is not the bakery's)
    "venelles.fr", "peynier.net", "plandecuques.fr",
    # added 2026-08-17 after Sam's V6 review. Every one of these SHIPPED in
    # V6 as a bakery's "Site web", and the first two shipped their own
    # mailbox as the lead's e-mail (info@mapquest.com,
    # contact@autour-de-moi.pro) — a real address at a company that is not
    # our prospect, which is the mappy lesson from V4 repeated.
    "mapquest", "autour-de-moi", "toogoodtogo", "rubypayeur", "restopropre",
    "data-prospection", "dnb.com", "doctrine.", "boulangerie.contact",
    "au-magasin", "framaps",
    "mairie.biz", "mairie-gemenos", "mairie-du-paradou",
    "mairie13-14.marseille", "chateauneuflesmartigues",
    # added 2026-08-17 from the m2_s21 hand-review. These render through
    # JavaScript, so the validator can only say "unreadable" — and its rule is
    # to flag what it cannot read rather than delete it (deleting on our own
    # inability is agriculture's DNS-timeout bug). That rule is right, and the
    # blacklist is the correct place to settle these instead: they are not
    # judgement calls, they are known non-bakeries.
    "reddit.", "vk.com", "vk.ru", "cheriefm", "macaddict", "ichtusmagazine",
    "mariages.net", "jooble", "cataloxy", "wheree.", "horairesdouverture",
    "eat-list.", "entreprise.one", "cessionpme", "data.inpi", "foodbevg",
    "myfoodstory", "fiestaclic", "nojyk",
    # communes and tourist offices reached the same way
    "saint-chamas.com", "laciotat.com", "puyloubier.fr", "ville-rognac",
    "saintvictoret", "otcarrylerouet", "mairie-ensues",
)
BAD_TLD = (".gouv.fr", ".gov", ".edu")

FIELDNAMES = ["siret", "siren", "raison_sociale", "enseigne", "commune",
              "code_postal", "website", "rank", "query", "backend",
              "phone", "phone_domain", "facebook", "instagram",
              "snippet_geo_ok"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s8")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 ]+", " ", s).upper()


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def flush(row: dict) -> None:
    new = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        if new:
            w.writeheader()
        w.writerow(row)
        fh.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover bakery websites via search APIs")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--backend", default="tavily",
                    choices=["tavily", "ddgs", "serper_web"])
    ap.add_argument("--redo-siteless", action="store_true",
                    help="re-search businesses that have NO VALIDATED site, "
                         "ignoring sites_done.txt. V7 deleted 833 wrong sites; "
                         "those businesses were 'done' only in the sense that a "
                         "previous backend answered wrongly.")
    args = ap.parse_args()

    if not OURS_PATH.exists():
        sys.exit(f"{OURS_PATH} not found — run scripts/m2_s2_transform.py first.")
    with OURS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        ours = list(csv.DictReader(fh, delimiter=";"))

    # Skip businesses whose site we already know from a harvest.
    known = set()
    if MATCHED_PATH.exists():
        with MATCHED_PATH.open(encoding="utf-8-sig", newline="") as fh:
            known = {r["siret"] for r in csv.DictReader(fh, delimiter=";") if r["website"]}
    done = load_done()
    if args.redo_siteless:
        # "Already searched" is not "already has a site". A previous backend
        # answered for these, and m2_s21 then proved the answer wrong — that is
        # the population worth spending a fresh quota on, and it is the biggest
        # gap in V7 (1,524 rows with no site at all).
        verdicts = CHECK_DIR / "site_verdicts.csv"
        if verdicts.exists():
            with verdicts.open(encoding="utf-8-sig", newline="") as fh:
                known = {r["siret"] for r in csv.DictReader(fh, delimiter=";")
                         if r["verdict"] in ("valide", "non_verifiable")}
        done = set(REDO_DONE_PATH.read_text(encoding="utf-8").split()) \
            if REDO_DONE_PATH.exists() else set()
        log.info(f"--redo-siteless: {len(known)} businesses hold a VALIDATED "
                 f"site; everyone else is a target again")
    todo = [r for r in ours if r["siret"] not in known and r["siret"] not in done]

    # Biggest first. "Don't scrape the whole base" is a standing rule here, and
    # a sole trader with no employees almost never has a website — measured at
    # 0/40 on an unsorted pilot. Businesses with a declared workforce are a
    # different population, so whatever fraction of the run completes covers
    # the most promising targets first.
    EFF_ORDER = {"": 0, "0 salarie": 1, "1 a 2 salaries": 2, "3 a 5 salaries": 3,
                 "6 a 9 salaries": 4, "10 a 19 salaries": 5, "20 a 49 salaries": 6,
                 "50 a 99 salaries": 7, "100 a 199 salaries": 8}
    todo.sort(key=lambda r: (-EFF_ORDER.get(r["tranche_effectif"], 9),
                             0 if r["enseigne"] else 1))
    if args.limit:
        todo = todo[:args.limit]
    log.info(f"{len(ours)} businesses, {len(known)} already have a site, "
             f"{len(done)} already searched -> {len(todo)} to do "
             f"(backend={args.backend})")

    found = snippet_phones = 0
    try:
        for i, r in enumerate(todo, 1):
            # Enseigne first: it is the name on the shopfront and therefore the
            # name on the website. Raison sociale is a legal string that often
            # appears nowhere on the internet.
            label = r["enseigne"] or r["raison_sociale"]
            q = f'{label} {r["commune"]} boulangerie patisserie'
            try:
                results = search(q, args.backend, max_results=8)
            except (QuotaExceeded, SearchAuthError):
                raise
            except Exception as exc:
                log.warning(f"[{i}/{len(todo)}] {label[:28]}: {type(exc).__name__}")
                with DONE_PATH.open("a", encoding="utf-8") as f:
                    f.write(r["siret"] + "\n")
                continue

            best, rank = "", 0
            seen = []
            for res in results:
                d = urllib.parse.urlparse(res["url"]).netloc.lower().replace("www.", "")
                if not d or d in seen:
                    continue
                seen.append(d)
                if any(x in d for x in AGGREGATORS) or d.endswith(BAD_TLD):
                    continue
                rank = len(seen)
                best = f"https://{d}"
                break

            # Snippet mining, PER RESULT. The geo gate is the whole point:
            # "Boulangerie Martin" exists in every French town. V5's tuning
            # showed a merged-blob gate is exactly the listing-page failure —
            # one geo marker anywhere passes every phone on the page — so the
            # gate and the phone must come from the SAME result. The result's
            # domain is recorded: m2_s14's corroborator can then tell whether
            # a second snippet witness is genuinely a different publisher.
            phone_val, phone_domain, geo_ok = "", "", False
            blob_all = " ".join(f"{res['title']} {res['snippet']} {res['url']}"
                                for res in results)
            for res in results:
                rb = f"{res['title']} {res['snippet']} {res['url']}"
                if not ((r["code_postal"] in rb) or (norm(r["commune"]) in norm(rb))):
                    continue
                geo_ok = True
                ph = extract_phones(f"{res['title']} {res['snippet']}")
                if ph and not phone_val:
                    phone_val = sorted(ph)[0]
                    phone_domain = urllib.parse.urlparse(res["url"]).netloc \
                        .lower().replace("www.", "")
            social = extract_social(blob_all)

            if best or phone_val or social["facebook"] or social["instagram"]:
                flush({"siret": r["siret"], "siren": r["siren"],
                       "raison_sociale": r["raison_sociale"], "enseigne": r["enseigne"],
                       "commune": r["commune"], "code_postal": r["code_postal"],
                       "website": best, "rank": rank, "query": q,
                       "backend": args.backend,
                       "phone": phone_val,
                       "phone_domain": phone_domain,
                       "facebook": social["facebook"],
                       "instagram": social["instagram"],
                       "snippet_geo_ok": "oui" if geo_ok else ""})
                found += bool(best)
                snippet_phones += bool(phone_val)
            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(r["siret"] + "\n")
            if i % 25 == 0 or best:
                log.info(f"[{i}/{len(todo)}] {label[:26]:26.26} -> "
                         f"{best or '(rien)':42.42} found={found}")
    except QuotaExceeded as exc:
        log.warning(f"STOP: {exc}")
    except SearchAuthError as exc:
        sys.exit(f"auth: {exc}")

    log.info("─" * 62)
    log.info(f"candidate sites found: {found} | snippet phones (geo-gated): "
             f"{snippet_phones}")
    log.info(f"quota: {quota_state()}")
    log.info("These are CANDIDATES. m2_s9_emails.py confirms each one against "
             "the SIRET or postcode+name on the page before it counts.")


if __name__ == "__main__":
    main()
