# dev.events

Probed 2026-11-15. Adapter: `jsonld_pages`. Kinds: conference, meetup (hackathon section exists but is
useless, see below).

AdonisJS server-rendered site behind Cloudflare (session cookie `adonis-session`), one JSON-LD
`EducationEvent` per row on every listing page. Covers conferences, meetups and hackathons with the
same machinery. Nothing is JS-rendered; paging is htmx (`?page=N`).

## Access

| URL | method | status | size | notes |
|---|---|---|---|---|
| `/robots.txt` | GET | 200 | 109 B | `*`: `Allow: /`; only `meta-externalagent`, `meta-webindexer` blocked. No `Crawl-delay`, no `Sitemap:` line |
| `/sitemap.xml` | GET | 200 | 606,676 B | 6,041 URLs, exists despite robots not advertising it |
| `/EU` | GET | 200 | 125,768 B | 30 events + 2 pinned; "showing 30 out of 518 conferences" |
| `/EU?page=N` | GET | 200 | 124–71 KB | N = 2…18; page 18 has 8 rows, page 19 has 0 |
| `/EU/RS` | GET | 200 | 46,244 B | 5 events (3 Serbian + 2 online) |
| `/EU/RS/Belgrade` | GET | 200 | 46,717 B | same 5 |
| `/meetups/EU/RS` | GET | 200 | 59,737 B | sidebar Europe 13 / Online 10; 13 rows, 3 Serbian |
| `/hackathons/EU/RS` | GET | 200 | 128,332 B | **47 rows, none Serbian** (see Quirks) |
| `/ON` | GET | 200 | 103,155 B | 144 online conferences, 30/page |
| `/conferences/<slug>-<hash>` | GET | 200 | 8.4–9.0 KB | one `EducationEvent` JSON-LD, identical to the listing row |
| `/rss.xml` | GET | 200 | 106,033 B | `text/xml`, 100 items, global |
| `/EU/rss.xml`, `/EU/RS/rss.xml` | GET | 200 | 147,589 B | **not RSS** — returns HTML (`text/html`), the filter is ignored |

Headers: no `etag`, no `last-modified`, no `cache-control` on any HTML; `cf-cache-status: DYNAMIC`;
server sets three cookies (`sorting`, `adonis-session`, a session id) that are not needed. HTTP/2,
`vary: Accept-Encoding`.

## Discovery

- **Paging is server-side and complete**: `?page=N`, **30 rows/page**, `hx-vals='{"page": "2"}'` on
  the "Show more" button, same URL otherwise. `/EU` = 518 conferences → pages 1–18 (17×30 + 8).
  Filters compose with paging because the htmx target is the current URL.
- Regional: `/EU/<CC>` (**37** country codes linked from `/EU`), `/EU/<CC>/<City>` (city slugs are the
  Latin transliteration: `Belgrade`, `Mostoles`), topic: `/EU/<topic>`, and both combined
  (`/EU/DE/Berlin/ai`). A separate `/ON` region holds online-only events (144).
- Region totals are in the sidebar of every listing, cheap to scrape for a coverage check: Asia 155,
  Africa 77, Europe 518, North America 209, South America 30, Oceania 27, Online 144; topics e.g.
  AI 144, Cloud 99, Big Data 110, Cybersecurity 53, Data/Database 112, .NET 14, Java 24.
- Sort: `?sorting=upcomingEvents` (default, start date ascending from today) and
  `?sorting=newestFirst` (submission order, dates unordered — verified: page 1 jumps 2026-12-03 →
  2027-02-05 → 2026-11-20).
- **`sitemap.xml` is useless for events**: 6,041 URLs, `lastmod` is the same build timestamp
  (`2026-11-15T00:00:00.000Z`) on all 6,041, and it contains **0** `/conferences/` URLs — only
  filter pages (`/EU` 2,269, `/NA` 1,248, `/AS` 951, `/AF` 497, `/meetups` 403, `/SA` 239, `/OC` 162,
  `/hackathons` 134, `/ON` 56 + topic roots like `/java`, `/ON/java`). No detail-URL enumeration.
- **`rss.xml` is not a discovery path for region work**: 100 items = the 100 newest *listings*
  (global, no region filter — `/EU/rss.xml` and `/EU/RS/rss.xml` silently return the HTML page).
  `pubDate` is when the event was added (`Clean Architecture Masterclass`: pubDate 2023-02-06, event
  2026-12-10). Zero Serbian events in the 100; the 3 Serbian entries were listed long ago.
  Categories per item: `conference`/`meetup`/`hackathon` plus topic tags (`AI`, `Fintech`, `Google`).
- Past events: **not reachable**. Every listing starts at today (`Solana Breakpoint 2026`,
  `DevFest Catania 2026` on 2026-11-15 are the first real rows), and no archive page or `past`
  parameter was found; the sitemap has no event URLs. Whether individual `/conferences/<slug>` pages
  of finished events still resolve is **unverified**.

Volume: 518 European conferences upcoming (18 pages), 144 online conferences, 5 European hackathons
+ 47 online hackathons, meetups: Europe 13 / Online 10 on the Serbia page.

## Fields

Listing JSON-LD and detail-page JSON-LD are byte-identical in structure — the detail page adds
nothing, and its visible HTML is a stub ("preview is not available … Visit conference's website
directly") plus one external link to the organiser.

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| URL path `/conferences/<slug>-<hash>` | `dev-fest-belgrade-2026-tpzpweng` | `source_id` | `dev-events:<slug>-<hash>`; trailing hash is the real id, the readable part changes on rename |
| `name` | `DevFest Belgrade 2026` | `title` | |
| `startDate` | `2026-12-03T00:00:00.000+00:00` | `starts_at` | always midnight UTC — **date-only, no time ever published** |
| `endDate` | `2026-12-04T00:00:00.000+00:00` | `ends_at` | inclusive last day at 00:00, so `ends_at` is the last date not the end instant |
| `eventAttendanceMode` | `OfflineEventAttendanceMode` / `OnlineEventAttendanceMode` | `format` | online events carry **no `location` at all**; hybrid never seen |
| `location.address.addressLocality` + `addressRegion` | `Belgrade` / `Serbia` | `location.city`, `location.country` | **country name in `addressRegion`, no `addressCountry`**; also duplicated in `Place.name` (`"Belgrade, Serbia"`); no street, no coordinates |
| `description` | `"Google conference in Belgrade, Serbia"`, `"Testing / QA conference in Belgrade, Serbia"` | `summary` | auto-generated from `<topic> <kind> in <city>, <country>` — 4–8 words, so `description_md` stays empty unless an LLM writes it |
| `performer.name` | `DevFest Belgrade 2026` | `organizer` | equals the title, not an organisation; the real organiser is only an external link on the detail page (`gdg.community.dev/...`) — not in JSON-LD |
| `image[]` | Cloudinary OG render | — | generated, not the organiser's artwork |
| `url` | canonical `/conferences/...` | `website_url` | detail page links out to the organiser once, as a plain anchor |
| — | — | `registration_url`, `registration_deadline`, `prize`, `team_size_*`, `language`, `eligibility`, `timezone`, `themes` | **none**. Topic only exists in the filter URL and the description prefix |
| `@type` | `EducationEvent` (not `Event`) | — | every row, meetups and hackathons included; a generic `Event`-only extractor misses all of it |

## Revisits and identity

- Identity: `dev-events:<slug>-<hash>` from the URL. The 8-char suffix is stable; the readable prefix
  is regenerated on rename.
- No `etag`/`Last-Modified` and no cache validators: revalidation is impossible, every poll is a full
  fetch. Budget ≈18 pages for `/EU`, plus per-country pages when filtering.
- Change detection must be diff-based after fetching listings, or per-page `?page=N` walks with a
  stored checksum. The site's own data has no timestamps exposed.
- Cancellation: every row inspected carries `EventScheduled`; a cancelled entry presumably just
  disappears from the listing. There is no results or winner data.
- Finished events drop out of every listing silently, so vanishing from a listing is the "finished"
  signal; whether the detail page then 404s is unverified. Do not turn it into `cancelled` without a
  second source.

## Rate and failure behavior

- 18 requests at ≥1 s spacing: all 200, ~0.2–0.5 s, no 429, no Cloudflare challenge, no rate-limit
  headers. `cf-cache-status: DYNAMIC` everywhere, so Cloudflare is proxying but not caching.
- Unknown paths are handled by the app, not 404: `/EU/rss.xml` and `/EU/RS/rss.xml` answer 200 with
  the HTML listing. Do not use status codes to detect an unsupported filter — check `content-type`.
- Missing event page: unverified.

## Volume and relevance

| scope | count |
|---|---|
| Europe, all conferences | 518 |
| Online, all conferences | 144 |
| Serbia `/EU/RS` | 5 rows = 3 Serbian + 2 online |
| Serbia, actual Serbian events | 3: `DevFest Belgrade 2026` (3–4 Dec 2026), `UCAAT 2027` (20–22 Apr 2027), `GITEX AI SERBIA 2027` (26–27 May 2027) |
| `/EU/RS/Belgrade` | the same 5 (online rows are injected into every location page) |
| `/meetups/EU/RS` | 13 rows, 3 Serbian; sidebar "Online 10", the rest foreign or online (Angular en español, AWS Discord, HashiCorp Hungary) |
| `/hackathons/EU/RS` | 47 rows, **0 Serbian** |
| EU hackathons total | 5 (Europe) + 47 (online) |
| RS in sitemap | `/EU/RS` present, no event URLs |

Serbia gets 3 conferences — the best *conference* hit rate of the surveyed sources,
but 2 of the 3 are 2027. The two online masterclasses are permanent
promotions that appear on every country and city page, so any country page understates its own
coverage by two and any "online" count is inflated by them. For Serbia the source is worth those 3
rows plus a periodic sweep of `/EU/<CC>` for regional conferences worth a trip from Belgrade.

## Quirks

- Country pages include the two pinned online masterclasses (`The Principal Dev – Masterclass`,
  `Clean Architecture Masterclass`) in a `<div class="featured">` "Best developer
  conferences in Europe" block that repeats on **every** listing page — `/EU` page 1, 2 and 18,
  `/EU/RS`, `/EU/RS/Belgrade`, `/ON`. They have no `/conferences/` anchor, so they are not rows but
  they are JSON-LD blocks: naive per-country diffs duplicate them 40× and inflate every count.
- Every listing page therefore has `rows + 2` `EducationEvent` blocks: `/EU` 32 blocks / 30 anchors,
  page 18 10/8, `/EU/RS` 5/3, `/hackathons/EU/RS` 49/47. Count rows by anchors, not by JSON-LD blocks.
- `/hackathons/EU/RS` ignores the country: it returns the 47 *online* hackathons while the sidebar
  reports "Europe 5". Online events match every country filter, and for hackathons the online set
  dominates. Never read the hackathon section as regional.
- `@type` is `EducationEvent`, not `Event`; a strict `schema.org/Event` matcher returns nothing.
- `addressRegion` holds the **country** (`Serbia`), and `addressCountry` is absent; `Place.name`
  repeats `city, country`.
- Dates are date-only at `T00:00:00.000+00:00`: a 3–4 Dec conference becomes
  `starts_at 2026-12-03T00:00Z`, `ends_at 2026-12-04T00:00Z`, which is *not* the end of the event.
  No timezone is published anywhere; pick the city's IANA zone from `location.city`.
- `description` is machine-generated and tautological (`"Google conference in Belgrade, Serbia"`) —
  it is a classifier hint, not a summary; cap `confidence` for it.
- `performer.name == name`, so `organizer` is not recoverable from JSON-LD; only the single outbound
  link on the detail page names the organiser.
- RSS `pubDate` is the listing date, years before the event date; sorting by it is meaningless.
- `/EU/rss.xml` and `/EU/RS/rss.xml` return the HTML page with status 200 — a "does this feed exist"
  check by status code lies.
- Filter pages and topic pages are in the sitemap (6,041 of them, all with identical `lastmod`),
  event pages are not — the sitemap is a map of filters, nothing else.

## Config sketch

```toml
[[source]]
id          = "dev-events"
adapter     = "jsonld_pages"
interval    = "24h"
rate        = "20/min"
listing     = "https://dev.events/EU"
pagination  = "query:page"
page_size   = 30
regions     = ["/EU", "/EU/RS", "/EU/BG", "/EU/RO", "/EU/HU", "/EU/HR", "/EU/BA", "/EU/SI", "/EU/MK", "/EU/ME", "/ON"]
meetups     = ["/meetups/EU", "/meetups/EU/RS"]
skip        = ["/hackathons"]
timezone_by_city = true
min_confidence = 0.5
```
