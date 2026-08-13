# 06 — The V5 plan (approved by Ines 2026-08-13) — ✅ EXECUTED 2026-08-13

> **Status: done, with plan changes forced by measurement.** V5 shipped the
> same day: 721 phones · 287 e-mails · 48.2% reachable · 0 €. Read the V5
> entry in `docs/m2_progress.md` — the pilot measured raw snippet precision
> at **32%, not 76%**, so m2_s16 gained a name gate ("untouched" below is
> superseded); Tavily was split ~600 phones / ~200 websites by Ines; the
> tavily backend exposed and purged a V4 contamination (56 junk e-mails);
> the PJ attach is CODED but not yet run (needs Ines at the keyboard);
> `m2_s20_verify_api.py` now answers the residential-IP SMTP wall for free
> (needs the Reoon key). Kept below for the reasoning.

## Context

V4 shipped 674 phones / 333 e-mails / 47.7% reachable, all free. Three things
changed at the end of that session:

1. **"Tomorrow" is no longer true.** m2_s16 (SERP phone-mining) was blocked
   only because the day's ddgs allowance was spent. Ines added a **fresh
   Tavily API key** to `.env` — and m2_s16 already has `--backend tavily`.
   So the phone-mining runs **immediately**. Tavily is also the better snippet
   source: 14/20 carried a phone in the V3 pilot vs ddgs' 2/20. ddgs stays for
   the day after, as a *second* independent SERP source for corroboration.
2. **Pages Jaunes has one manual route left.** PJ is Cloudflare. The V4
   attempt failed because Playwright *launched* the browser — Cloudflare saw
   an automated profile even in real Chrome, and a human-solved
   `cf_clearance` did not survive automated navigation. The untested route:
   **Ines launches her own Chrome with remote debugging, solves ONE challenge
   herself as a normal visitor, and the script ATTACHES to that trusted
   session** (`connect_over_cdp("http://localhost:9222")`) instead of
   launching anything. Uncertain — newer Cloudflare can detect the attach —
   but it is the best free shot and costs ~10 minutes.
3. Reserves: Serper 201 credits; ddgs 300/day resets each morning.

Decisions already taken by Ines: Tavily goes **mostly to phones**; **try the
PJ CDP attach**.

## Steps

### Step 1 — SERP phone-mining via Tavily (first thing)

- Add an auditable **`--reset-pool tavily`** command to
  `scripts/m2lib_search.py` (sets `used=0` for that pool only, logs it).
  The counter reads `tavily 1000/1000` from the OLD key; the new key means
  a fresh allowance. Never hand-edit `search_quota.json` silently.
- `python scripts/m2_s16_serp_phones.py --backend tavily --pilot 20`
  → measure the returned phones against trusted Maps phones. **Gate ≥70%.**
  If Tavily returns a quota/auth error, the new key shares the old account →
  fall back to ddgs (daily) + 201 Serper credits; document; stop this step.
- Full run: `python scripts/m2_s16_serp_phones.py --backend tavily`
  (hard-stops at 1,000; resumes via `serp_done.txt`). Output
  `serp_snippets.csv` is already read by `m2_s14_export_v3.py` (~line 303)
  for corroboration — an agreeing Tavily snippet promotes a `piste` phone to
  the dialled column as `corrobore(a+b)`.

### Step 2 — Pages Jaunes via CDP attach (parallel; best-effort; needs Ines)

Add `--attach` mode to `scripts/m2_s6_pagesjaunes.py`: instead of
`launch_persistent_context`, use
`p.chromium.connect_over_cdp("http://localhost:9222")`, grab the existing
context/page, keep `extract_listings` + `flush_rows` + `pj_done.txt` logic
unchanged.

Run order:
1. Ines closes all Chrome windows, then runs:
   `"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\pj_cdp_profile"`
2. She browses to pagesjaunes.fr HERSELF, solves the challenge, confirms a
   bakery search actually shows results.
3. `python scripts/m2_s6_pagesjaunes.py --attach --pilot 1` — crawls ONE
   commune and **saves the raw HTML**: we have never seen a real PJ results
   page (always blocked), so `extract_listings`' selectors are guesses that
   must be fixed against reality before scaling.
4. Selectors fixed → full crawl (resumable, 3–6 s delays). `pj_listings.csv`
   is already wired into `m2_s7_match.py` SOURCES and `pagesjaunes` into
   PHONE_RANK — zero downstream changes.
5. **Give-up criterion:** attached session still 403s or re-challenges on
   navigation → PJ is **definitively dead for free**; write that in
   `docs/m2_progress.md` and never retry without new information. Step 1
   captures much of PJ's data via snippets anyway — PJ is upside, not a
   dependency.

### Step 3 — Fold in

- `python scripts/m2_s7_match.py` (picks up PJ listings if any).
- Only if PJ yielded new websites: `python scripts/m2_s9_emails.py` (guards
  already in place).

### Step 4 — Rebuild in the RIGHT ORDER (the V4 timing bug must not repeat)

1. `python scripts/m2_s11_verify.py` — **wait until it finishes**; confirm
   `verified_emails.csv` mtime is fresh. Do not background-race it: last time
   the export won by 3 minutes and shipped 109 proven-invalid addresses, and
   the gate cannot catch that direction (H12 only fails on decreases).
2. Bump `BASENAME` to `"boulangerie_13_v5"` in `scripts/m2_s14_export_v3.py`;
   run it.
3. New `scripts/m2_s19_check_v5.py` = copy of `m2_s17_check_v4.py` with
   XLSX→v5, baseline→v4, logger renamed. Run `--strict`; must pass 19/19.
4. Read the workbook back and assert **0 shipped addresses have an
   `invalide` verdict** in `verified_emails.csv`.

### Step 5 — Document + commit

V5 entry in `docs/m2_progress.md` (Tavily snippet precision, PJ outcome
either way, final numbers); update `CLAUDE.md` handoff and
`docs/enrichment/README.md` table; conventional commits per step, **Ines
authorship only** (no Co-Authored-By), push.

## Files

- **Modified:** `m2lib_search.py` (`--reset-pool`), `m2_s6_pagesjaunes.py`
  (`--attach` + selector fix against first real HTML),
  `m2_s14_export_v3.py` (v5 basename).
- **New:** `scripts/m2_s19_check_v5.py`.
- **Untouched:** `m2_s16_serp_phones.py` (already has `--backend tavily`),
  `m2_s7`, `m2_s9`, `m2_s10`, `m2_s11`, `m2lib_contact`.

## Verification

Pilot-first everywhere (N=20, ≥70% precision vs Maps); PJ pilot judged by
INSPECTING returned HTML, never by HTTP 200; export only after verification's
file is fresh; `m2_s19_check_v5.py --strict` green; workbook read back for
the invalid-addresses assertion.

## Honest expected outcome

| Field | V4 | V5 expected |
|---|---|---|
| Phones | 674 (39.6%) | **~760–950** (snippets + corroboration; +hundreds more if the PJ attach works) |
| E-mails | 333 (19.5%) | ~340–370 (only grows if PJ surfaces new sites) |
| Reachable | 812 (47.7%) | ~52–58% without PJ · **~60–75% with PJ** |

PJ is the swing factor and is treated as upside only. Consumer-ISP SMTP
verification stays `non verifie` (residential IP — the one remaining paid
item, ~5 €/month, buys verification not volume).

## After V5 (already known, do not re-derive)

- **September 1**: Tavily monthly reset (old key) → `m2_s8 --backend tavily`
  for the ~400 still-siteless rows.
- ddgs 300/day → keep running `m2_s16 --backend ddgs` daily until the
  phoneless list is exhausted (adds the second independent SERP witness).
- FB/IG public-page pilot (`m2_s18`): 20 public pages, hard 30% go/no-go,
  **never logged-in** (settled with Ines).
- Supabase ingestion of sector 2 (after delivery; keep `source_sectors`
  scoping or a bakery run re-classifies 128k agriculture rows).
