# confs.tech (tech-conferences/conference-data)

Probed 2026-11-15. Adapter: `confs_tech`. Kinds: conference.

Community-maintained conference list as JSON in a Git repo, one file per year per topic. MIT licence (`LICENSE.md` is the MIT text without a copyright line). Consumed by confs.tech. Not an API: raw files on `raw.githubusercontent.com`, branch `main`.

## Access

| URL | method | status | size |
|---|---|---|---|
| `raw.githubusercontent.com/tech-conferences/conference-data/main/conferences/<year>/<topic>.json` | GET | 200 | 231 B – 35.4 KB per file |
| same + `If-None-Match` | GET | 304 | ETag is per file, e.g. `"897ee7f9…"` |
| `api.github.com/repos/tech-conferences/conference-data/git/trees/main?recursive=1` | GET | 200 | 99 KB, 395 entries, `truncated: false` |
| `github.com/tech-conferences/conference-data/commits/main.atom` | GET | 200 | 21 KB, 20 commits |
| `github.com/…/commit/<sha>.patch` | GET | 200 | unified diff |

No auth, no paging. Headers on raw: `etag`, `cache-control: max-age=300`, `expires`, `content-type: text/plain; charset=utf-8`, **no `Last-Modified`**. `raw.githubusercontent.com/robots.txt` → 404 (no robots file); `github.com/robots.txt` allows these paths (`Crawl-delay: 1` only for named AI crawlers). 59 sequential raw GETs at 1 s spacing: all 200, no 429, ~200–300 ms each.

Repo layout: `conferences/<year>/<topic>.json` only — no merged file. Files present: 2013 1, 2014–2016 3 each, 2017 11, 2018 19, 2019 28, 2020 29, 2021 33, 2022 33, 2023 34, 2024 37, 2025 32, **2026 32, 2027 25**. Topic names (2026): `accessibility android api clojure cpp css data devops dotnet general graphql groovy haskell ios iot java javascript kotlin leadership networking opensource performance php product python rust security sre tech-comm testing typescript ux`. There is no 2028 file yet. Tooling in the repo is the site build (`scripts/*.ts`), not a schema.

## Discovery

- Enumerate topics per year from the git tree (1 API call) or just hardcode the 32 topic names. Each file is a JSON **array**, already sorted by `startDate` ascending.
- No paging, no filters, no per-event endpoint. "Upcoming" = filter `endDate >= today`; past events are **kept in the file** (2026: 504 of 576 rows already ended on the probe date), so a file read is both a backfill and a refresh.
- Change detection, cheapest first:
  1. `commits/main.atom` — no quota, 20 entries, gives sha + date + message. Messages name the topic file: `Add JSNation for javascript (#8706)`, `ELC Conference 2027: correct date to 22 April 2027, www URLs (#8719)`. Most commits are bot merges adding one conference to several topics.
  2. `<sha>.patch` via `github.com` — exact rows added/removed/edited, a few hundred bytes, no quota.
  3. `git/trees/main` once, compare the tree sha — 1 API call, `x-ratelimit-limit: 60/h` shared per IP.
  4. Per-file `If-None-Match` — 32 conditional GETs per year, all 304 when nothing changed.
- Counts observed on the probe date: **2026: 576 rows across 32 files = 412 unique events** (164 duplicate rows in 108 groups, max 4 copies). **2027: 124 rows across 25 files = 84 unique events** (40 duplicate rows, 25 groups). Commit cadence: newest 2026-11-06, then 2026-11-02, 2026-10-29 (×6), 2026-10-16 — i.e. bursts of ~1–10 merges a week.

## Fields

Per-file JSON object keys (allowed set is `scripts/config/validFields.ts`; only the first 7 are required):

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `name` | `"Heapcon"`, `"Fosdem"` | `title` | free text, occasionally all-caps (`"GITEX AI SERBIA"`) |
| `url` | `"https://heapcon.io/2026"` | `website_url` | the conference site; sometimes a year-agnostic root |
| `startDate` | `"2026-11-05"` | `starts_at` | ISO date, **no time, no timezone**. Set 00:00 in the event-local zone |
| `endDate` | `"2026-11-06"` | `ends_at` | always present (576/576 in 2026); equals `startDate` for 158 of 576 rows → treat as single-day, `ends_at` = 23:59 local |
| `city` | `"Belgrade"`, `"San Jose, CA"` | `location.city` | absent on online-only events; free text, city-state suffixes (`", CA"`, `", TX"`) |
| `country` | `"Serbia"`, `"U.S.A."`, `"U.K."` | `location.country` | English names, **abbreviated forms** `U.S.A.`/`U.K.` need mapping to ISO-3166 (`US`, `GB`). Absent = online-only |
| `online` | `true` / `false` | `format` | see below; the key is missing on 2 of 576 rows |
| `locales` | `"EN"`, `"EN,DE"`, `"EN,FR,PL"` | `language` | comma-separated 2-letter codes; one bogus value `"EN,NE"`. 574/576 present |
| `cfpUrl` | `"https://forms.gle/QBM1aqkL8yS4c6YM8"` | `registration_url` | it is a **call-for-papers** URL, not attendee registration |
| `cfpEndDate` | `"2026-06-01"` | `registration_deadline` | CFP close, always `< startDate` (266/266 in 2026). Good proxy for a submission deadline; not an attendee deadline |
| `cocUrl` | `"https://heapcon.io/2026/code-of-conduct"` | — | 367/576 |
| `twitter` / `mastodon` / `bluesky` | `"@Heapconf"` | — | handles only, never person names |
| `offersSignLanguageOrCC` | `true` | — | accessibility flag, 41/576 |
| `discountCode` | `"FOO20"` | — | 4 rows in 2026 |
| `github` | — | — | declared valid but **0 rows** in 2026 and 2027 |
| — | — | `organizer`, `prize`, `team_size_*`, `description_md`, `summary`, `timezone`, `people`, `eligibility` | not present at all. Organiser can sometimes be inferred from `url` domain only |
| file name | `conferences/2026/general.json` → `general` | `themes` | the **only** topic/theme signal; an event in 3 topics is copied into 3 files |

Format mapping (observed, exact):

| `online` | `city`/`country` | rows 2026 | rows 2027 | → `format` / `location` |
|---|---|---|---|---|
| `true` | absent | 82 | 3 | `online`, `location = None` |
| `true` | present | 114 | 42 | `hybrid` (e.g. Webinale, Berlin, `online: true`) |
| `false` | present | 378 | 79 | `offline` |
| absent | absent | 2 | 0 | `None` / `None` (`AI World Congress` 2026-11-25, `EdTech Conference` 2026-11-26) |

Duplicates: an event listed under several topics is a **byte-identical copy** in each file (164 redundant rows in 2026), except 3 groups where `url` differs between copies (`Eurostar Conference` 2026-06-15: `conference.eurostarsoftwaretesting.com` in `data`/`devops` vs `eurostarconferences.com` in a second `testing` row; `X-Ops Conference Dubai`/`Madrid`: generic vs city subdomain). Dedupe on `(lower(name), startDate)`; keep all topics as `themes`.

## Revisits and identity

- No ids in the payload. Stable `source_id`: `confs_tech:<lower-snake(name)>@<startDate>`, e.g. `confs_tech:heapcon@2026-11-05`. Adding the year keeps next year's edition distinct while surviving `url` and `city` edits.
- Multi-topic copies must collapse to one import; merge their topic names into `themes`.
- Updates are ordinary commits; a **date correction** is a normal edit (commit `ELC Conference 2027: correct date to 22 April 2027`), so `starts_at`/`ends_at` must be updated in place.
- Cancellation = row deleted from the file. No `cancelled` flag exists; a disappearing row could also be a list cleanup — only mark `cancelled` when the conference's own site confirms.
- Finished events **stay in the JSON forever** (all 2013–2025 files still there, 504/576 of 2026 rows already past). There is no results, winners, speaker or attendee data anywhere in the repo; `people` is never available.
- No `Last-Modified` on raw, so ETag is the only validator; the git tree response does carry `last-modified` (`Fri, 06 Nov 2026 11:17:08 GMT`).

## Rate and failure behavior

- No rate limiting seen: 59 raw GETs at ≥1 s spacing, all 200, 231 B–35.4 KB, ~200–300 ms latency, `x-cache: MISS` then `HIT`.
- `api.github.com` anonymous is 60/h per IP and 304s count; a probe call at 18:07 showed `remaining: 15, used: 45` because other clients shared the IP. `commits?path=…` can answer 422 "The request is taking too long to process" (seen on the hackathonist repo) — retry or use the atom feed.
- A missing year/topic file is a plain 404. No redirects, no cookies, no anti-bot.
- Payload is untrusted input: `url`/`cfpUrl`/`cocUrl` values are user-submitted (validated only by the repo's CI link checker), so fetch targets must pass SSRF checks (§19.4.6).

## Volume and relevance

| scope | 2026 | 2027 |
|---|---|---|
| rows / unique events | 576 / 412 | 124 / 84 |
| `online = true` (all formats online+hybrid) | 196 | 45 |
| online-only (no city) | 82 | 3 |
| hybrid | 114 | 42 |
| Europe (my country list) | 272 | 74 |
| Europe ∩ online | 69 | 24 |
| Serbia | 1 (`Heapcon`, 5–6 Nov 2026) | 1 (`GITEX AI SERBIA`, 26–27 May 2027) |
| Croatia / Romania / Hungary / Greece / Moldova (Balkans excl. Serbia) | 10 rows, 6 unique | 0 | 0 |
| U.S.A. | 109 | 19 |

Relevance today: 412 events in 2026, of which 504 rows are already past — the useful 2026 remainder is 72 rows / 52 unique (0 in Serbia). Serbia is effectively 1 conference a year, so the value is the European pool for the "Europe" coverage requirement rather than local discovery. Noise: heavy US/India share (109 US + 23 India rows in 2026), Web3/AI summits listed under `data`. Filter: `country in EU/EEA+candidates` or `online = true`; drop `endDate < today` unless revisiting.

## Quirks

- Duplicated rows inflate any naive count by 40 %: 576 rows in 2026 is 412 events.
- `country` uses `U.S.A.` and `U.K.`, not ISO; `scripts/config/validLocations.ts` holds the full country→city allow-list (12 KB) and is the authority for spelling (`Czech Republic`, `Bosnia and Herzegovina`, `Türkiye` as `Turkey`).
- No time of day and no timezone anywhere in the file — the only defensible timezone is inferred from `city`/`country`, and pure-online events have none.
- `online: true` does not mean "online only": 114 of 196 online rows are hybrid with a real city.
- 2027 is still filling up (25 of 32 topic files, 84 events); 2026 keeps growing past the year too — 576 rows on the probe date versus the 567 recorded in the survey table two days earlier, so treat those counts as drift, not constants.
- `cfpEndDate` can be far in the past relative to import time (e.g. 2025-11-07 for a 2026-02-24 event) — a non-null deadline is not an open CFP.
- Licences/commercial: MIT, but the site confs.tech asks for attribution; data is user submissions, quality varies (occasional wrong dates, which is exactly what the bot PRs fix).
- `endDate == startDate` on 158/576 rows: 1-day conferences are common.

## Config sketch

```toml
[[source]]
id = "confs-tech"
adapter = "confs_tech"
interval = "24h"
rate = "30/min"
repo = "tech-conferences/conference-data"
branch = "main"
years = [2026, 2027]
topics = "all"
watch_atom = "https://github.com/tech-conferences/conference-data/commits/main.atom"
countries = ["RS", "HR", "SI", "BG", "RO", "HU", "GR", "BA", "ME", "MK", "AL", "MD", "EU"]
include_online = true
default_timezone = "Europe/Belgrade"
dedupe_key = "name+startDate"
```
