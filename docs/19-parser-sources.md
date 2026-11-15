# 19. Parser: sources and adapters

Survey of 2026-11-15. Implemented adapters (§19.6): `hackathonhub`, `hackathonist`, `ctftime`, `luma`, `jsonld_pages`, `icpc`, `mlh`, `hackalendar`, `kaggle`, `confs_tech`, `devpost`. Access methods, volumes and counts were tested on the survey date and will drift. Tier 1 sources and Kaggle were probed live; their observed behavior, field mappings and config sketches are in [19-parser-sources/](19-parser-sources/), one file per source.

## Access policy

Every working access method is allowed: official APIs with account tokens, undocumented and internal endpoints, public keys taken from a site's own client code, browser user agents, robots-disallowed paths, and sources whose terms restrict automated access. Terms-of-service and robots notes below are facts about the source, not blockers. Rate limits stay self-imposed per domain, with `Crawl-delay` as the minimum spacing (§19.4.6), so a source keeps answering.

## Adapter design

Sources are hot-swappable: adding or removing a place never touches the pipeline (§19.4).

- **Adapter.** One module per adapter under `hmm/parser/sources/`, registered by name. It implements `discover` (yields candidates, fetching listings or API pages) and `extract` (candidate → `EventImport` list, fetching detail pages when needed), and validates its own options. Fetching goes through the shared SSRF-safe, rate-limited fetcher. The schema.org Event JSON-LD extractor is shared (`hmm/parser/jsonld.py`); an LLM fallback is planned.
- **Instance.** A configured use of an adapter in `src/hmm/parser/sources.toml`. One adapter can back many instances (several Luma calendar lists, several Telegram channel lists). Secrets stay in the environment (§22); the TOML names the variable. A missing secret disables the instance with a log line, not a failed run.
- **Pipeline.** Classify, extract, normalize, dedupe, import and heartbeat are generic and never name a source. One worker job walks the instances that are due; a failing instance never stops the others (§19.4.7).
- **Identity.** The instance `id` is the `source_id` prefix (`luma-rs:evt-…`, `tg-rs:@nstechtalks/987`). It is stable: renaming it creates a new source.
- **Removal.** Delete the instance or set `enabled = false`. Its events stay in the catalog; tracked events whose prefix has no live instance are no longer revisited.
- **Generic adapters** make most additions a config line:

| adapter | input | covers |
|---|---|---|
| `jsonld_sitemap` | sitemap URL, URL pattern | HackathonRadar, Sessionize |
| `jsonld_pages` | listing or detail URLs, link pattern, paging | dev.events, TAIKAI, GDG, BBICC |
| `pages` | seed URLs, optional `render = true` | CASSINI, local conferences (CSS fields or LLM extraction) |
| `luma` | calendar IDs, country filter | Belgrade and Novi Sad communities |
| `telegram` | channel names | `t.me/s/<channel>` previews |
| `feed` | RSS/Atom or WP REST URL | Startit.rs, This Week in Rust |

Source-specific adapters exist only where the payload has its own shape: `hackathonhub`, `hackalendar`, `mlh`, `devpost`, `ctftime`, `icpc`, `kaggle`, `dorahacks`, `confs_tech`, `devopsdays`, `hackathonist`, `itch_jams`.

```toml
[[source]]
id = "hackathonhub"
adapter = "hackathonhub"
interval = "12h"
rate = "30/min"
countries = ["RS", "RO", "HU", "BG", "HR", "BA", "SI", "MK", "ME", "AL", "GR"]

[[source]]
id = "luma-rs"
adapter = "luma"
interval = "6h"
rate = "20/min"
calendars = ["cal-WcgY6wHZ5tA68xg", "cal-46L4tD8g5AzO0l9", "cal-xfRCBuSHDpIoEmp"]
countries = ["RS"]
include_online = false

[[source]]
id = "tg-rs"
adapter = "telegram"
interval = "6h"
channels = ["nstechtalks", "xecut_bg", "SERBIAGAMEDEVEVENTS"]

[[source]]
id = "kaggle"
adapter = "kaggle"
interval = "24h"
secret_env = ["KAGGLE_API_TOKEN"]
```

## Tier 1

High relevance, structured or cheap to extract.

| source | kinds | access | adapter | volume | notes |
|---|---|---|---|---|---|
| [hackathonhub.eu](https://hackathonhub.eu/events) | hackathon | site's public Supabase view `events_public` (anon key from the JS bundle), `/events/<slug>.md`; sitemap as fallback | `hackathonhub` | 1,795 published, 444 upcoming, 421 Europe, RS 9 (1 upcoming) | Undocumented API: country filter, `updated_at` for incremental sync. Sitemap `lastmod` is a build stamp and misses 572 live events. No Serbia page exists (unknown routes return a 200 SPA shell). [notes](19-parser-sources/hackathonhub.md) |
| [Hackathonist](https://raw.githubusercontent.com/zsevic/hackathonist/gh-pages/README.md) | hackathon | Markdown tables on `gh-pages`: `README.md` (current year), `2017.md`…`2025.md` | `hackathonist` | 219 rows; 15 in 2026 | No ids, times, country or deadlines; 8 date shapes. Change feed `commits/gh-pages.atom`. Organiser domains seed `pages`. [notes](19-parser-sources/hackathonist.md) |
| [MLH](https://www.mlh.com/seasons/2027/events) | hackathon | Inertia JSON in `<script data-page="app">`; paging needs `X-Inertia` + version headers | `mlh` | season 2027: 48 upcoming (6 EMEA), 85 past; RS 0 | `mlh.io` redirects to `www.mlh.com`. No JSON-LD. Date-only events carry a `01:11:11Z` placeholder. No team size, deadline or organizer. [notes](19-parser-sources/mlh.md) |
| [Hackalendar](https://hackalendar.com) | hackathon | public MCP endpoint `/api/mcp`; `/sitemap.xml` → `/e/{slug}` JSON-LD | `hackalendar` | 86 (20 upcoming, 66 past); RS 0 | MCP payload has timezone, country, prize, registration URL, cancelled flag. Conditional GET works. 13 of 20 upcoming are in France. [notes](19-parser-sources/hackalendar.md) |
| [Devpost](https://devpost.com/api/hackathons?status%5B%5D=open&page=1) | hackathon | `/api/hackathons?status[]=open` (40 per page); subdomain JSON-LD; winners `ItemList` on project galleries | `devpost` | 77 open (55 online, 22 in-person, 0 in Europe) | Only source with winners and members: project JSON-LD authors, GitHub repo links, GitHub avatar ids resolvable to logins. API dates are free text; subdomain JSON-LD has ISO dates with offsets. ToS forbids automated access. [notes](19-parser-sources/devpost.md) |
| [CTFtime](https://ctftime.org/api/) | contest | `/api/v1/events/?start=&finish=&limit=`; `/api/v1/results/{year}/` | `ctftime` | ~6.5 a month; of 710 in 2025–2027, 140 onsite, ~39 in Europe, RS 0 | Team size only in description prose. Results are team placings; no member identities. Crawl-delay 10. [notes](19-parser-sources/ctftime.md) |
| [CASSINI](https://www.cassini.eu/hackathons) | hackathon | server-rendered Drupal, 13 pages, stable field CSS classes; `rss.xml` | `pages` | 2 editions a year × 10 countries; RS 0 | No render or LLM needed. Country pages are reused per edition and carry dates without year or time. Registration deadline only in news posts or on TAIKAI. Winners on `portfolio`. [notes](19-parser-sources/cassini.md) |
| [ICPC regionals](https://icpc.global/regionals/finder/SEERC-) | contest | public JSON `icpc.global/api/contest/public/*`; SEERC WordPress REST | `icpc` | SEERC 11–13 Dec 2026 (reg to 20 Nov), CERC 8–10 Jan 2027 | No render needed. Contest code year is the ICPC season. Published times are local despite `timezone: "UTC"`. Teams of 3 plus a coach. [notes](19-parser-sources/icpc.md) |
| [BBICC](https://bbicc.fon.bg.ac.rs/) | contest | WordPress REST (12 pages, `modified_after`); `/bbicc-2027/` has Event JSON-LD | `jsonld_pages` | 1 a year (7–13 Mar 2027) | Teams of four undergraduates plus an advisor; by invitation, so no registration link, deadline or prize. [notes](19-parser-sources/bbicc.md) |
| [dev.events](https://dev.events/EU/RS) | conference, meetup | `/EU?page=N` (30 per page), `/EU/RS`; `EducationEvent` JSON-LD | `jsonld_pages` | EU 518, RS 3 (+2 pinned online promotions) | Date-only. Regional RSS feeds do not exist; `rss.xml` is the 100 newest global listings. `/hackathons/EU/RS` ignores the country. [notes](19-parser-sources/dev-events.md) |
| [confs.tech](https://github.com/tech-conferences/conference-data) | conference | `conferences/<year>/<topic>.json`; change feed `commits/main.atom` | `confs_tech` | 2026: 412 unique (576 rows), 2027: 84; RS 1 each | MIT. Same event repeats across topic files. `online` + city = hybrid. No time or timezone. [notes](19-parser-sources/confs-tech.md) |
| [Luma](https://luma.com) | meetup, workshop | `api.lu.ma/calendar/get-items?calendar_api_id=…&period=future\|past`; `event/get?event_api_id=` | `luma` | Serbian calendars: Garaža (22 past), NS Tech Talks (2 past); 1 upcoming RS event | UTC plus IANA timezone. AI Salon Belgrade lives on the global `aisalon` calendar (filter by country); Claude AI Builders uses a host's personal calendar. Undocumented endpoint. [notes](19-parser-sources/luma.md) |
| [GDG Belgrade](https://gdg.community.dev/gdg-belgrade/) | conference, hackathon | chapter page `__NEXT_DATA__` (all upcoming); detail pages Event JSON-LD + `eventData` | `jsonld_pages` | RS: 3 upcoming across 2 chapters (Belgrade, University of Belgrade) | No `/api/`, render or dev.events needed. ETag works. Past events capped at 4 per chapter page. No Novi Sad chapter. Crawl-delay 2. [notes](19-parser-sources/gdg.md) |
| [@nstechtalks](https://t.me/s/nstechtalks) | meetup | Telegram web preview, `?before=<id>` paging | `telegram` | 40 live posts since 2025-10-31, ≈ 10 events | Russian. One event spans 4–5 posts; dates in prose without year, so LLM extraction anchored on post time. No ETag. [notes](19-parser-sources/telegram.md) |
## Tier 2

Useful but noisy, low volume, or costly to reach.

| source | kinds | access | adapter | volume | notes |
|---|---|---|---|---|---|
| [HackathonRadar](https://www.hackathonradar.com/discover/europe) | hackathon | sitemap (3,962 entries) → Event JSON-LD | `jsonld_sitemap` | 423 upcoming in Europe (self-reported) | Highest EU count; overlap with hackathonhub.eu unmeasured. `/discover/serbia` 404. ToS requires permission for bots; an OAuth MCP is offered. |
| [TAIKAI](https://taikai.network/en/hackathons) | hackathon | SSR listing → detail JSON-LD | `jsonld_pages` | ~9 upcoming, mostly EU | Registration milestones and participant counts. No API. ToS forbids extracting "a substantial part" of the data. |
| [DoraHacks](https://dorahacks.io/hub/hackathons) | hackathon | `/api/v1/hub/hackathons?page=&page_size=` | `dorahacks` | 846 total; sampled 43 % virtual, 7 % in-person | Registration deadline (`timeline_pre_register`), `winner_announced`. Answers only browser user agents (WAF). Heavy Web3 noise; geo mostly null. |
| [Kaggle](https://www.kaggle.com/competitions) | contest | official API `api.kaggle.com/v1/competitions.CompetitionApiService/ListCompetitions` with `KAGGLE_API_TOKEN` (Bearer); SDK `kaggle` | `kaggle` | 20 in `group=general` (6 featured, 3 playground, 11 getting-started) plus prize-bearing `group=community` ones | Verified 2026-11-15. `enabledDate` → `starts_at`, `deadline` → `ends_at`, `newEntrantDeadline` → `registration_deadline`, `maxTeamSize`, `reward` as text ("75,000 Usd"), `organizationName`, `tags`. `general` holds only active competitions, one page. `community` is mostly junk with far-future deadlines: keep `Usd` rewards only. Drop "Getting Started" and "Knowledge"/"Kudos"/"Swag" rewards. 429 after ~10 rapid calls. Global, online only. The anonymous internal endpoint `www.kaggle.com/api/i/competitions.CompetitionService/ListCompetitions` also works without a token and adds `totalCompetitors`, `numPrizes` and `rulesRequired`. |
| [Sessionize](https://sessionize.com/sitemap/events.xml) | conference | sitemap (11,114 URLs, no `lastmod`) → HTML; CFP iCal at `/add-to-calendar/cfs/<slug>` | `jsonld_sitemap` (LLM fallback) | RS: BSides Belgrade, Azure UG Serbia, Highload Serbia | Best CFP deadlines. No `lastmod`: crawl once, then only new URLs. |
| [itch.io jams](https://itch.io/jams) | contest | HTML listing, ISO UTC start and close | `itch_jams` | hundreds a month | Near-zero Serbian signal. Keep only ranked jams with prizes or known EU hosts. |
| [Global Game Jam](https://globalgamejam.org/jam-sites) | contest | Drupal behind Cloudflare | `pages`, `render = true` | 1 edition (25–31 Jan 2027), per-city sites | Only source of in-person game jams in Serbia. Plain HTTP gets 403. |
| [Startit.rs](https://startit.rs/wp-json/wp/v2/posts) | meetup | WP REST `/wp-json/wp/v2/posts?after=` | `feed` | ~10 event posts in 6 months | Serbian/English prose; most posts are news, so classification does the work. |
| [ICT Hub](https://www.icthub.rs) | hackathon, meetup | article sitemaps (sr/en/ru) | `jsonld_sitemap` (LLM fallback) | hackathon announcements in articles | No event pages. |
| Local conferences | conference | [Sinergija](https://sinergija.live/en/2026/sinergija-26) (SPA), [Heapcon](https://heapcon.io/2026), [BalCCon](https://2k26.balccon.org/), [ITkonekt](https://itkonekt.com) | `pages` (Sinergija `render = true`) | 1 a year each | Sinergija 25–26 Nov 2026. ITkonekt 2026 dates not found. |
| [dev.bg](https://dev.bg/events/) | meetup, conference | monthly calendar HTML | `pages` | 24 in Nov 2026 | Bulgaria. |
| [This Week in Rust](https://this-week-in-rust.org/rss.xml) | meetup | weekly RSS, "Upcoming Events" section | `feed` | ~50 a month incl. virtual | One feed item holds many events; extraction splits the list. |
| [DevOpsDays](https://github.com/devopsdays/devopsdays-web/tree/main/data/events) | conference | `data/events/<year>/<city>/main.yml` | `devopsdays` | 54 in 2026 | Apache-2.0, dates with UTC offsets. No Serbian edition. |
| [@xecut_bg](https://t.me/s/xecut_bg), [@SERBIAGAMEDEVEVENTS](https://t.me/s/SERBIAGAMEDEVEVENTS) | meetup, workshop | Telegram web preview | `telegram` | 59 posts (Apr–Nov 2026); 60 posts ≈ 55 events | Belgrade hackerspace, Russian; gamedev, English, silent since 2026-09-24. [notes](19-parser-sources/telegram.md) |

## People and results

`people` (§19.3) only takes identities the source publishes.

| source | what is published |
|---|---|
| Devpost | winners `ItemList` on the project gallery; project JSON-LD authors (Devpost usernames); GitHub repo links when present; member avatars carry GitHub user ids that `api.github.com/user/<id>` resolves to a login |
| CTFtime | per-event team placings and team names; no member identities |
| ICPC | top 3 schools and team counts only; team names on the SEERC site's per-edition `teams` page; no people |
| Kaggle | leaderboard view returns the top 20 (`teamId`, `teamName`, score, submission date), no members; full public CSV via leaderboard download (untested) |
| CASSINI | winner blocks per edition on `portfolio` (country via flag URL); European top 3 only in a prose post |
| BBICC | one winning person per edition as a testimonial on `/history/`; organiser names and e-mails on the contact page |

A GitHub login comes only from a GitHub URL the source links to that person or team, never from a matching username.

## Overlaps

Dedupe pairs to expect (§19.4.2): CASSINI ↔ TAIKAI; Junction, UniHack, HackEPS ↔ MLH ↔ Hackalendar; DevFest Belgrade ↔ GDG ↔ dev.events; Garaža ↔ Luma ↔ Hackathonist; SEERC ↔ ICPC API ↔ SEERC site; NS Tech Talks ↔ Luma ↔ Telegram; Heapcon ↔ confs.tech ↔ own site; hackathonhub.eu ↔ HackathonRadar ↔ Hackathonist.

## Dropped

| source | reason |
|---|---|
| Meetup.com | to revisit: blocked only by robots (feeds, `/gql`); group pages render no events without the GraphQL API |
| Eventbrite | search API shut down in 2019 |
| Ludum Dare | API returns 403 everywhere; ldjam.com is an empty SPA |
| clist.by | API needs a key; mirrors sources reachable directly |
| Codeforces, AtCoder, Meta Hacker Cup, Google coding competitions | individual contests, or discontinued |
| Zindi, AIcrowd, DrivenData | low volume, no Europe angle; AIcrowd lists no dates, Zindi is JS-only |
| Devfolio, Unstop, HackerEarth, Hack Club, BeMyApp | India- or US-centric, ~0 European events |
| hackathon.com, Hackster.io, allhackathons.com | 4 active events; hardware niche; far-future junk |
| Starfield Labs (ex lablab.ai), EU Datathon, papercall.io | no events; page 404; DNS dead |
| python.org calendars, Vukobratović Robotics Days | ~20 future events worldwide, Serbian ones historic; mostly school-age |
| HUB 201, NTP Belgrade, NTP Novi Sad, ict2go, srbija.events, @afisha_rs | dormant, no calendar, stale or noisy |
| Business Case Challenge, T.I.M.E.S. | no crawlable calendar; faculty news and Instagram only |
| Microsoft Reactor, JetBrains, AWS, OWASP, Women Techmakers, EU Code Week, EU Digital Skills & Jobs | JS-only with no data endpoint, no dates in markup, or blocked |
| Junction, HackZurich, HackYeah, HackTM, Datathon ETH sites | one event each, already covered by MLH and Hackalendar |

## Unverified

- hackathonhub.eu `events_public`: an undocumented API keyed by the site's public anon key; may change without notice.
- Luma: stability of the undocumented `get-items` endpoint; per-calendar iCal not found. Other Serbian calendars may exist; only those listed were found.
- Devpost: `winners_announced` can be set while the gallery has no winner markup.
- ICPC: CERC's WordPress moves domain each year; only the current edition is reachable.
- GDG `/api/` past-event paging: now allowed, but the `links.next` URL in the chapter payload points at an internal host; untested.
- Sessionize per-event speaker JSON (`api/v2/<id>/view/All`) untested.
- Global Game Jam 2027 Serbian sites (40 sites registered so far); ITkonekt 2026 dates; Sinergija agenda fields; EU Code Week data endpoint.
- `render = true` sources (Sinergija, Global Game Jam) need a headless browser in the worker image. No Tier 1 source needs one.
