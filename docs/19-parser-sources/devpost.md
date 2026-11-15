# Devpost

Probed 2026-11-15. Adapter: `devpost`. Kinds: hackathon (plus project/winner records for `people`).

## Access

| what | observed |
|---|---|
| listing | `GET https://devpost.com/api/hackathons?status[]=open&page=1` — JSON, 9.6 KB, `content-type: application/json; charset=utf-8` |
| paging | same URL `&per_page=40&page=N`; `per_page` caps at **40** (asked 50, got 40) |
| single hackathon | `GET /api/hackathons/31667` → **404** (HTML error page). No per-item endpoint |
| hackathon site | `GET https://<slug>.devpost.com/` — HTML 141–201 KB, one `application/ld+json` Event block |
| rules page | `GET https://<slug>.devpost.com/rules` — HTML 170 KB, prose |
| schedule page | `GET https://<slug>.devpost.com/details/dates` — HTML 119 KB, schedule table |
| gallery | `GET https://<slug>.devpost.com/project-gallery` — HTML 148–201 KB; carries `<script type="application/ld+json" id="challenge-winners-json-ld">` |
| winner project | `GET https://devpost.com/software/<slug>` — HTML 130 KB, JSON-LD `CreativeWork` |
| project search | `GET https://devpost.com/software/search?query=<q>` — JSON, 4 KB, `total_count` + `software[]` |
| robots | `Allow: /` for `*`; blocks BLEXBot, Omgilibot, Omgili, ImagesiftBot, Bytespider only |
| headers | API sends `ETag` + `cache-control: max-age=0, private, must-revalidate`. `If-None-Match` with the same ETag → **200**, not 304. No rate-limit headers |
| ToS | Devpost's terms forbid automated access to the site. This survey ran minimal requests on the user's explicit acceptance. Nothing below relies on scraping at scale |

Latency 0.05–0.3 s. No 429 or challenge seen across ~35 requests without a browser UA.

## Discovery

`/api/hackathons` is the only enumeration path. `meta` = `{total_count, per_page, fuzzy}`, so `ceil(total_count / per_page)` requests per status.

| query | `total_count` |
|---|---|
| `status[]=open` | 77 |
| `status[]=upcoming` | 90 |
| `status[]=ended` | 14,185 |
| `status[]=open&open_to[]=public` | 66 |
| `status[]=open&open_to[]=students` | 11 |
| `status[]=open&challenge_type[]=online` | 55 |
| `status[]=open&challenge_type[]=in-person` | 22 |
| `status[]=open&themes[]=Machine%20Learning%2FAI` | 38 |
| `status[]=open&location=online` | 77 (param ignored) |
| `status[]=ended&winners_announced[]=true` | 14,185 (param ignored) |
| `status[]=open&page=999` | `{"hackathons":[],"meta":{…}}` with 200 |

So: filters that work are `status[]`, `open_to[]`, `challenge_type[]`, `themes[]`, plus `per_page`/`page`. Sort/order/`location`/`winners_announced` are ignored. Three filter groups combine (each is an OR within the group); combining `challenge_type[]=online` with `open_to[]=students` was not tested.

Only 77 open events exist in total and 21 of them are in-person, so a full open sweep is **2 requests**. `ended` cannot be swept (14,185 items ≈ 355 requests).

## Fields

Listing item (`/api/hackathons`):

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `id` | `31667` | `source_id` as `devpost:<id>` | integer, stable |
| `title` | `Kiroween 2026` | `title` | |
| `url` | `https://kiroween.devpost.com/` | `website_url`, `registration_url` | subdomain site |
| `displayed_location.location` | `Online`, `Stanford University`, `virtual` | `location` / `format` | **free text, no country, no city split.** `Online`/`virtual` were the only format markers |
| `open_state` | `open` | `status` | `open` / `ended` observed (`upcoming` implied by status filter) |
| `submission_period_dates` | `Oct 30 - Dec 04, 2026` | `starts_at`/`ends_at` | free text, **no time, no timezone**; forms seen: `Nov 15, 2026` (single day), `Nov 14 - 15, 2026`, `Oct 30 - Dec 04, 2026`, `Nov 15, 2026 - Feb 15, 2027`, `Feb 27 - Dec 25, 2026` |
| `time_left_to_submission` | `19 days left`, `about 14 hours left`, `9 minutes left` | — | coarse, useful only to sanity-check a parsed end |
| `themes[]` | `[{id: 6, name: "Machine Learning/AI"}]` | `themes` | 27 distinct tags; `Beginner Friendly` and `Open Ended` are not topics |
| `prize_amount` | `$<span data-currency-value>100,000</span>`, `$CAD <span …>1,050</span>`, `€<span …>1,000</span>`, `₹ <span …>` | `prize.amount`+`currency` | HTML. Currency is a prefix symbol; 67 `$` (ambiguous USD/CAD), 7 `$CAD`, 2 `₹`, 1 `€` |
| `prizes_counts` | `{cash: 1, other: 0}` | `prize.description` | 34 of 77 have `cash: 0` but a non-zero `prize_amount` |
| `registrations_count` | `14230` | — | signups, not submissions |
| `organization_name` | `Kiro` | `organizer` | always present in 77/77 |
| `winners_announced` | `false` | — | `true` on ended events with published winners |
| `invite_only` | `false` | `eligibility` | 11/77 invite-only |
| `featured`, `thumbnail_url`, `submission_gallery_url`, `start_a_submission_url`, `managed_by_devpost_badge`, `analytics_identifier` | | — | not imported |

Hackathon site JSON-LD (richer, per-event fetch):

| JSON-LD | example | maps to | notes |
|---|---|---|---|
| `startDate`/`endDate` | `2026-10-30T12:00:00.000-04:00` / `2026-12-04T17:00:00.000-05:00` | `starts_at`/`ends_at` | **real ISO datetimes with offset** — the only machine-readable dates available |
| `eventAttendanceMode` | `https://schema.org/OnlineEventAttendanceMode` | `format` | `Offline`/`Mixed` variants assumed, not observed |
| `eventStatus` | `https://schema.org/EventScheduled` | `status` | `EventCancelled` would arrive here |
| `organizer.name` | `Kiro` | `organizer` | |
| `keywords[]` | `["Beginner Friendly","Machine Learning/AI","Open Ended"]` | `themes` | same list as the API |
| `additionalProperty` | `{name: "Total prize pool", value: "100000", unitText: "USD"}` | `prize.amount`/`currency` | numeric + ISO currency, better than the API |
| `location.address` | `{name: "Online", addressLocality: "", addressRegion: ""}` | `location` | empty for online; in-person pages were not inspected for `addressCountry` |
| `description` | 250-char truncated prose | `summary` | ends with `...`, do not use as description |

Schedule page adds `Submissions`, `Judging`, `Winners Announced` rows as `October 30 at 9:00am PDT` — a "Schedule timezone" `<select>` is client-side only, so the labels are the only timezone hint and they are inconsistent (`PST` label next to a `-05:00` offset).

Rules page is prose: `Teams of Eligible Individuals, maximum 3 participants per team`, `at least the age of majority`, per-hackathon residency/sanction clauses. Team size and eligibility exist **only** here.

Missing from every machine-readable layer: country, city, team size, registration deadline (the submission start is the closest thing), language, eligibility.

## Revisits and identity

- `source_id` = `devpost:<id>`.
- Cheap revisit: one `?status[]=open&per_page=40&page=N` sweep (2 requests) and diff `open_state` + `time_left_to_submission`. Finished events fall out of `open`; there is no `cancelled` signal anywhere except the subdomain's `eventStatus`.
- Per-event truth requires the subdomain fetch (ISO dates, prize pool, event status). Budget 1 request per kept event.
- ETag exists on the API but conditional requests are ignored (200 + body again), so no bandwidth win.
- Results and winners, verified on `ycp-hacks-2026.devpost.com` (`winners_announced: true`):
  1. `GET https://ycp-hacks-2026.devpost.com/project-gallery` → `<script type="application/ld+json" id="challenge-winners-json-ld">`, an `ItemList` named `YCP Hacks 2026 winners` with **15** entries:
     `{"@type":"CreativeWork","name":"Default Deny","url":"https://devpost.com/software/default-deny","award":["Winner,  Best of Show Overall — YCP Hacks 2026","Winner, On-Theme Hack (Theme - Retro) — YCP Hacks 2026"]}`. Award strings are prize names; one project can appear once with multiple awards.
  2. Each project URL → `GET /software/<slug>` with JSON-LD `CreativeWork`: `author[]` = Devpost usernames (`{"@type":"Person","name":"Karimov24","url":"https://devpost.com/Karimov24"}`), `award[]` (same strings), `subjectOf[]` → the hackathon, `keywords[]` = built-with tags.
  3. GitHub: repos appear only in the page's `Try it out` list (`<ul data-role="software-urls">`) when the author linked one — verified `https://github.com/Archie505/Hackathon/tree/main` on `software/coterie-57fv6e`.
  4. Stronger identity: member avatars are served from `avatars.githubusercontent.com/u/<numeric-id>`. `GET https://api.github.com/user/173314529` → `{"login":"Karimov24"}` — a real GitHub account the user connected, resolvable without guessing usernames.
- `winners_announced` is the trigger for the winner fetch; galleries of hackathons without winners carry no `challenge-winners-json-ld`. Central Hacks 2024 (`winners_announced: true`, 11 projects) showed no winning markup on any gallery page, so the flag can be stale.
- The gallery's prize checkboxes (`prize_filter[prizes][]=75468`) and `/submissions/search` returned **0 gallery items** for both a valid and an invalid prize id, so filtering by prize is not usable without JS.
- `/software/search` looks like the winner entry point but is not: `query=Central+Hacks`, `&hackathon=`, `&hackathon_id=`, `&winners=true` are all ignored or soft-ignored — `query=Central+Hacks` returned `total_count: 216` of unrelated projects, `query=&hackathon_id=20677` returned 598,698. It does return `members[]`, `winner` (bool) and `tags[]` per project, useful only for name lookups.

## Rate and failure behavior

- ~35 requests over the probe, no 429, no Cloudflare challenge, no cookie requirement.
- Unknown hackathon id → 404 HTML (not JSON). Unknown project slug → 404.
- Page beyond the last → 200 with `hackathons: []`.
- `per_page` silently clamps to 40; large values are not an error.
- `total_count` is authoritative; `fuzzy: false` in every response observed.

## Volume and relevance

| slice | count |
|---|---|
| open | 77 |
| open, online | 56 |
| open, in-person | 21 — all US/Canada (Arlington, Austin, USC, ASU ×2, Stanford, Toronto, UBC, Montréal, Windsor, Edmonton, Seattle, Virginia Beach, Milpitas + online) |
| open, in-person, Europe | **0** |
| upcoming | 90 |
| ended | 14,185 |

Relevance comes entirely from the online slice: worldwide online hackathons open to European participants, plus a handful of European in-person events that Devpost hosts (DurHack 2026, GreatUniHack 2026 appear under `ended`, not `open`). Country is not in the payload, so "EU in-person" cannot be filtered — the location text has to be geocoded or matched against a city list. Themes and prize size are the only useful relevance signals at listing level.

## Quirks

- Two date representations that disagree in quality: free text without timezone in the API, real offset datetimes in the subdomain JSON-LD.
- A `PST` label on a value that is `-05:00` (EST). Never trust the schedule labels.
- `prize_amount` mixes currency symbols (`$` means USD or CAD), and `prizes_counts.cash: 0` coexists with a non-zero amount (34 of 77).
- One open event has a 10-month window (`Feb 27 - Dec 25, 2026`); `time_left_to_submission` still says "about 1 month".
- `displayed_location` values are venue names, not places (`Ginsburg Hall, USC`, `Polish Combatants' Hall`, `DOW Theatre`); one is literally `virtual` while 56 say `Online`.
- `winners_announced: true` does not guarantee a winner list on the site.
- The `/events` listing page is a JS app; only the JSON API and the server-rendered subdomains are usable.
- The subdomain JSON-LD `description` is pre-truncated at ~250 chars with `...`.

## Config sketch

```toml
[[source]]
id = "devpost"
adapter = "devpost"
interval = "6h"
rate = "10/min"
statuses = ["open", "upcoming"]
per_page = 40
details = true            # fetch <slug>.devpost.com for ISO dates + prize pool
detail_scope = "kept"     # only for events that survive the relevance filter
winners = true            # after an event ends: project-gallery ItemList + project pages
winner_max_projects = 50
resolve_github = true     # avatars.githubusercontent.com/u/<id> -> api.github.com/user/<id>
```
