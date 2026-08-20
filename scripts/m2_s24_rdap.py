"""
M2-S24 — Registrant e-mails from AFNIC RDAP (.fr whois, free, no key)
======================================================================
The registry that runs .fr publishes, for every domain whose holder is a
LEGAL ENTITY (personne morale), the holder's contact e-mail — GDPR redaction
("diffusion restreinte") applies to individuals only. Verified live
2026-08-20: 3 of 4 real bakery domains returned an address, one of them the
owner's personal gmail. That is an e-mail source for exactly the rows we
already trust most: the ones whose own website we validated in m2_s21.

    GET https://rdap.nic.fr/domain/<domain>      (no key, no card, no login)

WHAT SHIPS, and why — ownership is decided BEFORE the write, like everywhere
else in this pipeline:

  * evidence `domaine` — the (siret, domain) pair was judged `valide` by
    m2_s21: the site is PROVEN this business's own. The domain's registrant
    address is then the business's admin contact, even a bare gmail.
  * evidence `nom` — the pair was only `non_verifiable`, so the registrant
    NAME must match the company name (same token ratio as m2_s7's fuzzy
    rung). A non-matching registrant is a hosting agency or a stranger.

WHAT NEVER SHIPS:

  * REGISTRAR PROXIES — Gandi substitutes `<hash>@contact.gandi.net`, OVH
    `<uuid>@q.o-w-o.info`, BookMyName `<hex>@spamfree.bookmyname.com`. Those
    forwarders reach the holder but exist for domain administration; mailing
    prospection to them is abuse and gets the sender flagged.
  * THIRD-PARTY REGISTRANTS — the pilot caught `tech@ovh.net` ("OVH NET")
    and `webmaster@local.fr` ("Local.fr") on VALIDATED bakery domains: when
    an agency registers the domain in its own name, the registrant e-mail
    identifies the AGENCY. So even on a `valide` pair the address ships only
    if the registrant NAME matches the business, or the mailbox sits on the
    business's own domain (normalised: `contact@atelierolivier13.fr` is at
    home on `atelier-olivier13.fr`). This is the mapquest/mappy defect in
    RDAP form, and it is refused at the same place: before the write.
  * REDACTED INDIVIDUALS — AFNIC returns `Ano Nymous` / no e-mail. Skipped.
  * SHARED DOMAINS — a domain validated for >1 SIREN is a network's, and its
    registrant is the network. Skipped before the HTTP call is even made.

Failure rule, unchanged from m1_s9g: OUR failure is not evidence about the
data. A timeout or 5xx leaves the domain OUT of the done-file so a re-run
retries it; only a definite answer (payload, or 404 = not an AFNIC domain)
marks it done.

Output : exports/boulangerie/checkpoints/rdap_emails.csv
Resume : exports/boulangerie/checkpoints/rdap_done.txt (one domain per line)

Usage:
    python scripts/m2_s24_rdap.py --selftest     # ALWAYS first after an edit
    python scripts/m2_s24_rdap.py --pilot 15     # hand-check before scaling
    python scripts/m2_s24_rdap.py                # the rest, resumable
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2_s7_match import tokens, ratio                      # noqa: E402

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
VERDICTS_PATH = CHECK_DIR / "site_verdicts.csv"
OURS_PATH = CHECK_DIR / "etablissements.csv"
OUT_PATH = CHECK_DIR / "rdap_emails.csv"
DONE_PATH = CHECK_DIR / "rdap_done.txt"

RDAP_URL = "https://rdap.nic.fr/domain/{}"
# AFNIC operates these TLDs; anything else 404s at this endpoint.
AFNIC_TLDS = (".fr", ".re", ".pm", ".yt", ".tf", ".wf")
SHIPPABLE = {"valide", "non_verifiable"}
NAME_MIN = 0.62               # m2_s7's FUZZY_MIN, same bar on purpose
RATE_S = 1.0                  # courteous; 12 rapid requests drew no 429, but
                              # there is no quota to race — nothing resets.

# A proxy forwarder reaches the holder but is NOT an outreach address.
PROXY_DOMAINS = {"contact.gandi.net", "spamfree.bookmyname.com"}
PROXY_SUFFIXES = (".o-w-o.info",)
PROXY_WORDS = ("anonym", "redact", "privacy", "whoisprotect", "ano-nymous")
HEX_LOCAL = re.compile(r"[0-9a-f]{16}")   # hashed local part, anywhere in it

FIELDNAMES = ["siret", "siren", "raison_sociale", "domain", "email",
              "registrant", "evidence", "pair_verdict"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s24")


def is_proxy_or_redacted(email: str, registrant: str) -> bool:
    e = (email or "").lower().strip()
    if not e or "@" not in e:
        return True
    local, _, dom = e.partition("@")
    if dom in PROXY_DOMAINS or dom.endswith(PROXY_SUFFIXES):
        return True
    if HEX_LOCAL.search(local):
        return True
    if any(w in e for w in PROXY_WORDS):
        return True
    if "ano nymous" in (registrant or "").lower():
        return True
    return False


def dom_core(d: str) -> str:
    """'atelier-olivier13.fr' -> 'atelierolivier13': the identity without
    punctuation or TLD, so one business's two spellings compare equal."""
    d = (d or "").lower().strip()
    d = re.sub(r"\.(fr|re|pm|yt|tf|wf|com|net|org|eu)$", "", d)
    return re.sub(r"[^a-z0-9]", "", d)


def at_home(email: str, domain: str) -> bool:
    """Is the mailbox on the business's own domain (or a spelling of it)?"""
    edom = email.partition("@")[2]
    return bool(dom_core(edom)) and dom_core(edom) == dom_core(domain)


def parse_registrant(payload: dict) -> tuple[str, str]:
    """(registrant name, e-mail) from an RDAP payload; '' when absent.

    The registrant entity is preferred; the administrative contact is the
    fallback because AFNIC often mirrors the holder there. vCard properties
    are positional: ["email", {params}, "text", "x@y"].
    """
    best = ("", "")
    for want in ("registrant", "administrative"):
        for ent in payload.get("entities", ()):
            if want not in (ent.get("roles") or ()):
                continue
            fn, mail = "", ""
            vcard = ent.get("vcardArray") or []
            props = vcard[1] if len(vcard) == 2 else []
            for p in props:
                if len(p) < 4:
                    continue
                if p[0] == "fn" and not fn:
                    fn = str(p[3]).strip()
                elif p[0] == "org" and not fn:
                    fn = str(p[3]).strip()
                elif p[0] == "email" and not mail:
                    mail = str(p[3]).strip().lower()
            if fn and not best[0]:
                best = (fn, best[1])
            if mail:
                return (fn or best[0], mail)
    return best


def fetch(domain: str, timeout: float = 20.0):
    """RDAP payload, 'absent' on 404, or None on OUR failure (retry later)."""
    req = urllib.request.Request(
        RDAP_URL.format(domain),
        headers={"Accept": "application/rdap+json",
                 "User-Agent": "leads-pipeline-m2s24/1.0"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "absent"
            if e.code == 429:
                time.sleep(10.0 * attempt)
                continue
            log.warning(f"{domain}: HTTP {e.code}")
            return None
        except Exception as e:
            log.warning(f"{domain}: {e.__class__.__name__}: {e}")
            time.sleep(2.0 * attempt)
    return None


def selftest() -> None:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}: {got!r}"
              + ("" if good else f" (wanted {want!r})"))

    raw = {"entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [
        ["version", {}, "text", "4.0"],
        ["fn", {}, "text", "ML Investissement"],
        ["email", {}, "text", "JulienMoreau@Gmail.com"]]]}]}
    check("raw registrant e-mail parsed", parse_registrant(raw),
          ("ML Investissement", "julienmoreau@gmail.com"))

    admin_only = {"entities": [
        {"roles": ["registrant"], "vcardArray": ["vcard", [
            ["fn", {}, "text", "LF ST MENET"]]]},
        {"roles": ["administrative"], "vcardArray": ["vcard", [
            ["email", {}, "text", "chef@fournil.fr"]]]}]}
    check("administrative fallback", parse_registrant(admin_only),
          ("LF ST MENET", "chef@fournil.fr"))

    check("gandi proxy refused",
          is_proxy_or_redacted("82a344af@contact.gandi.net", "X"), True)
    check("ovh proxy refused",
          is_proxy_or_redacted("ab12@q.o-w-o.info", "X"), True)
    check("hex-hash local refused",
          is_proxy_or_redacted("7f1def0123456789ab@mail.fr", "X"), True)
    check("redacted person refused",
          is_proxy_or_redacted("x@y.fr", "Ano Nymous"), True)
    check("real gmail kept",
          is_proxy_or_redacted("julienmoreau@gmail.com", "ML Investissement"),
          False)
    check("bookmyname proxy refused",
          is_proxy_or_redacted("b29d1c4861cc6868.1178956@spamfree.bookmyname.com",
                               "Zeronet Networks"), True)
    # The pilot's two agency leaks: name differs AND mailbox not on the
    # business's domain -> refused even on a valide pair.
    check("own-domain mailbox recognised",
          at_home("contact@atelierolivier13.fr", "atelier-olivier13.fr"), True)
    check("hoster mailbox is not at home",
          at_home("tech@ovh.net", "au-royaume-des-abeilles.fr"), False)
    check("agency mailbox is not at home",
          at_home("webmaster@local.fr", "boulangerie-patisserie-coulin.fr"),
          False)
    # The name gate: registrant must match the company for `nom` evidence.
    check("name match accepts",
          ratio(tokens("BOULANGERIE AIXOISE"),
                tokens("SARL BOULANGERIE AIXOISE")) >= NAME_MIN, True)
    check("name mismatch refuses",
          ratio(tokens("WEB AGENCY SUD"),
                tokens("BOULANGERIE AIXOISE")) >= NAME_MIN, False)
    print("selftest:", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Registrant e-mails via AFNIC RDAP")
    ap.add_argument("--pilot", type=int, default=0, help="stop after N domains")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()

    if not VERDICTS_PATH.exists():
        sys.exit(f"{VERDICTS_PATH} missing — run scripts/m2_s21_validate_sites.py first.")
    with OURS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        ours = {r["siret"]: r for r in csv.DictReader(fh, delimiter=";")}

    # domain -> [(siret, verdict)], shippable pairs only, AFNIC TLDs only.
    pairs: dict[str, list] = {}
    with VERDICTS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            d = (r.get("domain") or "").strip().lower()
            if r.get("verdict") in SHIPPABLE and d.endswith(AFNIC_TLDS) \
                    and r.get("siret") in ours:
                pairs.setdefault(d, []).append((r["siret"], r["verdict"]))

    # A domain validated for >1 SIREN belongs to a network; its registrant
    # identifies the network, not any one shop. Skip before any HTTP.
    n_shared = 0
    targets = {}
    for d, pl in pairs.items():
        sirens = {ours[s]["siren"] for s, _ in pl}
        if len(sirens) > 1:
            n_shared += 1
            continue
        targets[d] = pl

    done = set()
    if DONE_PATH.exists():
        done = {l.strip() for l in DONE_PATH.read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = sorted(d for d in targets if d not in done)
    log.info(f"AFNIC-eligible domains: {len(pairs)} | shared skipped: {n_shared} "
             f"| already done: {len(done & set(targets))} | to query: {len(todo)}")
    if args.pilot:
        todo = todo[:args.pilot]

    seen_rows = set()
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            seen_rows = {(r["siret"], r["domain"]) for r in csv.DictReader(fh, delimiter=";")}
    new_file = not OUT_PATH.exists()

    stats = Counter()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as out_fh, \
         DONE_PATH.open("a", encoding="utf-8") as done_fh:
        w = csv.DictWriter(out_fh, fieldnames=FIELDNAMES, delimiter=";")
        if new_file:
            w.writeheader()
        for i, d in enumerate(todo, 1):
            payload = fetch(d)
            if payload is None:
                stats["our failure (will retry)"] += 1
                continue
            if payload == "absent":
                stats["not an AFNIC domain"] += 1
                done_fh.write(d + "\n"); done_fh.flush()
                continue
            registrant, email = parse_registrant(payload)
            if is_proxy_or_redacted(email, registrant):
                stats["proxy/redacted/none"] += 1
                done_fh.write(d + "\n"); done_fh.flush()
                time.sleep(RATE_S)
                continue
            reg_tok = tokens(registrant)
            for siret, verdict in targets[d]:
                if (siret, d) in seen_rows:
                    continue
                b = ours[siret]
                name_ok = reg_tok and max(
                    ratio(reg_tok, tokens(b["raison_sociale"])),
                    ratio(reg_tok, tokens(b.get("enseigne", "")))) >= NAME_MIN
                home = at_home(email, d)
                # A validated domain proves the SITE is the business's; it
                # does not prove the REGISTRANT is — an agency registers in
                # its own name (tech@ovh.net, webmaster@local.fr, both caught
                # by the pilot). The address must identify the business:
                # matching registrant name, or a mailbox on its own domain.
                if verdict == "valide" and (name_ok or home):
                    evidence = "domaine" + ("+nom" if name_ok else "")
                elif name_ok:
                    evidence = "nom"
                elif verdict == "valide":
                    stats["registrant is a third party (agency/hoster)"] += 1
                    continue
                else:
                    stats["non_verifiable pair, registrant name differs"] += 1
                    continue
                w.writerow({"siret": siret, "siren": b["siren"],
                            "raison_sociale": b["raison_sociale"], "domain": d,
                            "email": email, "registrant": registrant,
                            "evidence": evidence, "pair_verdict": verdict})
                out_fh.flush()
                seen_rows.add((siret, d))
                stats[f"shipped ({evidence.split('+')[0]})"] += 1
                log.info(f"[{i}/{len(todo)}] {d} -> {email}  "
                         f"({registrant[:30]!r}, {evidence})")
            done_fh.write(d + "\n"); done_fh.flush()
            time.sleep(RATE_S)

    log.info("─" * 62)
    for k, v in stats.most_common():
        log.info(f"  {k:<44} {v:>5}")
    log.info(f"output -> {OUT_PATH}")
    log.info("Next: python scripts/m2_s11_verify.py  (then m2_s14 — H17 order)")


if __name__ == "__main__":
    main()
