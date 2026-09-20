"""
M7-S18 — Contact e-mails / phones off the producteurs' public Facebook pages (wrap of m2_s18)
===========================================================================================
`m2_s18_social_emails.py` carries the rules (its OWN empty headless browser,
never Ines's Chrome; public pages only, no login — a wall is a measurement;
page root only; a page shared by two SIREN belongs to neither; the printed
postcode must be ours; e-mails `faible`, phones claims). Measured on bakeries:
42 % of pages gave an e-mail; Instagram 0 of 14 — closed, not tried here.

This wrapper only changes WHERE the pool comes from. M7 never mined social
columns into an export, so the pool is built here from every Facebook URL
the sector holds:
  * `site_contacts.csv` (m7_s4 crawl: the farm's site links its page),
  * `matched_<dept>.csv` / `unmatched_<dept>.csv` website values that ARE a
    Facebook page (Agence Bio and bonfromager register the page as the site),
  * `search_hits.csv` kind=social with a postcode match (m7_s3).
Pseudo-operators `U<source>:<id>` (the Sans SIRET tab) are kept with their
listing postcode. A business that already ships an e-mail is skipped.

Outputs (exports/producteurs/checkpoints/): social_pool.csv (the pool, for
the record), social_ours.csv (siret → postcode, what the core's geo gate
reads), social_emails.csv (m2_s18 FIELDNAMES), social_done.txt.

Usage:
    python scripts/m7_s18_social.py --pilot 20     # go/no-go 30 % (6 of 20)
    python scripts/m7_s18_social.py
    python scripts/m7_s18_social.py --departements Provence-Alpes-Cote-d-Azur --pilot 20
"""

import csv
import logging
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import m2_s18_social_emails as core                                   # noqa: E402
from m7_lib import CHECK_DIR, OUT_DIR, DEPARTEMENTS, read_csv         # noqa: E402
from france_lib import parse_departements                             # noqa: E402


def pop_departements() -> list[str]:
    """--departements is ours, not the core's: strip it before m2_s18 parses argv."""
    if "--departements" not in sys.argv:
        return []
    i = sys.argv.index("--departements")
    if i + 1 >= len(sys.argv):
        sys.exit("--departements needs a value (a list, a région name, or all)")
    spec = sys.argv[i + 1]
    del sys.argv[i:i + 2]
    return parse_departements(spec)


# The pool only ever holds operators of these départements (the shared files
# site_contacts / search_hits are joined on them), so this IS the scope.
DEPTS = pop_departements() or list(DEPARTEMENTS)

POOL_PATH = CHECK_DIR / "social_pool.csv"
OURS_PATH = CHECK_DIR / "social_ours.csv"
POOL_FIELDS = ["siret", "siren", "raison_sociale", "network", "page_url", "postcode", "via"]
FB_HOST_RE = re.compile(r"^(www\.|m\.|fr-fr\.|web\.)?facebook\.com$", re.I)
NOT_ROOT = ("/posts/", "/videos/", "/photos/", "/events/", "/reels/", "/story", "/groups/",
            "/marketplace", "/watch", "/share/", "/permalink", "/p/", "photo.php", "story.php")
NOT_PAGE = {"", "profile.php", "pages", "people", "public", "login", "home.php", "sharer", "sharer.php",
            "hashtag", "search", "help", "policies", "privacy", "about", "groups", "events", "watch",
            # the PLATFORM's page, linked from a site's footer (measured 2026-09-11: WordPress, Wix,
            # Squarespace) and the network's page (bienvenue à la ferme) — nobody's own page
            "wordpresscom", "wordpress", "wix", "wixcom", "squarespace", "shopify", "jimdo", "weebly",
            "bienvenuealaferme", "agencebio", "pagesjaunes", "facebook", "meta"}


def fb_root(url: str) -> str:
    """The page ROOT (H23): https://www.facebook.com/<slug>/ — or '' when not a page."""
    u = (url or "").strip()
    if not u:
        return ""
    if not u.lower().startswith("http"):
        u = "https://" + u
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return ""
    if not FB_HOST_RE.match(p.netloc):
        return ""
    if any(x in p.path.lower() for x in NOT_ROOT):
        return ""
    path = p.path.strip("/")
    if path.lower().startswith("pages/"):                      # /pages/<name>/<id>
        parts = path.split("/")
        return f"https://www.facebook.com/{'/'.join(parts[:3])}/" if len(parts) >= 3 else ""
    if path.lower().startswith("profile.php"):
        pid = urllib.parse.parse_qs(p.query).get("id", [""])[0]
        return f"https://www.facebook.com/profile.php?id={pid}" if pid.isdigit() else ""
    slug = path.split("/")[0]
    if slug.lower() in NOT_PAGE or len(slug) < 3:
        return ""
    return f"https://www.facebook.com/{slug}/"


def build_pool() -> list[dict]:
    """Every Facebook page URL the sector holds, one row per (business, page)."""
    ops, emailed, pseudo_cp, names = {}, set(), {}, {}
    for d in DEPTS:
        for r in read_csv(CHECK_DIR / f"operateurs_{d}.csv", delim=","):
            rid = r["siret"] or f"X{r.get('siren', '')}"
            ops[rid] = r
        # the latest export tells who already ships an e-mail
        exports = sorted((p for p in OUT_DIR.glob(f"producteurs_{d}_v*.csv")
                          if re.fullmatch(rf"producteurs_{d}_v\d+\.csv", p.name)),
                         key=lambda p: int(re.search(r"_v(\d+)\.csv$", p.name).group(1)))
        if exports:
            for r in read_csv(exports[-1], delim=","):
                if (r.get("email_final") or "").strip():
                    emailed.add(r["siret"] or f"X{r.get('siren', '')}")
        for u in read_csv(CHECK_DIR / f"unmatched_{d}.csv"):
            urid = f"U{u['source']}:{u['listing_id']}"
            pseudo_cp[urid] = (u.get("postcode") or "")[:5]
            names[urid] = u.get("name", "")
            if (u.get("email") or "").strip():
                emailed.add(urid)
    cands: dict[tuple, dict] = {}

    def add(rid: str, url: str, via: str) -> None:
        root = fb_root(url)
        if not root or rid in emailed:
            return
        if rid in ops:
            op = ops[rid]
            row = {"siret": rid, "siren": op.get("siren", ""), "raison_sociale": op["raisonSociale"],
                   "postcode": op["codePostal"]}
        elif rid in pseudo_cp:
            row = {"siret": rid, "siren": "", "raison_sociale": names.get(rid, rid), "postcode": pseudo_cp[rid]}
        else:
            return
        row.update({"network": "facebook", "page_url": root, "via": via})
        cands.setdefault((rid, root), row)

    for r in read_csv(CHECK_DIR / "site_contacts.csv"):
        add(r["row_id"], r.get("facebook", ""), "site_crawl")
    for d in DEPTS:
        for r in read_csv(CHECK_DIR / f"matched_{d}.csv"):
            add(r["row_id"], r.get("website", ""), f"{r['source']}:website")
        for u in read_csv(CHECK_DIR / f"unmatched_{d}.csv"):
            add(f"U{u['source']}:{u['listing_id']}", u.get("website", ""), f"{u['source']}:website")
    for r in read_csv(CHECK_DIR / "search_hits.csv"):
        if r.get("kind") == "social" and r.get("geo") == "cp":
            add(r["row_id"], r.get("url", ""), "search")
    # H24: a page named by two different legal units belongs to neither
    owners = defaultdict(set)
    for (rid, root), row in cands.items():
        owners[root].add(row["siren"] or rid)
    pool = [row for (rid, root), row in cands.items() if len(owners[root]) == 1]
    shared = sum(1 for (rid, root) in cands if len(owners[root]) > 1)
    pool.sort(key=lambda r: (0 if not r["siret"].startswith("U") else 1, r["postcode"], r["raison_sociale"]))
    with POOL_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=POOL_FIELDS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(pool)
    with OURS_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["siret", "code_postal", "tranche_effectif"], delimiter=";")
        w.writeheader()
        seen = set()
        for r in pool:
            if r["siret"] not in seen:
                seen.add(r["siret"])
                w.writerow({"siret": r["siret"], "code_postal": r["postcode"],
                            "tranche_effectif": ops.get(r["siret"], {}).get("tranche_effectif", "")})
    core.log.info(f"pool: {len(pool)} Facebook page(s) for {len(seen)} business(es) without an e-mail "
                  f"({sum(1 for r in pool if r['siret'].startswith('U'))} Sans SIRET); "
                  f"dropped {shared} shared by 2+ legal units; by route {dict(Counter(r['via'].split(':')[0] for r in pool))}")
    return pool


def targets(limit: int = 0, network: str = "") -> list:
    pool = build_pool()
    done = core.load_done()
    out = [dict(r, _size="") for r in pool if r["siret"] not in done and (not network or r["network"] == network)]
    return out[:limit] if limit else out


core.CHECK_DIR = CHECK_DIR
core.OURS_PATH = OURS_PATH
core.OUT_PATH = CHECK_DIR / "social_emails.csv"
core.DONE_PATH = CHECK_DIR / "social_done.txt"
core.targets = targets
core.log = logging.getLogger("m7_s18")

if __name__ == "__main__":
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    if "--network" not in sys.argv:
        sys.argv += ["--network", "facebook"]        # Instagram: 0 of 14 measured, closed
    core.main()
