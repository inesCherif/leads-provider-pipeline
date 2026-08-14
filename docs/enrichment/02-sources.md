# 02 — Sources: everything tried, everything measured (2026-08-13)

All measurements from this machine (residential IP, Marseille-area ISP).
"Verdict" is operational: what a new session should actually do with it.

## ✅ Working sources (all free)

| Source | Access | What it gives | Measured | Verdict |
|---|---|---|---|---|
| **recherche-entreprises.api.gouv.fr** | public API, no key | The population itself: SIRET, names, NAF, geo (1,700/1,704 geocoded), dirigeants split nom/prénom, procédures collectives | 1,704 rows, 90.5% named | The backbone. Query `departement=13` matches any company with an établissement there — build rows from `matching_etablissements`, never the siège |
| **OpenStreetMap Overpass** | public API, no key | 994 bakeries, 383 with `ref:FR:SIRET`, phones, sites | phones agree with Maps **45/47 = 95.7%** | Harvested (m2_s5). Highest-trust phone source (rank 0) |
| **Serper.dev `/maps`** | free tier, **2,500 one-time credits, no card**; `SERPER_API_KEY` | Google Maps listings: ~20/query, phone on ~16/20, website ~5/20, cid, geo | pilot precision 30/31 via matcher | **The V3 unlock.** 3 credits/query. **No pagination** (page 2 = empty) → use more `ll` anchors, never more pages. **Used 2,299/2,500; 201 left** |
| Serper `/places` | same pool, 1 credit | geo + website, **NO phone field** | measured before building | Don't use for phones |
| Serper `/search` | same pool, 1 credit | Google organic + snippets | not piloted alone | Backup web search |
| **Tavily** | free tier, 1,000/month, no card; `TAVILY_API_KEY` | web search, rich snippets | site discovery 17/20; **snippets carried a phone 14/20** | Primary website finder + best snippet source. August allowance spent on the OLD key; **a NEW key is in `.env`** (counter reset needed — see plan) |
| **ddgs** (python lib) | keyless, free | DuckDuckGo-multiplexed search | 20/20 candidate sites in pilot; snippets poor for phones (2/20) | Overflow searcher. Our own politeness cap: 300/day |
| **The businesses' own websites** | requests crawl | e-mails, phones, socials | see rules doc — confirmation is everything | Crawled (m2_s9, `--deep` for sitemap+links). Site *identity* must be proven (SIRET/CP+name on page) |
| **SMTP verification** (m1_s9g core) | direct port-25 | proves/refutes addresses | 8% of own-domains yield a PROVEN pattern address | Works for corporate domains; consumer ISPs refuse residential IPs |

## ❌ Blocked sources — do not retry without new information

| Source | What blocks it | Detail |
|---|---|---|
| **Pages Jaunes** | ✅ **OPEN via CDP attach** — 816 listings, 100% with a phone (2026-08-14) | It is Cloudflare, not DataDome. A **launched** browser is detected and its human-solved `cf_clearance` dies on navigation — that part of the V4 note holds. But **attached over CDP to the human's own Chrome, plain `goto()` returns HTTP 200 with 20 cards**: Cloudflare binds clearance to the browser, not to the act of navigating. Setup + the one false-positive that hid this for a day: see the V6 entry in `m2_progress.md` |
| Brave Search (scraped) | CAPTCHA after ~85 queries | Brave's official API exists but needs a credit card → excluded |
| Bing (2026-08 recipe) | serves results for the wrong query | recipe stale |
| DuckDuckGo lite / Mojeek | 403 | the `ddgs` lib works where raw scraping doesn't |
| Startpage | useless (1 link) | — |
| Google Places API / Brave API | need a credit card | excluded by the 0-€-no-card constraint |
| Facebook / Instagram scraping | login walls + account risk | Public-page pilot allowed (20 pages, 30% go/no-go). **Logged-in scraping: never** — settled with Ines |

## Quota state at end of V4 session

```
serper   2,299 / 2,500  (one-time; 201 reserve)
tavily   1,000 / 1,000  (OLD key, August) — NEW key in .env, counter reset pending
ddgs       300 / 300    (our own cap; resets daily)
```

Counter lives in `exports/boulangerie/checkpoints/search_quota.json`, enforced
by `m2lib_search.charge()` — charge happens BEFORE the request and hard-stops
at the limit. Never hand-edit it silently; the V5 plan adds `--reset-pool`.

## Keys

`SERPER_API_KEY`, `TAVILY_API_KEY` in `.env` (gitignored), documented in
`.env.example`. No key: ddgs, Overpass, recherche-entreprises, SMTP.
