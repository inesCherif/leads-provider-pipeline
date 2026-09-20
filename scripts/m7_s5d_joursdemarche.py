"""
M7-S5d — jours-de-marche.fr producteurs locaux for one département (DB-free)
============================================================================
Tiny (20 fiches per dept on 2026-09-11) but every fiche prints the phone
as a `tel:` link, the e-mail as a `mailto:`, the postal address block and,
on most, the SIRET — the only directory in Sam's family that hands us the
identifier directly (SIRET-exact rung in m7_s8).

Index : /producteur-local/<dd>-<dept-slug>/   (cards = producteur-local/<slug>-<id>.html)
Fiche : that URL.

Output : checkpoints/jdm_listings.csv (';', m7_lib.LISTING_FIELDS), source
         'jours_de_marche'. The host stays in AGRI_AGGREGATORS: a listing
         is a witness, never a farm's own site.
Done   : jdm_listings_done.txt

Usage:
    python scripts/m7_s5d_joursdemarche.py --departement 63 --dump
    python scripts/m7_s5d_joursdemarche.py --departement 03
"""

import argparse
import html as htmlmod
import logging
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import normalize_fr_phone                                # noqa: E402
from m7_lib import (CHECK_DIR, LISTING_FIELDS, LISTING_DELAY,               # noqa: E402
                    make_session, get, strip, listing_excluded, dept_of_cp,
                    read_csv, append_rows, load_done, mark_done, is_social)
from france_lib import in_dept                                             # noqa: E402

BASE = "https://www.jours-de-marche.fr"
DEPT_SLUGS = {"03": "03-allier", "63": "63-puy-de-dome", "15": "15-cantal", "43": "43-haute-loire", "42": "42-loire",
              # PACA (2026-09-20) — read off the site's own /producteur-local/ index (95 slugs), not guessed
              "04": "04-alpes-de-haute-provence", "05": "05-hautes-alpes", "06": "06-alpes-maritimes",
              "13": "13-bouches-du-rhone", "83": "83-var", "84": "84-vaucluse"}
SOURCE = "jours_de_marche"
LIST_DONE = CHECK_DIR / "jdm_listings_done.txt"
OUT_PATH = CHECK_DIR / "jdm_listings.csv"
EMAIL_BLOCK = ("mediawix", "jours-de-marche", "sentry", "example")
SITE_SKIP = ("jours-de-marche", "agencebio", "rackcdn", "facebook", "instagram", "google", "twitter", "youtube")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("m7_s5d")


def parse_fiche(html: str, url: str, lid: str) -> dict:
    name = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    name = strip(name.group(1)) if name else lid
    blk = re.search(r"Adresse postale</strong>(.*?)</p>", html, re.S)
    lines = [strip(x) for x in re.split(r"<br\s*/?>", blk.group(1))] if blk else []
    lines = [x for x in lines if x]
    cp = city = street = ""
    for ln in lines:
        m = re.match(r"(\d{5})\s+(.*)", ln)
        if m:
            cp, city = m.group(1), m.group(2)
        elif strip(ln).lower() != name.lower():
            street = (street + " " + ln).strip()
    tels = [normalize_fr_phone(t) for t in re.findall(r'href="tel:([^"]+)"', html)]
    tels = list(dict.fromkeys(t for t in tels if t))
    emails = [e.lower() for e in re.findall(r'href="mailto:([^"?]+)', html) if not any(b in e.lower() for b in EMAIL_BLOCK)]
    siret = re.search(r"siret\D{0,40}(\d{3}\s?\d{3}\s?\d{3}\s?\d{5})", html, re.I)
    siret = re.sub(r"\D", "", siret.group(1)) if siret else ""
    # the farm's own site is the fa-globe line of the Coordonnées block;
    # label links (demeter.fr, natureetprogres.org, igp-aop.fr) sit elsewhere
    site = ""
    m = re.search(r'fa-globe[^>]*></span>\s*<a href="(https?://[^"]+)"', html)
    if m and not is_social(m.group(1)) and not any(s in m.group(1).lower() for s in SITE_SKIP):
        site = htmlmod.unescape(m.group(1))[:300]
    desc = re.search(r'name="description" content="([^"]*)"', html)
    desc = strip(htmlmod.unescape(desc.group(1))) if desc else ""
    labels = " - ".join(sorted(set(re.findall(r'title="(Agriculture biologique|AOP|AOC|IGP|Label Rouge|Demeter|Nature & Progr[eè]s)"', html))))
    return {"listing_id": lid, "dept": dept_of_cp(cp), "name": name[:200], "alt_name": "",
            "phone": tels[0] if tels else "", "mobile": tels[1] if len(tels) > 1 else "",
            "email": emails[0] if emails else "", "website": site, "city": city, "postcode": cp,
            "address": street, "url": url, "description": desc[:1500], "productions": labels,
            "categorie": desc[:300], "siret": siret}


def main() -> None:
    ap = argparse.ArgumentParser(description="jours-de-marche.fr producteurs for one dept")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()
    dept = args.departement
    slug = DEPT_SLUGS.get(dept) or sys.exit(f"no slug for dept {dept}")
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    sess = make_session()
    html = get(sess, f"{BASE}/producteur-local/{slug}/", log)
    if not html:
        sys.exit("index page empty")
    fiche_re = r'href="(https://www\.jours-de-marche\.fr/producteur-local/[^"]+-\d+\.html)"'
    urls = set(re.findall(fiche_re, html))
    # The département page prints its first 20 fiches and NO pagination (measured
    # 2026-09-20: Vaucluse announces 41, serves 20; /2/, ?page=2 return page 1).
    # The rest is reached through the commune pages it links. 03 / 63 never showed
    # it: they hold fewer than 20 fiches each.
    communes = sorted(set(re.findall(r'href="(https://www\.jours-de-marche\.fr/producteur-local/(\d{5})-[a-z0-9-]+/)"', html)))
    n_dept = len(urls)
    for cu, cp in communes:
        if not in_dept(cp, dept):
            continue
        ch = get(sess, cu, log)
        if ch:
            urls |= set(re.findall(fiche_re, ch))
        time.sleep(LISTING_DELAY)
    log.info(f"[{dept}] département page {n_dept} fiches, + {len(urls) - n_dept} through {len(communes)} commune pages")
    urls = sorted(urls)
    done = load_done(LIST_DONE)
    todo = [u for u in urls if u.rsplit("-", 1)[-1].split(".")[0] not in done]
    log.info(f"[{dept}] index: {len(urls)} producteurs, {len(todo)} to open")
    n = skipped = w_email = w_phone = w_siret = 0
    for i, u in enumerate(todo, 1):
        lid = u.rsplit("-", 1)[-1].split(".")[0]
        h = get(sess, u, log)
        if not h:
            continue
        if args.dump and n == 0:
            (CHECK_DIR / "jdm_sample.html").write_text(h, encoding="utf-8")
        row = parse_fiche(h, u, lid)
        why = listing_excluded(row["name"], row["categorie"], row["website"], row["email"], row["description"])
        if why:
            skipped += 1
            mark_done(LIST_DONE, lid)
            log.info(f"[{i}/{len(todo)}] EXCLU {row['name'][:40]} <- {why}")
            time.sleep(LISTING_DELAY)
            continue
        append_rows(OUT_PATH, LISTING_FIELDS, [row])
        mark_done(LIST_DONE, lid)
        n += 1
        w_email += bool(row["email"]); w_phone += bool(row["phone"]); w_siret += bool(row["siret"])
        log.info(f"[{i}/{len(todo)}] {row['name'][:28]:28.28} {row['postcode']:5} {row['city'][:14]:14.14} "
                 f"{row['phone'] or '-':14} {row['email'] or '-':30.30} {row['siret'] or '-':14} {row['website'][:28]}")
        time.sleep(LISTING_DELAY)
    log.info("-" * 62)
    log.info(f"[{dept}] written this run: {n} | skipped on principle {skipped} | phone {w_phone} | e-mail {w_email} | siret {w_siret}")
    log.info(f"file: {OUT_PATH} ({len(read_csv(OUT_PATH))} rows total)")


if __name__ == "__main__":
    main()
