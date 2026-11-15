# 19. Parser: sources and adapters

Survey of 2026-11-15. Report only: nothing here is implemented yet. Access methods, volumes and counts were tested on the survey date and will drift.

## Adapter design

Sources are hot-swappable: adding or removing a place never touches the pipeline (§19.4).

- **Adapter.** One module per adapter under `hmm/parser/sources/`, registered by name. It implements a small protocol: `discover` (refs to fetch), `fetch` (page or API payload) and `extract` (payload → `EventImport` list). An adapter that returns nothing from `extract` falls back to the generic extractor: schema.org Event JSON-LD first, then the LLM.
- **Instance.** A configured use of an adapter in `src/hmm/parser/sources.toml`. One adapter can back many instances (several Luma calendar lists, several Telegram channel lists). Secrets stay in the environment (§22); the TOML names the variable. A missing secret disables the instance with a log line, not a failed run.
- **Pipeline.** Classify, extract, normalize, dedupe, import and heartbeat are generic and never name a source. One worker job walks the instances that are due; a failing instance never stops the others (§19.4.7).
- **Identity.** The instance `id` is the `source_id` prefix (`luma-rs:evt-…`, `tg-rs:@nstechtalks/987`). It is stable: renaming it creates a new source.
- **Removal.** Delete the instance or set `enabled = false`. Its events stay in the catalog; tracked events whose prefix has no live instance are no longer revisited.
- **Generic adapters** make most additions a config line:

| adapter | input | covers |
|---|---|---|
| `jsonld_sitemap` | sitemap URL, URL pattern | hackathonhub.eu, HackathonRadar, Sessionize |
| `jsonld_pages` | listing or detail URLs, link pattern | Hackalendar, dev.events, TAIKAI, GDG |
| `pages` | seed URLs, optional `render = true` | CASSINI, BBICC, ICPC, local conferences (LLM extraction) |
| `luma` | calendar IDs, country filter | Belgrade and Novi Sad communities |
| `telegram` | channel names | `t.me/s/<channel>` previews |
| `feed` | RSS/Atom or WP REST URL | Startit.rs, This Week in Rust |

Source-specific adapters exist only where the payload has its own shape: `mlh`, `devpost`, `ctftime`, `kaggle`, `dorahacks`, `confs_tech`, `devopsdays`, `hackathonist`, `itch_jams`.

```toml
[[source]]
id = "hackathonhub"
adapter = "jsonld_sitemap"
interval = "12h"
rate = "30/min"
sitemap = "https://hackathonhub.eu/sitemap-events.xml"

[[source]]
id = "luma-rs"
adapter = "luma"
interval = "6h"
rate = "20/min"
calendars = ["cal-…", "cal-…"]
countries = ["RS"]
include_online = true

[[source]]
id = "tg-rs"
adapter = "telegram"
interval = "6h"
channels = ["nstechtalks", "xecut_bg", "SERBIAGAMEDEVEVENTS"]

[[source]]
id = "kaggle"
adapter = "kaggle"
interval = "24h"
secret_env = ["KAGGLE_USERNAME", "KAGGLE_KEY"]
```

## Tier 1

High relevance, structured or cheap to extract.

| source | kinds | access | adapter | volume | notes |
|---|---|---|---|---|---|
| [hackathonhub.eu](https://hackathonhub.eu/sitemap-events.xml) | hackathon | sitemap (1,231 URLs) → Event JSON-LD per page | `jsonld_sitemap` | RS 8, RO 23, HU 8, BG 7, HR/BA/SI 2 each | European backbone. robots allows all. Its Serbia theme page is near-empty, so discover through the sitemap. |
| [Hackathonist](https://raw.githubusercontent.com/zsevic/hackathonist/gh-pages/README.md) | hackathon | Markdown table on the `gh-pages` branch | `hackathonist` | 15 Serbian hackathons in 2026 | Hand-curated, MIT, updated 2026-11-13. Its organiser links (hakaton.fonis.rs, hack.garaza.org, ethbelgrade.rs) seed `pages`. |
| [MLH](https://mlh.io/events) | hackathon | `<script data-page="app">` JSON (`upcomingEvents`, `pastEvents`) | `mlh` | 2–4 EMEA a month | ISO dates, `formatType`, `region`, venue country. No prize or team size. `api.mlh.com` needs auth. |
| [Hackalendar](https://hackalendar.com) | hackathon | slugs on the homepage → `/e/{slug}` JSON-LD | `jsonld_pages` | ~20 curated, in-person Europe | robots allows bots by name. |
| [Devpost](https://devpost.com/api/hackathons?status%5B%5D=open&page=1) | hackathon | `/api/hackathons?status[]=open&page=N`; winners via `/software/search?query=` | `devpost` | 45 open, 36 online, ~0 EU in-person | The only source with winner lists: `winner`, `members` (Devpost usernames), and project pages that link GitHub repos. Dates are free text without a timezone. ToS forbids automated access. |
| [CTFtime](https://ctftime.org/api/) | contest | `/api/v1/events/?start=&finish=&limit=`; `/api/v1/results/{year}/` | `ctftime` | ~19 a month, ~5 onsite | `onsite`, `format`, `restrictions`, `prizes`, `participants`. No team size. Crawl-delay 10. API terms allow analysis use. |
| [CASSINI](https://cassini.eu/hackathons) | hackathon | Drupal HTML, one page per country per edition | `pages` | 2 editions a year × 10 countries (RO, BG, PL, UA…) | No JSON-LD. Also listed on TAIKAI; prefer this as authoritative. |
| [ICPC regionals](https://icpc.global/regionals/finder/SEERC-) | contest | finder is a JS app; [SEERC site](https://seerc.euc.icpc.global/home-2026/about) is WordPress | `pages`, `render = true` | SEERC 12–13 Dec 2026; also CERC | Serbian teams compete in SEERC. Teams of 3 plus a coach. Results on icpc.global. |
| [BBICC](https://bbicc.fon.bg.ac.rs/) | contest | WordPress HTML | `pages` | 1 a year (7–13 Mar 2027) | Belgrade student case competition, undergraduate teams. |
| [dev.events](https://dev.events/EU/RS) | conference, meetup | `/EU/RS`, `/EU` JSON-LD; `rss.xml` | `jsonld_pages` | RS 5, EU 518, online 144 | Best Serbian hit rate for conferences (DevFest Belgrade, GITEX AI Serbia). `/meetups/EU/RS` is mostly online noise. |
| [confs.tech](https://github.com/tech-conferences/conference-data) | conference | `conferences/<year>/<topic>.json`, 32 files | `confs_tech` | 567 in 2026, 193 online, RS 1 | MIT. `cfpUrl`, `cfpEndDate`, `online`. No timezone. |
| [Luma](https://luma.com) | meetup, workshop | `api.lu.ma/calendar/get-items?calendar_api_id=…&period=future` | `luma` | AI Salon Belgrade 17 upcoming, DSI 5 | UTC times plus IANA `timezone`, `geo_address_info.country_code`, cursor paging. No Belgrade discover page, so calendar IDs are curated. The endpoint is undocumented; the official API needs Luma Plus and covers only your own calendar. |
| [GDG Belgrade](https://gdg.community.dev/gdg-belgrade/) | conference, hackathon | detail pages carry Event JSON-LD; the listing loads through robots-disallowed `/api/` | `jsonld_pages` | ~5 a year (DevFest conference and hackathon) | Discover detail URLs via dev.events and seed URLs. Crawl-delay 2. Novi Sad chapter likewise. |
| [@nstechtalks](https://t.me/s/nstechtalks) | meetup | Telegram web preview | `telegram` | 12 posts, May–Nov 2026 | Russian. Also on Luma; dedupe. |

## Tier 2

Useful but noisy, low volume, or costly to reach.

| source | kinds | access | adapter | volume | notes |
|---|---|---|---|---|---|
| [HackathonRadar](https://www.hackathonradar.com/discover/europe) | hackathon | sitemap (3,962 entries) → Event JSON-LD | `jsonld_sitemap` | 423 upcoming in Europe (self-reported) | Highest EU count; overlap with hackathonhub.eu unmeasured. `/discover/serbia` 404. ToS requires permission for bots; an OAuth MCP is offered. |
| [TAIKAI](https://taikai.network/en/hackathons) | hackathon | SSR listing → detail JSON-LD | `jsonld_pages` | ~9 upcoming, mostly EU | Registration milestones and participant counts. No API. ToS forbids extracting "a substantial part" of the data. |
| [DoraHacks](https://dorahacks.io/hub/hackathons) | hackathon | `/api/v1/hub/hackathons?page=&page_size=` | `dorahacks` | 846 total; sampled 43 % virtual, 7 % in-person | Registration deadline (`timeline_pre_register`), `winner_announced`. Answers only browser user agents (WAF). Heavy Web3 noise; geo mostly null. |
| [Kaggle](https://www.kaggle.com/competitions) | contest | official API `/api/v1/competitions/list` with `KAGGLE_USERNAME`/`KAGGLE_KEY` | `kaggle` | a few cash competitions a quarter | `deadline`, `maxTeamSize`, `reward`, team-merger deadline. Global, online only. The anonymous internal endpoint stays off-limits. |
| [Sessionize](https://sessionize.com/sitemap/events.xml) | conference | sitemap (11,114 URLs, no `lastmod`) → HTML; CFP iCal at `/add-to-calendar/cfs/<slug>` | `jsonld_sitemap` (LLM fallback) | RS: BSides Belgrade, Azure UG Serbia, Highload Serbia | Best CFP deadlines. No `lastmod`: crawl once, then only new URLs. |
| [itch.io jams](https://itch.io/jams) | contest | HTML listing, ISO UTC start and close | `itch_jams` | hundreds a month | Near-zero Serbian signal. Keep only ranked jams with prizes or known EU hosts. |
| [Global Game Jam](https://globalgamejam.org/jam-sites) | contest | Drupal behind Cloudflare | `pages`, `render = true` | 1 edition (25–31 Jan 2027), per-city sites | Only source of in-person game jams in Serbia. Plain HTTP gets 403. |
| [Startit.rs](https://startit.rs/wp-json/wp/v2/posts) | meetup | WP REST `/wp-json/wp/v2/posts?after=` | `feed` | ~10 event posts in 6 months | Serbian/English prose; most posts are news, so classification does the work. |
| [ICT Hub](https://www.icthub.rs) | hackathon, meetup | article sitemaps (sr/en/ru) | `jsonld_sitemap` (LLM fallback) | hackathon announcements in articles | No event pages. |
| Local conferences | conference | [Sinergija](https://sinergija.live/en/2026/sinergija-26) (SPA), [Heapcon](https://heapcon.io/2026), [BalCCon](https://2k26.balccon.org/), [ITkonekt](https://itkonekt.com) | `pages` (Sinergija `render = true`) | 1 a year each | Sinergija 25–26 Nov 2026. ITkonekt 2026 dates not found. |
| [dev.bg](https://dev.bg/events/) | meetup, conference | monthly calendar HTML | `pages` | 24 in Nov 2026 | Bulgaria. |
| [This Week in Rust](https://this-week-in-rust.org/rss.xml) | meetup | weekly RSS, "Upcoming Events" section | `feed` | ~50 a month incl. virtual | One feed item holds many events; extraction splits the list. |
| [DevOpsDays](https://github.com/devopsdays/devopsdays-web/tree/main/data/events) | conference | `data/events/<year>/<city>/main.yml` | `devopsdays` | 54 in 2026 | Apache-2.0, dates with UTC offsets. No Serbian edition. |
| [@xecut_bg](https://t.me/s/xecut_bg), [@SERBIAGAMEDEVEVENTS](https://t.me/s/SERBIAGAMEDEVEVENTS) | meetup, workshop | Telegram web preview | `telegram` | 19 and 20 posts | Russian and English. |

## People and results

`people` (§19.3) only takes identities the source publishes.

| source | what is published |
|---|---|
| Devpost | winning projects, member usernames, GitHub repo links on project pages |
| CTFtime | per-event team placings, team names |
| ICPC | team lists and results per contest |
| Kaggle | leaderboards with usernames and team names |
| CASSINI, BBICC | finalists and winners in news posts |

A GitHub login comes only from a GitHub URL the source links to that person or team, never from a matching username.

## Overlaps

Dedupe pairs to expect (§19.4.2): CASSINI ↔ TAIKAI; Junction, UniHack, HackEPS ↔ MLH ↔ Hackalendar; DevFest Belgrade ↔ GDG ↔ dev.events; NS Tech Talks ↔ Luma ↔ Telegram; Heapcon ↔ confs.tech ↔ own site; hackathonhub.eu ↔ HackathonRadar ↔ Hackathonist.

## Dropped

| source | reason |
|---|---|
| Meetup.com, Eventbrite | robots blocks feeds and search; Meetup API is Pro-only; Eventbrite search API shut down in 2019 |
| Ludum Dare | API returns 403 everywhere; ldjam.com is an empty SPA |
| clist.by | robots disallows `/api/`; mirrors sources reachable directly |
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

- hackathonhub.eu: 8 Serbian events in the sitemap against a near-empty Serbia page.
- GDG: whether dev.events and seed URLs find every chapter event without the listing API.
- Luma: stability of the undocumented `get-items` endpoint; per-calendar iCal not found.
- Sessionize per-event speaker JSON (`api/v2/<id>/view/All`) untested.
- Global Game Jam 2027 Serbian sites (40 sites registered so far); ITkonekt 2026 dates; Sinergija agenda fields; EU Code Week data endpoint.
- `render = true` sources (ICPC finder, Sinergija, Global Game Jam) need a headless browser in the worker image.
