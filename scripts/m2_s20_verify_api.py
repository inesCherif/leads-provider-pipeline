"""
M2-S20 — Verify the residential-IP-blocked addresses through a free-tier API
=============================================================================
Orange, SFR, Outlook, Yahoo and the greylisters refuse SMTP probes from a
residential IP, so m2_s11 leaves those addresses `non verifie` — 129 of 392
on 2026-08-13. That block is on OUR CONNECTION, not on the data. The V3
lesson applies to SMTP exactly as it did to search: a verification API with
a free tier probes from ITS OWN infrastructure (clean reverse-DNS, warmed
IPs), which is precisely what the ~5 EUR/month VPS was going to buy.

Providers implemented (key in .env, first configured one wins):
    REOON_API_KEY            emailverifier.reoon.com — 600 free/month +
                             100 instant credits, NO credit card at signup.
                             This is the recommended one; Reoon was already
                             short-listed in the original project plan.
    MILLIONVERIFIER_API_KEY  millionverifier.com — 10,000 free credits, but
                             signup asked for a VAT number in 2026-08 —
                             the company has one: a company decision.

THE GATE THAT MUST RUN FIRST (--pilot): before the provider is trusted with
addresses we cannot check, it is tested on 20 addresses we CAN check — a
sample of m2_s11's own SMTP verdicts (10 valide + 10 invalide on domains
that answered our probes). More than 2 decisive disagreements out of 20 and
the script refuses to write anything. S9-G's incident rule: our tool's
failure must never be written as evidence about the data — and neither may
a third party's.

Write-back rules:
  - only rows whose verdict is `non verifie` are ever updated — the API
    never overrides our own SMTP evidence;
  - provider "unknown" leaves `non verifie` untouched;
  - detail records provenance: `api:reoon:safe` — auditable per address;
  - m2_s11 REWRITES verified_emails.csv wholesale, so the order is always
    m2_s11 -> m2_s20 -> m2_s14 (the H17 mtime gate enforces export-last).

Quota: persistent monthly counter in checkpoints/verify_api_quota.json,
hard-stop at the free tier, same contract as m2lib_search.

Usage:
    python scripts/m2_s20_verify_api.py --pilot     # ground-truth gate, ~20 credits
    python scripts/m2_s20_verify_api.py             # verify the non-verifie rows
    python scripts/m2_s20_verify_api.py --limit 10  # smaller batch
"""

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
VERIFIED_PATH = CHECK_DIR / "verified_emails.csv"
QUOTA_PATH = CHECK_DIR / "verify_api_quota.json"

LIMITS = {"reoon": 600,             # free tier resets monthly
          "millionverifier": 10_000}  # one-time free credits
TIMEOUT = 30
DELAY = (0.8, 1.6)
MAX_DECISIVE_DISAGREEMENTS = 2      # out of 20 pilot addresses

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s20")


class QuotaExceeded(RuntimeError):
    pass


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass


def charge(provider: str, n: int = 1) -> None:
    q = json.loads(QUOTA_PATH.read_text(encoding="utf-8")) if QUOTA_PATH.exists() else {}
    entry = q.get(provider, {"used": 0})
    month = date.today().strftime("%Y-%m")
    if provider == "reoon" and entry.get("month") != month:
        entry = {"used": 0, "month": month}
    if entry["used"] + n > LIMITS[provider]:
        raise QuotaExceeded(f"{provider}: {entry['used']}/{LIMITS[provider]} used")
    entry["used"] += n
    entry["limit"] = LIMITS[provider]
    q[provider] = entry
    QUOTA_PATH.write_text(json.dumps(q, indent=2), encoding="utf-8")


# ------------------------------------------------------------- providers ----
# Each returns (verdict_fr, raw_status): valide / invalide / risque / None
# (None = no usable signal, leave the row alone).

def _get(url: str, params: dict) -> dict:
    import requests
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def verify_reoon(email: str, key: str) -> tuple:
    payload = _get("https://emailverifier.reoon.com/api/v1/verify",
                   {"email": email, "key": key, "mode": "power"})
    status = (payload.get("status") or "").lower()
    if status in ("safe", "valid", "role_account"):
        # role accounts (contact@) are the NORM for shop mailboxes — a cold-
        # mail tool flags them, a B2B phone-first list keeps them.
        return "valide", status
    if status in ("invalid", "disabled", "disposable", "spamtrap"):
        return "invalide", status
    if status in ("catch_all", "inbox_full"):
        return "risque", status
    return None, status or "?"


def verify_millionverifier(email: str, key: str) -> tuple:
    payload = _get("https://api.millionverifier.com/api/v3/",
                   {"api": key, "email": email})
    result = (payload.get("result") or "").lower()
    if result == "ok":
        return "valide", result
    if result in ("invalid", "disposable"):
        return "invalide", result
    if result == "catch_all":
        return "risque", result
    return None, result or "?"


PROVIDERS = [("reoon", "REOON_API_KEY", verify_reoon),
             ("millionverifier", "MILLIONVERIFIER_API_KEY", verify_millionverifier)]


def pick_provider() -> tuple:
    _load_env()
    for name, env, fn in PROVIDERS:
        key = os.environ.get(env, "")
        if key:
            return name, key, fn
    sys.exit(
        "No verification API key configured. Free options (pick ONE):\n"
        "  1. Reoon (recommended, no credit card): register at\n"
        "     https://emailverifier.reoon.com/register/  -> create an API key,\n"
        "     then add  REOON_API_KEY=...  to .env       (600 free/month)\n"
        "  2. MillionVerifier (asks for a VAT number — company decision):\n"
        "     https://millionverifier.com  -> MILLIONVERIFIER_API_KEY=...\n"
        "Then run:  python scripts/m2_s20_verify_api.py --pilot")


def read_verified() -> list:
    if not VERIFIED_PATH.exists():
        sys.exit(f"{VERIFIED_PATH} not found — run scripts/m2_s11_verify.py first.")
    with VERIFIED_PATH.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def write_verified(rows: list) -> None:
    with VERIFIED_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["email", "domain", "verdict", "detail"],
                           delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)


def run_pilot(provider: str, key: str, fn) -> None:
    """Test the provider against OUR OWN SMTP verdicts before trusting it."""
    rows = read_verified()
    ours_valid = [r for r in rows if r["verdict"] == "valide"
                  and not r["detail"].startswith("api:")]
    ours_invalid = [r for r in rows if r["verdict"] == "invalide"
                    and not r["detail"].startswith("api:")]
    rnd = random.Random(13)          # deterministic sample, dept 13
    sample = rnd.sample(ours_valid, min(10, len(ours_valid))) + \
        rnd.sample(ours_invalid, min(10, len(ours_invalid)))
    log.info(f"pilot: {len(sample)} addresses with a KNOWN verdict -> {provider}")

    agree = disagree = abstain = 0
    for r in sample:
        charge(provider)
        try:
            got, raw = fn(r["email"], key)
        except Exception as exc:
            log.warning(f"  {r['email']:<40} API error: {exc}")
            abstain += 1
            continue
        if got is None or got == "risque":
            abstain += 1
            mark = "~"
        elif got == r["verdict"]:
            agree += 1
            mark = "="
        else:
            disagree += 1
            mark = "X"
        log.info(f"  {mark} {r['email']:<40} ours={r['verdict']:<9} api={got or 'aucun'} ({raw})")
        time.sleep(random.uniform(*DELAY))

    log.info("─" * 62)
    log.info(f"agree {agree} | decisive disagreements {disagree} | abstain {abstain}")
    if disagree > MAX_DECISIVE_DISAGREEMENTS:
        sys.exit(f"GATE FAILED: {disagree} > {MAX_DECISIVE_DISAGREEMENTS} decisive "
                 f"disagreements — do NOT trust {provider} on unverifiable rows.")
    log.info(f"GATE PASSED — {provider} may be run on the non-verifie rows:")
    log.info("  python scripts/m2_s20_verify_api.py")


def run_full(provider: str, key: str, fn, limit: int) -> None:
    rows = read_verified()
    todo_idx = [i for i, r in enumerate(rows) if r["verdict"] == "non verifie"]
    if limit:
        todo_idx = todo_idx[:limit]
    log.info(f"{len(todo_idx)} 'non verifie' rows -> {provider}")

    changed = 0
    stats = {}
    try:
        for k, i in enumerate(todo_idx, 1):
            r = rows[i]
            charge(provider)
            try:
                got, raw = fn(r["email"], key)
            except Exception as exc:
                log.warning(f"[{k}] {r['email']}: API error {exc} — left as-is")
                continue
            stats[raw] = stats.get(raw, 0) + 1
            if got is not None:
                r["verdict"] = got
                r["detail"] = f"api:{provider}:{raw}"
                changed += 1
            if k % 10 == 0 or got == "invalide":
                log.info(f"[{k}/{len(todo_idx)}] {r['email']:<40} -> {got or 'aucun'} ({raw})")
            # Flush every 10 updates — a crash must not lose the batch.
            if changed and changed % 10 == 0:
                write_verified(rows)
            time.sleep(random.uniform(*DELAY))
    except QuotaExceeded as exc:
        log.warning(f"STOP (free tier): {exc} — resumes next month or with the other provider")

    write_verified(rows)
    from collections import Counter
    final = Counter(r["verdict"] for r in rows)
    log.info("─" * 62)
    log.info(f"updated {changed} verdicts via {provider}; raw statuses: {stats}")
    log.info(f"verified_emails.csv now: {dict(final)}")
    log.info("Rebuild the export AFTER this (m2_s14), then gate (m2_s19 --strict).")


def main() -> None:
    ap = argparse.ArgumentParser(description="API-verify the IP-blocked addresses")
    ap.add_argument("--pilot", action="store_true",
                    help="test the provider on 20 known-verdict addresses (REQUIRED first)")
    ap.add_argument("--limit", type=int, default=0, help="cap the batch")
    args = ap.parse_args()

    provider, key, fn = pick_provider()
    log.info(f"provider: {provider}")
    if args.pilot:
        run_pilot(provider, key, fn)
    else:
        run_full(provider, key, fn, args.limit)


if __name__ == "__main__":
    main()
