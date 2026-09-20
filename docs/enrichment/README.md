# Enrichment sector 2 (boulangeries dept 13) — the complete documentation

> **Note (handover, 2026-09-20).** This is a reference document kept as it was written.
> It sometimes cites working notes that are not part of this repository (per-sector work
> logs `*_progress.md`, `implementation_plan_leads.md`, session handoffs). The current
> commands are in `docs/RUNBOOK.md`; the rules and conventions in `CLAUDE.md`.

Written 2026-08-13, at the end of the V4 session, so that **any new session can
resume cold**. Everything here was measured on this machine; nothing is assumed.

## Files in this folder

| File | What it answers |
|---|---|
| [01-strategy.md](01-strategy.md) | WHY we enrich the way we do — the principles, the two kinds of walls, the free-API doctrine |
| [02-sources.md](02-sources.md) | Every data source tried, its measured result, its verdict, the quotas and API keys |
| [03-scripts.md](03-scripts.md) | Every script: what it does, inputs/outputs, CLI flags, gotchas |
| [04-data-files.md](04-data-files.md) | Every checkpoint CSV and deliverable: columns, who writes it, who reads it |
| [05-rules-and-gates.md](05-rules-and-gates.md) | The precedence rules, the guards, the 19 quality checks, and every incident that created them |
| [06-plan-v5.md](06-plan-v5.md) | **The approved V5 plan — start coding here** |

## The story so far (one screen)

The CEO asked for "les boulangeries du département 13" as an Excel. There was
no client file, so the sector was sourced **entirely from the free registry**
(recherche-entreprises API) — the "S9g" pattern the agriculture docs always
predicted. Everything is **DB-free by design**: scripts read and write files
under `exports/boulangerie/checkpoints/`, no Supabase connection anywhere,
which structurally prevents the pooler-timeout data loss that hit agriculture
three times. Supabase ingestion is a later step, after delivery.

| Version | Delivered | Phones | E-mails | Sites | Reachable | The step |
|---|---|---|---|---|---|---|
| V1 | 2026-08-13 | — | — | — | — | 1,704 rows from the registry, 90.5% with a named dirigeant |
| V2 | 2026-08-13 | 105 | 34 | 57 | 6.6% | OSM harvest + site crawl; every scraped source blocked |
| V3 | 2026-08-13 | 590 | 242 | 1,297 | 39.1% | **Free-tier APIs**: Serper /maps geo-sweep, Tavily, ddgs |
| V4 | 2026-08-13 | 674 | 333 | 1,302 | 47.7% | Named queries, SMTP-proven patterns, corroboration |
| V5 | 2026-08-13 | **721** | **287**¹ | **1,010**¹ | **48.2%** | Tavily name-gated SERP mining + the aggregator purge |

¹ V5's e-mail/site counts are LOWER because **V4 was contaminated**: 56 of its
333 e-mails and ~290 of its "sites" were aggregator/registry pages sold as the
shops' own (measured, 0 unexplained losses — see the V5 progress entry). PJ
attach + ddgs mining + m2_s20 API verification are still in flight and only add.

Current deliverable: **`exports/boulangerie/boulangerie_13_v5.xlsx`** (1,704
rows, 32 columns). Gate: `python scripts/m2_s19_check_v5.py --strict` → 21
checks, 0 failed. `exports/` is gitignored — files exist locally only.

**Total money spent across all of it: 0 €.**

## The one lesson above all others

V2 concluded the contact data needed a ~5 €/month paid IP, because every
scraped route was blocked. That conclusion was **wrong**: the block was on the
*scraper*, not on the *data*. Official APIs with free tiers (Serper `/maps`,
Tavily, ddgs) served the same data unblocked. **Before declaring any source
unreachable, check whether it has an API.** The full doctrine is in
[01-strategy.md](01-strategy.md).

## How to resume in a new session

1. Read this README, then [06-plan-v5.md](06-plan-v5.md).
2. Check quotas: `python scripts/m2lib_search.py --quota`
   (serper 2,299/2,500 used · tavily counter says 1,000/1,000 but a **new key
   is in `.env`** → the counter needs the planned `--reset-pool tavily` ·
   ddgs 300/day, resets daily).
3. Commands, durations and resume rules for every sector: `docs/RUNBOOK.md`.
4. The wider project (agriculture, Supabase pipeline, working conventions):
   `CLAUDE.md` at the repo root — section "Current state and open items".
