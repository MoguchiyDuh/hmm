# MLH (Major League Hacking)

Probed 2026-11-15. Adapter: `mlh`. Kinds: hackathon.

## Access

| what | observed |
|---|---|
| host | `mlh.io` 302 → `www.mlh.com`; `/events` 302 → `/events` → `/seasons/2027/events/` (2 hops, final 200) |
| listing | `GET https://www.mlh.com/seasons/2027/events` — HTML 247 KB, payload in `<script data-page="app" type="application/json">` (44 KB, Inertia.js page object) |
| detail | `GET /events/<slug>` (`Events/Show`, 74–185 KB) and `/events/<slug>/prizes` (`Events/Prizes/Index`) |
| api | `GET https://api.mlh.com/v4/events?season=2027` → 401 `{"status":401,"error":"Unauthorized"}`. `props.apiBaseUrl` points there |
| sitemap | `https://www.mlh.com/sitemap.xml` → `sitemaps/events-{1..5}.xml`, 10,000 URLs each, every URL with `lastmod` |
| robots | `www.mlh.com/robots.txt`: `Allow: /`, disallows `/account/ /tools/ /v4/ /auth/ /admin/ /graphql`. `/events/` and the season pages are allowed |
| headers | Cloudflare + Heroku. `cache-control: max-age=0, public, stale-while-revalidate=300, s-maxage=60`. No `ETag`, no `Last-Modified` on the HTML |
| JSON-LD | none. 0 `application/ld+json` blocks on listing, event page and prizes page |

No ToS page was consulted beyond robots.txt.

## Discovery

The listing is the whole season, split into two independently paginated lists in the same payload:

| list | key | page param | pages | total seen |
|---|---|---|---|---|
| upcoming | `props.upcomingEvents` | `?upcoming_page=N` | 1–2 (24 each) | 48 |
| past | `props.pastEvents` | `?page=N` | 1–4 (24/24/24/13) | 85 |

Paging only works with the Inertia headers:

```
GET /seasons/2027/events?upcoming_page=2
X-Inertia: true
X-Inertia-Version: <page "version" value, e.g. 233dea28baaf5ec514a3bd1f3042198400fa2164>
X-Requested-With: XMLHttpRequest
→ 200, application/json, 43 KB (props only, same shape)
```

- Without `X-Inertia` the server ignores the param and returns the same page 1 HTML for every `?page=`/`?upcoming_page=` value (verified pages 2, 3, 5).
- Wrong or missing `X-Inertia-Version` → **409 with an empty body**. Read `version` from the last HTML page load (changes per deploy).
- `?page=2` alone moves `pastEvents` to page 2 and leaves `upcomingEvents` on page 1; `?upcoming_page=2` moves only `upcomingEvents`.
- Season URL is the only discovery dimension; `/seasons/2028/events` → 404. `/seasons/2026/events` and `/seasons/2025/events` return `upcomingEvents: []` and `pastEvents: [24 …]` (page 1 only).
- `sitemaps/events-*.xml` covers the archive of every `/events/<slug>` ever created (test events, peer-group calls, non-hackathons included) with `lastmod`, so it is a change feed for tracked items but a bad discovery feed.
- No search/filter API: `props.filters` (`region`, `month`, `search`, `place`, `lat/lng`, `diversityFocused`) is server-side only and none of them are accepted as query params.

## Fields

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `id` | `01a063b5-6f38-3b26-77aa-c9afde29ba75` | `source_id` as `mlh:<id>` | ULID, stable across slug renames |
| `slug` | `unihack-bd` | `urls` | `https://www.mlh.com/events/<slug>` |
| `name` | `UniHack` | `title` | |
| `status` | `in_progress` | `status` | `pending` / `in_progress` / `ended`. No cancelled state exists |
| `startsAt` / `endsAt` | `2026-11-13T20:00:00Z` | `starts_at` / `ends_at` | ISO 8601 UTC, second precision |
| `dateRange` | `NOV 13 - 15` | — | display string, no year |
| `location` | `Timișoara, Timiș` | `location.city`/`state` | free text, no country |
| `venueAddress.country` | `RO` | `location.country` | ISO-2, set on all 133 events sampled |
| `formatType` | `physical` | `format` | only `physical` and `hybrid_physical` seen (133 events). No `online` |
| `region` | `EMEA` | — | `AMER`/`EMEA`/`APAC`, `null` on 6 past events. Use for cheap prefilter |
| `websiteUrl` | `https://unihack.eu/` | `website_url` | organizer site, always external |
| `customFields.hackathon_focus` | `["Software"]` | `themes` | present on ~10 % of events, else `[]` |
| `customFields.underserved_types` | `[]` | — | always empty in the sample |
| `organizationName` | `null` | `organizer` | `null` in all 133 listing events |
| `logoUrl`, `backgroundUrl` | `https://mlhusercontent.com/…` | — | CDN, no auth |

Detail page (`/events/<slug>`, `props`):

| source field | example | maps to | notes |
|---|---|---|---|
| `event.description` / `rules` | `null` | `description_md` | `null` on all 5 events sampled (UniHack, HackUDC, HackSheffield11, HackEPS, Junction) |
| `event.links` | `[]` | — | empty on the same 5 |
| `event.schedule` | `Sat, Nov 28 · 2:00 PM – Sun, Nov 29 · 10:00 PM UTC` | — | human text, rendered in UTC. **Can be stale**: Junction listing says 13–15 Nov, its schedule string says `Fri, Nov 24 – Sun, Nov 26` (2017 leftover) |
| `submissionDeadline` / `submissionEndsAt` | `null` | `registration_deadline` | `null` on all 5; only MLH-hosted registration sets it |
| `submissionsCount` | `0` | — | |
| `registrationHost` | `external` | — | `mlh` for MLH-hosted signups |
| `prizeCategories[]` | `{name: "Best Use of ElevenLabs", sponsorName, prizeDescription: "Wireless Earbuds"}` | `prize.description` | sponsor prizes only, text. Junction also carries `20 000€ - Junction 2017 Main Prize`, i.e. stale entries persist |

Missing everywhere: registration deadline (except MLH-hosted events), team size, numeric prize, organiser name, language, eligibility, explicit timezone.

Date quirk: MLH pads date-only events with `01:11:11Z` start and `23:59:59Z` end — HackEPS, HackUDC and OxHack all start `T01:11:11Z`. Treat that timestamp as "date unknown time".

## Revisits and identity

- `source_id` = `mlh:<ulid>`; the ULID survives slug changes, the crawler must not key on slug.
- Update detection: re-read the season page and diff `status` + `startsAt`/`endsAt`. There is no ETag or `Last-Modified`, and `cache-control` is `max-age=0` so a conditional GET buys nothing.
- Cancellation: an event that disappears from the season listing is either cancelled or moved; `status` never becomes `cancelled`. Removal has to be soft (stop revisiting, don't mark cancelled) unless the event page 404s.
- Finished event: moves from `upcomingEvents` to `pastEvents` (page 1 first) with `status: ended`.
- Results/winners/people: nothing. Prizes page lists sponsor prizes, never winners, no projects, no teams, no people. `/events/<slug>/gallery` was not probed.

## Rate and failure behavior

- Latency 0.2–1.3 s per page through Cloudflare; no rate-limit headers, no 429 seen at ~20 requests.
- 404 is a rendered `Errors/404` component (65 KB) with status 404, not an empty body.
- 409 (Inertia version mismatch) has a zero-length body — an adapter must reload the HTML page and retry once.
- `api.mlh.com` is a hard 401 without a session.

## Volume and relevance

Season 2027 as of 2026-11-15:

| slice | total | EMEA | AMER | APAC |
|---|---|---|---|---|
| upcoming | 48 | 6 | 41 | 1 |
| past | 85 | 4 | 69 | 6 (+6 `null`) |

EMEA upcoming: UniHack (Timișoara, RO), Junction (Espoo, FI), DurHack (Durham, GB), HackEPS (Lleida, ES), HackSheffield11 (Sheffield, GB), HackUDC (A Coruña, ES). Zero Serbian events in either list. Volume is ~2 EMEA per month, in line with the survey. Everything outside `region == "EMEA"` is noise; that single field removes 87 % of rows.

## Quirks

- `mlh.io` is a redirector now; every URL must be followed to `www.mlh.com`, and the season path is `/seasons/<year>/events` (the site also answers `/seasons/<year>/events/` with a trailing slash).
- Pagination is Inertia, not plain HTTP: two different page params, a required matching version, and 409-on-mismatch.
- The listing is a snapshot of one season; the 2027 season already holds 85 past events, so an adapter must read both lists.
- No JSON-LD at all, so the `jsonld_sitemap`/`jsonld_pages` fallback extracts nothing — a dedicated adapter is justified.
- `01:11:11Z` is a placeholder time for date-only events.
- Detail-page `schedule` and `prizeCategories` can be years out of date.
- `region` can be `null`, and `formatType` has no `online` variant, so hybrid events show up as `hybrid_physical` only.

## Config sketch

```toml
[[source]]
id = "mlh"
adapter = "mlh"
interval = "12h"
rate = "10/min"
seasons = [2027]
inertia = true
pages_upcoming = 3
pages_past = 8
regions = ["EMEA"]
details = true
```
