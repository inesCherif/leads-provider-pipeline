"""
m2lib_contact — French phone + social-link utilities shared by the M2 scripts
=============================================================================
Library, not a stage: no step number, no side effects, no network, no files.
Imported by m2_s13 (places harvest), m2_s8 (snippet parsing), m2_s9 (site
crawl) and m2_s14 (export precedence). Run `--selftest` after any edit.

Why a phone module needs to exist at all (both lessons already paid for):

  * Agriculture's dedup once collapsed 12.5% of the base as "duplicates"
    because thousands of rows shared the SAME premium-rate 08 99 number —
    a paid hotline printed by a directory, not the farm's own line. Premium
    numbers must therefore be FLAGGED, never silently trusted or silently
    dropped: `is_surtaxe()` is that flag.
  * Sources write the same number five ways (+33 4..., 0033 4..., 04.91...,
    tel: links). Dedup and precedence only work on a single canonical form:
    `normalize_fr_phone()` produces `0X XX XX XX XX` or "" (invalid).

Social links: bakeries live on Facebook/Instagram more than on the open web.
`extract_social()` pulls page URLs out of HTML while rejecting share/login/
plugin widget URLs, which outnumber the real page link on most sites.

Usage:
    python scripts/m2lib_contact.py --selftest
"""

import argparse
import re
import sys
from collections import Counter

# ---------------------------------------------------------------- phones ----

# Broad candidate net; validation happens in normalize_fr_phone(). The
# lookarounds stop us matching 10 digits out of the middle of a SIRET or a
# timestamp — a 14-digit identifier must not yield a "phone".
PHONE_CAND_RE = re.compile(
    r"(?<![\d/])(?:\+33|0033|0)[\s.\-]?[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")

# 089x is surtaxé (premium); 081x/082x are "tarif majoré" (surchargeable).
# 080x (numéro vert) is free and NOT flagged.
SURTAXE_PREFIXES = ("089", "081", "082")


def normalize_fr_phone(raw: str) -> str:
    """Canonical `0X XX XX XX XX`, or "" when it is not a French number."""
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("0033"):
        d = "0" + d[4:]
    elif d.startswith("33") and len(d) == 11:
        d = "0" + d[2:]
    if len(d) != 10 or not d.startswith("0") or d[1] == "0":
        return ""
    return " ".join(d[i:i + 2] for i in range(0, 10, 2))


def is_surtaxe(phone: str) -> bool:
    """True for premium/surchargeable prefixes. Input: any format."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("0033"):
        d = "0" + d[4:]
    elif d.startswith("33") and len(d) == 11:
        d = "0" + d[2:]
    return d.startswith(SURTAXE_PREFIXES)


def extract_phones(html: str) -> set:
    """Normalized French phone numbers found in HTML (incl. tel: links)."""
    out = set()
    for m in PHONE_CAND_RE.finditer(html or ""):
        p = normalize_fr_phone(m.group(0))
        if p:
            out.add(p)
    return out


# ---------------------------------------------------------------- social ----

FB_RE = re.compile(
    r"https?://(?:www\.|m\.|fr-fr\.|web\.)?facebook\.com/([^\s\"'<>?#]+)", re.I)
IG_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{2,30})/?(?=[\s\"'<>?#/]|$)", re.I)
LI_RE = re.compile(
    r"https?://(?:[a-z]{2}\.)?linkedin\.com/(company|in)/([^\s\"'<>?#/]+)", re.I)

# Widget/share/login paths that appear on almost every site and are NOT the
# business's own page. First path segment, lowercased, compared exactly.
FB_JUNK = {"sharer", "sharer.php", "share", "share.php", "login", "login.php",
           "plugins", "dialog", "hashtag", "events", "watch", "reel", "groups",
           "policies", "help", "privacy", "legal", "tr", "l.php", "photo.php",
           "story.php", "home.php", "recover", "pages", "marketplace", "about"}
IG_JUNK = {"p", "reel", "reels", "explore", "accounts", "stories", "share",
           "developer", "about", "legal", "directory", "invites"}


def extract_social(html: str) -> dict:
    """{'facebook': url|'', 'instagram': url|'', 'linkedin': url|''}.

    When several candidates appear, the most frequent wins — a site links its
    own page from every footer, but a share widget only once per article.
    """
    fb, ig, li = Counter(), Counter(), Counter()
    for m in FB_RE.finditer(html or ""):
        path = m.group(1).rstrip("/").rstrip("\\")
        seg = path.split("/")[0].lower()
        if not path or seg in FB_JUNK or seg.endswith(".php"):
            continue
        fb[f"https://www.facebook.com/{path}"] += 1
    for m in IG_RE.finditer(html or ""):
        handle = m.group(1).rstrip(".")
        if handle.lower() in IG_JUNK:
            continue
        ig[f"https://www.instagram.com/{handle}"] += 1
    for m in LI_RE.finditer(html or ""):
        li[f"https://www.linkedin.com/{m.group(1).lower()}/{m.group(2)}"] += 1
    return {
        "facebook":  fb.most_common(1)[0][0] if fb else "",
        "instagram": ig.most_common(1)[0][0] if ig else "",
        "linkedin":  li.most_common(1)[0][0] if li else "",
    }


# -------------------------------------------------------------- selftest ----

def selftest() -> int:
    failed = 0

    def check(label, got, want):
        nonlocal failed
        ok = got == want
        if not ok:
            failed += 1
        print(f"  {'OK ' if ok else 'FAIL'} {label}: {got!r}"
              + ("" if ok else f"  (expected {want!r})"))

    print("normalize_fr_phone:")
    check("+33 spaced", normalize_fr_phone("+33 4 91 12 34 56"), "04 91 12 34 56")
    check("0033 glued", normalize_fr_phone("0033491123456"), "04 91 12 34 56")
    check("dots", normalize_fr_phone("04.91.12.34.56"), "04 91 12 34 56")
    check("tel: link", normalize_fr_phone("tel:+33-6-12-34-56-78"), "06 12 34 56 78")
    check("bare 33", normalize_fr_phone("33491123456"), "04 91 12 34 56")
    check("too short", normalize_fr_phone("0491 12 34"), "")
    check("leading 00", normalize_fr_phone("0091123456"), "")
    check("not FR", normalize_fr_phone("+1 415 555 0100"), "")
    check("empty", normalize_fr_phone(""), "")

    print("is_surtaxe:")
    check("0891 premium", is_surtaxe("0891 67 20 00"), True)
    check("0820 majoré", is_surtaxe("08 20 12 34 56"), True)
    check("+33 891", is_surtaxe("+33 891 67 20 00"), True)
    check("0800 free", is_surtaxe("0800 12 34 56"), False)
    check("mobile", is_surtaxe("06 12 34 56 78"), False)
    check("landline 04", is_surtaxe("04 91 12 34 56"), False)

    print("extract_phones:")
    check("plain", extract_phones("Appelez le 04 91 12 34 56 !"), {"04 91 12 34 56"})
    check("tel link", extract_phones('<a href="tel:+33491123456">'), {"04 91 12 34 56"})
    # The trap: a SIRET must never yield a phone.
    check("SIRET immune", extract_phones("SIRET 13000556789012 RCS Marseille"), set())
    check("two formats one number",
          extract_phones("Tél: 04.91.12.34.56 ou +33 4 91 12 34 56"),
          {"04 91 12 34 56"})
    check("date-like ignored", extract_phones("du 01 02 2026 au 03 04 2026"), set())
    check("surtaxé still extracted (flagging is the caller's job)",
          extract_phones("Service client 0891 67 20 00"), {"08 91 67 20 00"})

    print("extract_social:")
    html = ('<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>'
            '<a href="https://www.facebook.com/BoulangerieMarius">page</a>'
            '<a href="https://www.facebook.com/BoulangerieMarius/">footer</a>'
            '<a href="https://instagram.com/p/Cxyz123">post</a>'
            '<a href="https://instagram.com/boulangerie.marius">profile</a>'
            '<a href="https://fr.linkedin.com/company/marius-sarl">li</a>')
    got = extract_social(html)
    check("facebook page wins over sharer", got["facebook"],
          "https://www.facebook.com/BoulangerieMarius")
    check("instagram profile not post", got["instagram"],
          "https://www.instagram.com/boulangerie.marius")
    check("linkedin company", got["linkedin"],
          "https://www.linkedin.com/company/marius-sarl")
    check("empty html", extract_social(""), {"facebook": "", "instagram": "", "linkedin": ""})

    print(f"\n{'ALL OK' if not failed else str(failed) + ' FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Phone/social utilities (library)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    ap.print_help()
