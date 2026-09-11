"""
M7-S5c — fermes-locales.fr, the whole of France in one pass (Sam: "contient les mails")
=======================================================================================
WordPress site. The region pages load their cards by JavaScript, but the
sitemap `wp-sitemap-posts-fermes-N.xml` names every farm fiche (1,066 on
2026-09-11), and each fiche is plain HTML with a `#contactfarm` block:

    <b>Nom : </b>Camille Joyeux<br /><b>E-mail :</b> <a href="mailto:…" id="emailfarm">
    <a href="https://www.jardinsdebrenne.com/" …>Voir le site</a>

plus `<h1>name</h1> street CP city` and an og:description. No département
filter exists, so the fiches are ALL opened once (≈ 15 min at 0.8 s) and
the postcode decides the dept — the France index is a by-product.

Output : checkpoints/fl_listings.csv (';', m7_lib.LISTING_FIELDS), source
         'fermes_locales'. Listings hitting the principle are skipped.
Done   : fl_index.csv, fl_listings_done.txt

Usage:
    python scripts/m7_s5c_fermeslocales.py --pilot 10 --dump
    python scripts/m7_s5c_fermeslocales.py
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
from m2lib_contact import normalize_fr_phone, extract_phones_ctx            # noqa: E402
from m7_lib import (CHECK_DIR, LISTING_FIELDS, PAGE_DELAY, LISTING_DELAY,   # noqa: E402
                    make_session, get, strip, listing_excluded, dept_of_cp,
                    read_csv, append_rows, load_done, mark_done, is_social)

BASE = "https://www.fermes-locales.fr"
SITEMAP = BASE + "/wp-sitemap-posts-fermes-{n}.xml"
SOURCE = "fermes_locales"
INDEX_PATH = CHECK_DIR / "fl_index.csv"
LIST_DONE = CHECK_DIR / "fl_listings_done.txt"
OUT_PATH = CHECK_DIR / "fl_listings.csv"
INDEX_FIELDS = ["slug", "url"]
SITE_SKIP = ("fermes-locales", "facebook", "instagram", "twitter", "youtube", "linkedin", "google", "wp.me")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("m7_s5c")


def walk_index(sess) -> list[dict]:
    if INDEX_PATH.exists():
        return read_csv(INDEX_PATH)
    rows = []
    for n in range(1, 10):
        xml = get(sess, SITEMAP.format(n=n), log)
        if not xml:
            break
        locs = [l for l in re.findall(r"<loc>(.*?)</loc>", xml) if "/fermes/" in l]
        rows += [{"slug": l.rstrip("/").split("/")[-1], "url": l} for l in locs]
        log.info(f"sitemap {n}: {len(locs)} farms")
        time.sleep(PAGE_DELAY)
    if not rows:
        sys.exit("no farm URL in the sitemap — the site changed, stop.")
    append_rows(INDEX_PATH, INDEX_FIELDS, rows)
    return rows


def parse_fiche(html: str, idx: dict) -> dict:
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>(.*?)<br", html, re.S)
    name = strip(h1.group(1)) if h1 else idx["slug"].replace("-", " ").title()
    addr_line = strip(h1.group(2)) if h1 else ""
    m = re.search(r"\b(\d{5})\b\s*(.*)$", addr_line)
    cp, city, street = (m.group(1), m.group(2).strip(" ,-"), addr_line[:m.start()].strip(" ,")) if m else ("", "", addr_line)
    city = re.sub(r",?\s*France$", "", city).strip(" ,")
    block = re.search(r'id="contactfarm"(.*?)<!--contactfarm-->', html, re.S)
    block = block.group(1) if block else ""
    contact = re.search(r"<b>\s*Nom\s*:\s*</b>\s*(.*?)<br", block, re.S)
    contact = strip(contact.group(1)) if contact else ""
    email = re.search(r'href="mailto:([^"?]+)', block)
    email = email.group(1).strip().lower() if email else ""
    site = ""
    for u in re.findall(r'href="(https?://[^"]+)"', block):
        if not any(s in u.lower() for s in SITE_SKIP) and not is_social(u):
            site = htmlmod.unescape(u)[:300]
            break
    phones = sorted(p for p in extract_phones_ctx(block) if p)
    tels = [normalize_fr_phone(t) for t in re.findall(r'href="tel:([^"]+)"', html)]
    phones = [t for t in tels if t] + [p for p in phones if p not in tels]
    desc = re.search(r'property="og:description" content="([^"]*)"', html)
    desc = strip(htmlmod.unescape(desc.group(1))) if desc else ""
    cats = sorted({strip(c) for c in re.findall(r'/produits/[^"/]+/"[^>]*>(.*?)</a>', html)}
                  | {strip(c) for c in re.findall(r'/typesferme/[^"/]+/"[^>]*>(.*?)</a>', html)})
    return {"listing_id": idx["slug"], "dept": dept_of_cp(cp), "name": name[:200], "alt_name": contact[:80],
            "phone": phones[0] if phones else "", "mobile": phones[1] if len(phones) > 1 else "",
            "email": email, "website": site, "city": city, "postcode": cp, "address": street,
            "url": idx["url"], "description": desc[:1500], "productions": " - ".join(cats)[:300],
            "categorie": " - ".join(cats)[:300], "siret": ""}


def main() -> None:
    ap = argparse.ArgumentParser(description="fermes-locales.fr, national")
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--recheck-excluded", action="store_true",
                    help="re-open the fiches marked done but absent from the listings file (excluded under an earlier rule, or fetch failures)")
    args = ap.parse_args()
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    sess = make_session()
    index = walk_index(sess)
    done = load_done(LIST_DONE)
    if args.recheck_excluded:
        written = {r["listing_id"] for r in read_csv(OUT_PATH)}
        redo = {s for s in done if s not in written}
        done -= redo
        log.info(f"--recheck-excluded: {len(redo)} fiches re-opened under the current rules")
    todo = [r for r in index if r["slug"] not in done]
    if args.pilot:
        todo = todo[:args.pilot]
    log.info(f"index: {len(index)} farms nationally, {len(todo)} to open")
    n = skipped = w_email = w_phone = w_site = w_contact = 0
    by_dept = {}
    for i, r in enumerate(todo, 1):
        html = get(sess, r["url"], log)
        if not html:
            continue
        if args.dump and n == 0:
            (CHECK_DIR / "fl_sample.html").write_text(html, encoding="utf-8")
        row = parse_fiche(html, r)
        why = listing_excluded(row["name"], row["categorie"], row["website"], row["email"], row["description"])
        if why:
            skipped += 1
            mark_done(LIST_DONE, r["slug"])
            log.info(f"[{i}/{len(todo)}] EXCLU {row['name'][:40]} <- {why}")
            time.sleep(LISTING_DELAY)
            continue
        append_rows(OUT_PATH, LISTING_FIELDS, [row])
        mark_done(LIST_DONE, r["slug"])
        n += 1
        by_dept[row["dept"]] = by_dept.get(row["dept"], 0) + 1
        w_email += bool(row["email"]); w_phone += bool(row["phone"]); w_site += bool(row["website"]); w_contact += bool(row["alt_name"])
        if row["dept"] in ("03", "63") or args.pilot or i % 50 == 0:
            log.info(f"[{i}/{len(todo)}] {row['dept']:2} {row['name'][:28]:28.28} {row['postcode']:5} {row['city'][:14]:14.14} "
                     f"{row['phone'] or '-':14} {row['email'] or '-':30.30} {row['alt_name'][:20]:20.20} {row['website'][:28]}")
        time.sleep(LISTING_DELAY)
    log.info("-" * 62)
    log.info(f"written this run: {n} | skipped on principle {skipped} | phone {w_phone} | e-mail {w_email} "
             f"| site {w_site} | contact person {w_contact} | dept 03: {by_dept.get('03', 0)} dept 63: {by_dept.get('63', 0)}")
    log.info(f"file: {OUT_PATH} ({len(read_csv(OUT_PATH))} rows total)")


if __name__ == "__main__":
    main()
