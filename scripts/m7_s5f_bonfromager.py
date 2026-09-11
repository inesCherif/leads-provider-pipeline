"""
M7-S5f — bonfromager.fr fromageries of one région, kept for 03/63 (DB-free)
===========================================================================
Region pages `/auvergne-rhone-alpes/` + `/p2`…`/p5` (611 entries on
2026-09-11) list cards `<a href="/<dd>/<ville>/<slug>">` with a
`<span class="cpVille">63500 Saint-Babel</span>` — the postcode is read
off the card, so only 03/63 fiches are opened. A fiche prints the
address, a legals table with the SIRET and the forme juridique, the
e-mail with an `ⓐ` in place of `@` and a Facebook link. The phone sits
behind a click-to-call endpoint `<path>/tel` that the site's robots.txt
disallows (`Disallow: */tel$`) — it is never requested; this source is
e-mail + SIRET + address only.

Most entries are crémeries / shops: the registry NAF at match time keeps
only the producers (10.51C) — the shop rows simply never match.

Output : checkpoints/bf_listings.csv (';', m7_lib.LISTING_FIELDS), source
         'bonfromager'.
Done   : bf_index.csv, bf_pages_done.txt, bf_listings_done.txt

Usage:
    python scripts/m7_s5f_bonfromager.py --departement 63,03 --pilot 5 --dump
    python scripts/m7_s5f_bonfromager.py --departement 63,03
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
from m7_lib import (CHECK_DIR, LISTING_FIELDS, PAGE_DELAY, LISTING_DELAY,   # noqa: E402
                    make_session, get, strip, listing_excluded, dept_of_cp,
                    read_csv, append_rows, load_done, mark_done)

BASE = "https://bonfromager.fr"
REGION = "auvergne-rhone-alpes"
SOURCE = "bonfromager"
INDEX_PATH = CHECK_DIR / "bf_index.csv"
PAGES_DONE = CHECK_DIR / "bf_pages_done.txt"
LIST_DONE = CHECK_DIR / "bf_listings_done.txt"
OUT_PATH = CHECK_DIR / "bf_listings.csv"
INDEX_FIELDS = ["path", "dept", "name", "postcode", "city"]
# main list card: <h3><a href="/38/ville/slug">Name</a></h3><p class="sub">street, 38120 Ville</p>
CARD_RE = re.compile(r'<h3[^>]*>\s*<a href="(/(\d{2})/[^"/]+/[^"/]+)"[^>]*>(.*?)</a>\s*</h3>\s*<p class="sub">(.*?)</p>', re.S)
CP_RE = re.compile(r"(\d{5})\s+([^<,0-9]+)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("m7_s5f")


def walk_index(sess) -> None:
    done = load_done(PAGES_DONE)
    for p in range(1, 60):
        key = f"{REGION}:{p}"
        if key in done:
            continue
        url = f"{BASE}/{REGION}/" + (f"p{p}" if p > 1 else "")
        html = get(sess, url, log)
        if not html:
            log.info(f"region page {p}: empty — index complete")
            return
        rows = []
        for path, dd, nm, sub in CARD_RE.findall(html):
            cp = CP_RE.search(strip(sub))
            rows.append({"path": path, "dept": dd, "name": strip(nm),
                         "postcode": cp.group(1) if cp else "", "city": strip(cp.group(2)) if cp else ""})
        if not rows:
            log.info(f"region page {p}: no card — index complete")
            return
        append_rows(INDEX_PATH, INDEX_FIELDS, rows)
        mark_done(PAGES_DONE, key)
        log.info(f"region page {p}: {len(rows)} cards")
        time.sleep(PAGE_DELAY)


def parse_fiche(sess, html: str, idx: dict) -> dict:
    name = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    name = strip(name.group(1)) if name else idx["name"]
    addr = re.search(r'<p class="address">.*?<a[^>]*>(.*?)</a>', html, re.S)
    street = ""
    if addr:
        lines = [strip(x) for x in re.split(r"<br\s*/?>", addr.group(1))]
        street = " ".join(x for x in lines if x and not re.match(r"\d{5}\s", x))
    siret = re.search(r"SIRET</th><td>(\d{14})", html)
    email = re.search(r'class="malto"[^>]*>([^<]+)</a>', html)
    email = email.group(1).strip().replace("ⓐ", "@").replace("&#9398;", "@").lower() if email else ""
    if "@" not in email:
        email = ""
    fb = re.search(r'<tr class="facebook">.*?href="([^"]+)"', html, re.S)
    site = re.search(r'<tr class="(?:website|site|web)">.*?href="([^"]+)"', html, re.S)
    typ = re.search(r'<span class="type def">(.*?)</span>', html)
    forme = re.search(r"Forme juridique</th><td>([^<]*)", html)
    # The phone sits behind a click-to-call endpoint `<path>/tel` that
    # robots.txt DISALLOWS (`Disallow: */tel$`, read 2026-09-11) — never
    # fetched. bonfromager contributes e-mail + SIRET + address; the phone
    # comes from the other witnesses.
    phone = mobile = ""
    return {"listing_id": idx["path"].strip("/").split("/")[-1], "dept": dept_of_cp(idx["postcode"]),
            "name": name[:200], "alt_name": "", "phone": phone, "mobile": mobile, "email": email,
            "website": (site.group(1) if site else (fb.group(1) if fb else ""))[:300],
            "city": idx["city"], "postcode": idx["postcode"], "address": street, "url": BASE + idx["path"],
            "description": (f"{typ.group(1)} · {forme.group(1)}" if typ and forme else (typ.group(1) if typ else ""))[:300],
            "productions": "", "categorie": typ.group(1) if typ else "Fromagerie",
            "siret": siret.group(1) if siret else ""}


def main() -> None:
    ap = argparse.ArgumentParser(description="bonfromager.fr fromageries for 03/63")
    ap.add_argument("--departement", default="63,03")
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()
    depts = {d.strip() for d in args.departement.split(",") if d.strip()}
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    sess = make_session()
    walk_index(sess)
    index = [r for r in read_csv(INDEX_PATH) if r["dept"] in depts]
    seen, todo = set(), []
    for r in index:
        if r["path"] not in seen:
            seen.add(r["path"])
            todo.append(r)
    done = load_done(LIST_DONE)
    todo = [r for r in todo if r["path"] not in done]
    if args.pilot:
        todo = todo[:args.pilot]
    log.info(f"index: {len(read_csv(INDEX_PATH))} cards in the region, {len(seen)} in {sorted(depts)}, {len(todo)} to open")
    n = skipped = w_email = w_phone = w_siret = 0
    for i, r in enumerate(todo, 1):
        html = get(sess, BASE + r["path"], log)
        if not html:
            continue
        if args.dump and n == 0:
            (CHECK_DIR / "bf_sample.html").write_text(html, encoding="utf-8")
        row = parse_fiche(sess, html, r)
        why = listing_excluded(row["name"], row["categorie"], row["website"], row["email"], row["description"])
        if why:
            skipped += 1
            mark_done(LIST_DONE, r["path"])
            log.info(f"[{i}/{len(todo)}] EXCLU {row['name'][:40]} <- {why}")
            time.sleep(LISTING_DELAY)
            continue
        append_rows(OUT_PATH, LISTING_FIELDS, [row])
        mark_done(LIST_DONE, r["path"])
        n += 1
        w_email += bool(row["email"]); w_phone += bool(row["phone"]); w_siret += bool(row["siret"])
        log.info(f"[{i}/{len(todo)}] {row['name'][:28]:28.28} {row['postcode']:5} {row['city'][:14]:14.14} "
                 f"{row['phone'] or '-':14} {row['email'] or '-':30.30} {row['siret'] or '-':14} {row['description'][:24]}")
        time.sleep(LISTING_DELAY)
    log.info("-" * 62)
    log.info(f"written this run: {n} | skipped on principle {skipped} | phone {w_phone} | e-mail {w_email} | siret {w_siret}")
    log.info(f"file: {OUT_PATH} ({len(read_csv(OUT_PATH))} rows total)")


if __name__ == "__main__":
    main()
