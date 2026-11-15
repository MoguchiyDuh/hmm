# CASSINI Hackathons

Probed 2026-11-15. Adapter: `pages`. Kinds: hackathon.

EUSPA-funded hackathon series. Each **edition** (12th runs 27–29 Nov 2026) is held simultaneously in **10 country locations**, each a separately organised physical event with its own organiser, venue and local prizes, plus a European Final (Demo Day). The site is one Drupal 11 site; there is no JSON-LD, no JSON:API and no sitemap, but every page carries stable Drupal field CSS classes, so extraction is CSS-driven, not LLM-driven.

`hackathons.cassini.eu` (the old Drupal 9 site) now serves only a meta refresh to `cassini.eu/hackathons`; do not crawl it.

## Access

All requests `GET`, no auth, no cookies, `Accept-Encoding: gzip` → 5× smaller.

| URL | status | bytes | notes |
|---|---|---|---|
| `https://www.cassini.eu/robots.txt` | 200 | 2,141 | `Last-Modified: Tue, 04 Aug 2026 07:51:09 GMT`, `Cache-Control: max-age=31536000`. Only Drupal paths (`/core/`, `/profiles/`, `/admin/`, `/search/`, `/user/*`) are disallowed. **No `Sitemap:` line.** `cassini.eu` → 301 to `www.cassini.eu` |
| `https://www.cassini.eu/hackathons` | 301 | — | → `/hackathons/` (trailing slash required) |
| `https://www.cassini.eu/hackathons/` | 200 | 74,641 (15,154 gz) | index: edition theme, challenges, agenda, 10 location cards. TTFB 0.52 s |
| `/hackathons/{country}` | 200 | 48–105 KB | 10 pages: `bulgaria`, `czechrepublic`, `Finland`, `germany`, `ireland`, `italy`, `lithuania`, `poland`, `romania`, `ukraine` (note capital `F` in `Finland`) |
| `/hackathons/portfolio` | 200 | 219,950 (27,850 gz) | "Winners": all editions, all local winners, 2021–2026 |
| `/hackathons/big-ideas-campaign` | 200 | 59,289 | pre-hackathon webinar series with dates/times |
| `/hackathons/rss.xml` | 200 | 9,877 | 10 latest news nodes |
| `/hackathons/jsonapi`, `/sitemap.xml`, `/hackathons/sitemap.xml`, `/sitemap_index.xml` | **404** | — | no JSON:API, no sitemap anywhere |
| `/hackathons/romania?_format=json` | **406** | 98 | `{"message":"A route that returns a rendered array as its response only supports the HTML format."}` — REST serialisation is off for rendered routes |
| `/hackathons/{old-country}` e.g. `austria`, `greece`, `netherlands` | **301** | — | redirect module (`X-Redirect-ID`) maps retired country slugs onto a *different current country*: `austria`→`czechrepublic`, `greece`→`poland`, `netherlands`→`romania`. `spain`, `hungary`, `serbia` → 404 |

No `ETag`/`Last-Modified` on any HTML page (`Cache-Control: must-revalidate, no-cache, private`, `X-Drupal-Dynamic-Cache: UNCACHEABLE`) → conditional GET is useless, every check is a full 13-page refetch (≈1.4 MB, ≈15 s at 1 req/s). Content is "© EU 2021-2025"; reuse of page text for a catalogue entry is the normal reading of the site's own disclaimer pages (no scraping ban stated).

## Discovery

There is no listing endpoint. Enumerate from the two nav menus, which are identical on every page:

- **countries** — `/hackathons/` location cards + the "Locations" nav submenu. Their `data-drupal-link-system-path` gives the **node id**, stable per country: Germany 74, Lithuania 76, Romania 77, Ireland 78, Ukraine 79, Bulgaria 80, Czech Republic 81, Poland 82, Finland 83, Italy 84. Portfolio 30, Big Ideas Campaign 65, Tools 104.
- **editions** — a new edition does not get a new URL: the same 10 country nodes are **overwritten** each edition (Romania has shown the 11th and now the 12th). The edition is identified only by the page `<title>` (`12th CASSINI Hackathon | Romania | EU Space for Peace and Resilience`) and the body text.
- **new-edition announcement** — a news node appears first: `/hackathons/rss.xml` (`pubDate`, 10 items, no `lastBuildDate`) is the cheapest change signal. The 12th edition post is `node/105` (created `2026-09-28T19:18:34+02:00`); it is the only page that states the full dates *with year* and the registration deadline.
- Nothing is filterable; there is no upcoming/past split — `portfolio` is the only historical view and it holds winners only.

Counts on the probe date: **10 local events** (one edition) + 1 European final (Demo Day, 2 Dec 2026) + 1 webinar series (10 sessions, 2–13 Nov 2026, all finished).

## Fields

Country page, one `<div class="field field--name-…">` per fact:

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| node id / URL | `node/77`, `/hackathons/romania` | `source_id` | `cassini:node/77`. **Not edition-stable** — add the edition number (`cassini:node/77:12`) since the node is reused each edition |
| `<title>` | `12th CASSINI Hackathon \| Romania \| EU Space for Peace and Resilience` | `title` | edition number + country + theme, pipe-separated |
| `field--name-field-hackathon-date` | `27 - 29 November` | `starts_at`, `ends_at` | **no year, no times.** Identical on all 10 pages. Year only from the index (`27-29 November 2026`, `field-hero-caption`) or `node/105` (`Hackathon Weekend: 27 – 29 November 2026`) |
| `field--name-field-hackathon-location` | `Bucharest`; `Adgar Park West, Al. Jerozolimskie 181B, 02-222 Warszawa`; `Ivano-Frankivsk, Kyiv` | `location.city`, `location.address` | free text: city only for 8 of 10, full street address for Poland, two cities for Ukraine. Country comes from the page, not the field |
| `field--name-field-hackathon-type` | `On-site` | `format` | all 10 are `On-site` on the probe date; field is free text (`Online`/`Hybrid` presumably possible — unverified) |
| `field--name-body` | 2–4 paragraphs | `description_md` | theme, free registration, what participants get. Contains the local prize table on the country page, as prose |
| `field--name-field-hck-register-button` + the fixed "Register Now" header link | `https://taikai.network/cassinihackathons/hackathons/space-for-peace-resilience` | `registration_url` | same TAIKAI URL for all 10 locations; the location is chosen inside TAIKAI |
| — | — | `registration_deadline` | **not on the country pages.** Only in `node/105`: `Registration Deadline: 20 November 2026`. Rules of Contest PDF §5 gives `Deadline for the registration to Stage 1` per edition (13/09/2024, 22/11/2024, 28/03/2025, 26/11/2025 for editions 7–10) |
| local prize prose | `1st place: €1.000`, `2nd: €500`, `3rd: €250` (BG); `1st place: €2.000` (RO) | `prize` | only the 1st place is stated for some countries; central prizes `€5,000 / 3,000 / 1,000` + 6 months mentoring are the same everywhere (also on the index) |
| organiser card | `Organised by: Risk Space Transfer - Technology Transfer Office` + `Sofia, Bulgaria` | `organizer` | name + city in prose; the organiser's own URL is in `field--name-field-organiser-link` |
| mentor cards `field--name-field-hackathon-team` / `-team-1` | 9–15 persons with `field--name-field-name`, `field--name-field-company`, `field--name-field-linkedin` | `people` (speaker/mentor) | **LinkedIn URLs, not GitHub.** Despite the field name, this is the mentors grid, not a team-size field |
| local agenda `field--name-field-agenda-day-date` + `field--name-field-time` | `Thursday November 5`, `16:00 - 17:00 CET` | — | Big Ideas webinars; some carry `CET`, some only a start time |
| central agenda (index) `field-agenda-day-date` + `field-time` | `Friday November 27`, `18:00`, `13:30`, `15:00` | — | **times have no timezone anywhere in the markup** — CET is only implied (`node/105` and the webinar fields say CET) |
| rules PDF | 17 pp, 347,994 B | `eligibility`, `team_size_*` | 18+ natural persons, EU/Horizon-Europe-associated countries, teams or individuals, one submission per participant, one team per person. **No team-size cap stated.** Sign-up as an individual is expected ("we'll connect you with professionals to form a team") |
| — | — | `timezone`, `language`, `themes`, `difficulty` | never published. `language` is `en` throughout; `timezone` must be inferred per city (`Europe/Bucharest`, `Europe/Sofia`, `Europe/Helsinki`, `Europe/Berlin`, `Europe/Dublin`, `Europe/Rome`, `Europe/Vilnius`, `Europe/Warsaw`, `Europe/Kyiv`) |

Winners (`/hackathons/portfolio`), one `<section class="winners winners--<theme>-<year>">` per edition, 11 sections, **109 winner paragraphs**:

| source field | example | maps to | notes |
|---|---|---|---|
| section class / `field--name-field-winners-heading-*` | `winners--water-2026`, `EU Space for Water 2026` | edition, `title` suffix | years present: 2026 water, 2025 consumer + healthcare, 2024 defence + green transition, 2023 aid + defence, 2022 financial + (re)visit, 2021 arctic + green spaces |
| `field--name-field-winner-name` | `Viadrus AI` | `people[].display_name` | team name, **not a person** |
| `field--name-field-winner-country` | `<img src="https://flagcdn.com/h24/pl.png">` | `people[].place` | country code is only in the flag URL (`pl`, `es`, `at`, …, `xk` for Kosovo, `nc` New Caledonia) |
| `field--name-field-winner-short-description` | `Winner Poland` | — | redundant with the flag |
| `field--name-field-winner-description` | 1–3 sentences + `Watch their pitch!` → `vimeo.com/1190219975` | `summary`, `urls` | also links the TAIKAI project (`…/projects/<id>/idea`) |

The **European top 3** is not marked on the portfolio page. It appears only in the winners news node (`node/100` for the 10th: `1st Place: Avalanche Detectors (Bulgaria)`, `2nd TrailRadar (Finland)`, `3rd GlideBuddy (Italy)`), i.e. as prose, and only for editions that got a write-up.

## Revisits and identity

- Stable ids: `cassini:node/<id>` per country location; `cassini:portfolio:<theme>-<year>:<winner-slug-or-name>` for winners; `cassini:node/105` for edition metadata/news.
- Because the country node is reused, the stored `starts_at` changes every ~6 months and the same `source_id` must flip from announced to finished. Compare `starts_at` + `title` and treat a changed edition number as an update of the same row, or key on `node/<id>:<edition>` — the latter avoids resurrecting last year's date on the current page.
- No `ETag`/`Last-Modified`; no `max-age` on HTML. All pages are `private, no-cache`. Refetch all 13 pages, or the RSS first (10 items, cheap) and only then the country pages once a new edition post appears.
- A finished event: the country page keeps saying `27 - 29 November` (no year), the "Register Now" TAIKAI link 404s/redirects, and the portfolio gains that edition's section. `node/105` stays as-is.
- Results/winners: portfolio (all local winners, team names + country + pitch video) and the winners news node (European ranking). Organiser and mentor names are published with LinkedIn links. **No GitHub logins, no participant/attendee lists, no per-person placements.**

## Rate and failure behavior

- No `Crawl-delay` in robots.txt, no rate-limit headers observed. ~35 sequential requests at 1.2 s spacing over ~20 min: no 429, no blocks, no challenge page.
- Latency 0.48–0.52 s TTFB per HTML page (uncached Drupal render); API/JSON endpoints do not exist.
- 404 returns a full 10–17 KB Drupal error page, `X-Drupal-Dynamic-Cache: UNCACHEABLE (404, sub-request: HIT)`.
- Occasional header-level quirk: `curl -D -` on a redirect chain aborts with `curl: (23) Failed writing headers` — cosmetic, the 301 is served correctly.
- Country slugs are case-sensitive: `/hackathons/finland` → 301 `/hackathons/Finland`; use the nav link as canonical.

## Volume and relevance

| relevance | count today |
|---|---|
| Serbia (in-country) | **0** — no Serbian location in the 12th edition (Serbia is not in the country menu; `serbia` 404s) |
| Europe, in person | 10 locations: Espoo FI, Gabrovo BG, Prague CZ, Darmstadt DE, Dublin IE, Turin IT, Kaunas LT, Warsaw PL, Bucharest RO, Ivano-Frankivsk/Kyiv UA + 1 online Demo Day |
| Online | 1 (Demo Day 2 Dec 2026) + 10 finished webinars |

Noise is essentially zero: every country page is one hackathon. The closest events to Serbia are Gabrovo (BG) and Bucharest (RO). Volume per year: 10 + 1, roughly 2 editions per year (7th–10th in 2024–2025, 11th in Mar 2026, 12th in Nov 2026).

## Quirks

- "The 12th CASSINI Hackathon **took place** simultaneously across 10 different locations on 27-29 November 2026" — the index already uses past tense three weeks before the event. Do not trust tense as a status signal.
- Country `field-hackathon-date` has no year at all; if the parser stores it naively it produces a date in the current year and silently mis-dates next year's edition.
- The registration deadline (20 Nov 2026) exists only in a news node, and that node also says "All participants need to register individually, even if in the same team".
- Rules of Contest PDF is the 2024 edition covering hackathons 7–11; no 12th-edition rules document is linked (only a TAIKAI platform PDF from 2025-03).
- The Big Ideas page says Romania's 5 Nov session has a "Recording available", while `romania` still shows `Register here` for that same session — per-page drift.
- Winners' countries include non-European ones (New Caledonia, Kosovo) and country flags are only in `flagcdn.com` URLs.
- Retired country slugs 301 to *unrelated* countries, so following redirects on old bookmarks yields wrong data.
- `field--name-field-hackathon-team` is the mentors grid despite the name; nothing on the site states a team-size limit.
- Agenda times carry no timezone in the central agenda; only the webinar fields say `CET`.

## Config sketch

```toml
[[source]]
id          = "cassini"
adapter     = "pages"
interval    = "24h"
rate        = "20/min"
seed        = ["https://www.cassini.eu/hackathons/"]
news_feed   = "https://www.cassini.eu/hackathons/rss.xml"
winners     = "https://www.cassini.eu/hackathons/portfolio"
render      = false
country_urls = [
  "https://www.cassini.eu/hackathons/bulgaria",
  "https://www.cassini.eu/hackathons/czechrepublic",
  "https://www.cassini.eu/hackathons/Finland",
  "https://www.cassini.eu/hackathons/germany",
  "https://www.cassini.eu/hackathons/ireland",
  "https://www.cassini.eu/hackathons/italy",
  "https://www.cassini.eu/hackathons/lithuania",
  "https://www.cassini.eu/hackathons/poland",
  "https://www.cassini.eu/hackathons/romania",
  "https://www.cassini.eu/hackathons/ukraine",
]
default_tz  = "Europe/Belgrade"
```
