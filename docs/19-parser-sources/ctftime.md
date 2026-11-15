# CTFtime

Probed 2026-11-15. Adapter: `ctftime`. Kinds: contest (CTF).

## Access

| what | observed |
|---|---|
| events | `GET https://ctftime.org/api/v1/events/?limit=&start=&finish=` — JSON, 44 KB for 26 events, 1.1 MB for 710 |
| single event | `GET https://ctftime.org/api/v1/events/3419/` — 3.2 KB, exactly the same field set as a list element |
| results | `GET https://ctftime.org/api/v1/results/2026/` — 4.96 MB for 243 events |
| team | `GET https://ctftime.org/api/v1/teams/439893/` — 684 B |
| docs | `GET https://ctftime.org/api/` — lists `/top/`, `/top/{year}/`, `/top-by-country/{cc}/`, `/events/`, `/events/{id}/`, `/teams/{id}/`, `/results/`, `/results/{year}/`, `/votes/{year}/` |
| event page | `GET https://ctftime.org/event/3419/` — 21 KB HTML (26 KB with a results table, 259 KB for a 1551-team event) |
| headers | no `ETag`, no `Last-Modified`, no `Cache-Control`, no rate-limit headers on the API |
| robots | `Crawl-delay: 10`; `Disallow: /user/ /task/tags/ /login/ /export/ /dating/`; `Content-signal: search=yes, ai-train=no, use=reference`. `/api/` and `/event/` are allowed |
| terms | the API page states it is "provided for data analysis and mobile applications only. You can not use this API to run CTFtime clones" |

All requests here were spaced 11 s per the crawl delay; 9 requests total.

## Discovery

`start`/`finish` are Unix timestamps (`&start=1794700800&finish=1805068800`); `limit` is honoured up to at least 1000.

| window | events |
|---|---|
| now → +120 d (2026-11-15 → 2027-03-15) | 26 |
| 2025-01-01 → 2027-03-15 | 710 |

26 in four months is ≈6.5/month, well below the "~19 a month" in the survey; either the schedule is thin right now or organisers add events late. A full incremental sweep needs one request per interval plus one per changed event, so the 10 s crawl delay is the binding cost, not volume.

There is no search, no filter, no sort parameter and no change feed. Incremental discovery = call the events endpoint with `start=now`, diff by `id` and by `start`/`finish`/`participants`, and call `/api/v1/events/{id}/` for events whose listing entry changed (same shape, so only needed when the list response is cached elsewhere).

## Fields

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `id` | `3419` | `source_id` as `ctftime:3419` | integer, matches `/event/<id>/` |
| `title` | `EyesOpenCTF 7th edition` | `title` | |
| `start` / `finish` | `2026-11-20T10:00:00+00:00` | `starts_at` / `ends_at` | ISO 8601 with `+00:00`, always UTC |
| `duration` | `{"days": 2, "hours": 0}` | — | derived from start/finish, redundant; `0d20h`, `0d8h` also seen |
| `format` / `format_id` | `Jeopardy` / `1` | `themes`/`kind` | `Jeopardy` (24/26) and `Hack quest` (2/26). All are contests |
| `onsite` | `true` | `format` | boolean. `false` → online, `true` → offline; nothing for hybrid |
| `location` | `France, Gennevilliers`, `Mathura, Uttar Pradesh, India`, `Onsite at 6 locations in Canada` | `location` | free text, no structure, **no country code.** Empty string for all online events |
| `restrictions` | `Open`, `Academic`, `Casual`, `Prequalified` | `eligibility` | 4 values seen, 20/26 `Open` |
| `url` | `https://eyesopensecurity.com/ctf-briefing.html` | `registration_url`, `website_url` | organiser page, sometimes a form link |
| `ctftime_url` | `https://ctftime.org/event/3419/` | `urls` | |
| `prizes` | `1st: 300 USD + 1 year ExamBoot subscription…` | `prize` | plain text with `\r\n`. No amount/currency fields |
| `participants` | `49` | — | registered teams, integer, often `0` before the event |
| `weight` | `73.17`, `0.0`, `0` | `difficulty` input | CTFtime rating weight; `0` means unrated. JSON is `0.0` for some and `0` (int) for others |
| `organizers[]` | `[{id: 200580, name: "EyesOpen_Security"}]` | `organizer` | one or more, id is a CTFtime organiser id |
| `description` | 20–4600 chars, `\r\n`, plain text | `description_md` | the only place team size and eligibility prose live |
| `is_votable_now` / `public_votable` | `false` / `true` | — | weighted-vote status, not imported |
| `live_feed` | `""` | — | always empty in the sample |
| `ctf_id` | `847` | — | CTFtime "CTF" entity id, shared across editions of the same competition |
| `logo` | `https://ctftime.org//media/events/…png` | — | note the double slash |

Missing: team size (field), registration deadline, timezone field (UTC is implied), language, prize amount/currency, country code.

Team size exists only as prose in `description`. Real examples from the 26-event sample:

| event | description text |
|---|---|
| `3485` L3m0n CTF 2026 | `team-based event: teams of 1 to 4 players` |
| `3489` CyberNSK2027 | `Teams of up to 5 participants (school and university students)` |
| `3510` Black Hat India 2026 | `Team size: up to 3` |
| `3386` BlackHat MEA CTF Final | `teams of 3–5 players` |
| `3498` Leo's 20th Birthday Bash | `Teams can have 1 to 10 members (6 to 8 recommended)` |

So the adapter needs a regex pass over `description` (`teams? of (up to )?\d+`, `\d+ to \d+ (players|members)`) plus the LLM for the rest.

Event page (`/event/<id>/`) adds, in HTML only — no JSON-LD:

- `Fri, 20 Nov. 2026, 10:00 UTC — Sun, 22 Nov. 2026, 10:00 UTC` (human dates, UTC labelled).
- `On-line` or `On-site` plus a country flag, `Format: Jeopardy`, `Official URL`.
- `Future weight: 50.00` and `Rating weight: 33.49` — the API exposes only one `weight`.
- Organiser names as links.
- For finished events, a results table: `Place | Team | CTF points | Rating points`, team names linked to `/team/<id>` (1,551 rows on `event/2969`).

## Revisits and identity

- `source_id` = `ctftime:<id>`.
- No ETag/Last-Modified and no `If-Modified-Since` support, so every revisit is a full list fetch; diff on `start`, `finish`, `participants`, `weight` and `description` length.
- Cancellation: a vanished event is indistinguishable from a deleted one — absence from the list is the only signal, so it must be treated as soft (stop tracking) rather than `cancelled`.
- Finished event: disappears from a `start=now` window, then appears in `/api/v1/results/<year>/`.

Results endpoint shape (verified `/api/v1/results/2026/`):

```json
{"2969": {"title": "UofTCTF 2026",
          "time": 1768003200.0,
          "scores": [{"team_id": 58979, "points": "4787.0000", "place": 1}, …]}}
```

- Top-level object keyed by event id as a **string**; `time` is an epoch float (event date); `points` is a **string** with 4 decimals; `scores` carries every ranked team (8 to 1,550 rows, median 314). 243 events, 4.96 MB for 2026.
- No rating points, no team names, no country — one `teams/<id>` call per team to enrich, which is unaffordable at 314 teams/event with a 10 s delay. Team names are free from the event page HTML instead (1 request per event).
- `/api/v1/teams/<id>/` returns `{id, name, primary_alias, aliases[], academic, country: "RU", logo, rating: {"<year>": {place?, country_place?}}}`. **No member list exists**, so `people` can hold team names at best; CTFtime never publishes individual competitors.
- A finished event's `people`/results are therefore: team name + place + points, or nothing.

## Rate and failure behavior

- Crawl-delay 10 is real: it is the dominant cost. 26 upcoming events cost 1 request; a year of results costs 1 request; a per-team enrichment does not scale.
- Latency 0.4–1.1 s including the 4.96 MB results payload.
- No 429 observed, but the probe never exceeded 1 request/11 s by design. The `Disallow: /export/` entry shows bulk export is deliberately closed.
- 404 for an unknown event id returns the HTML error page.

## Volume and relevance

Window 2025-01-01 → 2027-03-15 (710 events):

| slice | count |
|---|---|
| online (`onsite: false`) | 570 |
| onsite | 140 |
| onsite with a European-looking location string | ~39 (`Italy, Torino`, `France, Gennevilliers`, `SwissTech Convention Center / Lausanne / Switzerland`, `Vienna, Austria / Hybrid (Online)`, `Timisoara, Romania`) |
| Serbia / Belgrade | **0** |

Upcoming 120-day window: 26 events, 5 onsite, 1 in Europe (`OTERIA Cyber Cup #4`, Gennevilliers, FR — academic). Everything else is online and global, which is relevant to Serbian teams by participation even though nothing is local. Noise is low: no non-CTF events, no cancelled placeholders, no recurring meetups.

## Quirks

- `location` is prose in at least five layouts (`Country, City`; `City, State, Country`; `venue / city / Country`; `Country`; `Onsite at N locations in Country`) and is `""` for online events — **and for 6 onsite events as well**.
- `onsite` and `location` disagree: `CyberNSK2027-Qualifier` has `onsite: false` with `location: "Russia"`; two events are labelled `Hybrid (Online)` inside the location string while `onsite` stays a boolean.
- `weight` type flips between `0.0` and `0` in the same response.
- `logo` URLs contain a double slash after the host.
- The event page shows a `Rating weight` that the API does not expose, and a `Future weight` that exists only before the event.
- Descriptions carry `\r\n` and marketing prose (one is 4.6 KB); the stiff cap in §19.3 (20,000) is not at risk but summary extraction needs it trimmed.
- Team-size phrasing includes ranges with a recommendation (`1 to 10 members (6 to 8 recommended)`) and non-numeric forms (`Teams must consist entirely of female participants` for eligibility).

## Config sketch

```toml
[[source]]
id = "ctftime"
adapter = "ctftime"
interval = "12h"
rate = "1/10s"            # robots Crawl-delay: 10
window_days = 120         # start=now, finish=now+window
limit = 200
details = "changed"       # /event/<id>/ only for new or changed events (team size prose + results)
results_years = [2026]    # 5 MB each, fetch after the year ends or on demand
fetch_results = true
team_size_regex = true    # "teams of (up to )?N", "N to M (players|members)"
```
