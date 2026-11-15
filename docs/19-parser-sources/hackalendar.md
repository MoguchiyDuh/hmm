# Hackalendar (hackalendar.com)

Probed 2026-11-15. Adapter: `hackalendar` (public MCP endpoint; `/e/{slug}` JSON-LD as
fallback). Kinds: hackathon.

Hand-curated list ("a short list of European hackathons worth showing up for, checked by hand").
Next.js on Vercel (`/_next/static/immutable/...`). The homepage's 20 are only the *upcoming* set — `/past` holds 66 more and
`/sitemap.xml` enumerates all 86. There is also `POST /api/mcp` with three public browsing tools.

## Access

| URL | method | status | size | notes |
|---|---|---|---|---|
| `/robots.txt` | GET | 200 | 473 B | one group for GPTBot, ClaudeBot, PerplexityBot, Bingbot, CCBot, Amazonbot, Google-Extended … + one for `*`: `Allow: /`, `Disallow: /admin`, `/api/admin`. `/api/mcp` is **not** disallowed |
| `/sitemap.xml` | GET | 200 | 18,212 B | 96 URLs; `etag`, `last-modified` = request time, `x-vercel-cache: MISS`, `max-age=0, must-revalidate` |
| `/` | GET | 200 | 204,993 B | 20 `/e/` links, `ItemList` with 20 items (no `numberOfItems`) |
| `/past` | GET | 200 | 573,662 B | 66 `/e/` links, no JSON-LD list |
| `/e/<slug>` | GET | 200 | 110,705 B | `Event` JSON-LD + `WebSite`/`Organization`; `etag`, `max-age=0, must-revalidate` |
| `/agents` | GET | 200 | 60,500 B | documents the MCP endpoint and the public tools |
| `/feed.xml` | GET | 200 | 52,822 B | RSS 2.0, 20 items, `max-age=1800`, `ttl 60` |
| `/calendar.ics` | GET | 200 | 49,587 B | 20 `VEVENT`, `max-age=3600`, `X-PUBLISHED-TTL:PT12H` |
| `/api/mcp` | POST | 200 | 0.9–34 KB | JSON-RPC, streamable HTTP, stateless (no `Mcp-Session-Id` returned), `application/json` |

robots.txt names GPTBot, ClaudeBot, PerplexityBot, Bingbot, CCBot, Amazonbot, Google-Extended and
`*` with the same rules, so our UA is treated as a normal crawler. No `Crawl-delay`. No ToS page was
probed; the `/agents` page explicitly invites machine clients and offers subscriptions/webhooks, so
programmatic reading is intended.

`/e/<unknown-slug>` → **404** (16,740 B error page) — proper status, no shell. `If-None-Match` on a
detail page returns **304 / 0 B**, so conditional revalidation works here (unlike hackathonhub).

## Discovery

Four overlapping paths; pick one:

1. **MCP `search_hackathons`** (best): public, no key, JSON-RPC `tools/call`. Returns
   `{count, nextOffset, events[]}`; `limit` 1–50 default 20, follow `nextOffset` until `null`.
   Filters: free-text `query`, `city`, `theme` (`ai, web3, climate, health, data, hardware, student,
   creative`), `mode` (`in_person, online, hybrid`). Observed `count: 20, nextOffset: null` with
   `limit: 50` — the public tool set is the same 20 upcoming events as the homepage. `city: "Belgrade"`
   → `{count: 0}`.
2. **`sitemap.xml`** — the only enumeration of the full set: 96 URLs = `/`, 5 city pages
   (`paris, berlin, amsterdam, london, toulouse`), `/past`, `/agents`, `/submit`, `/privacy` and
   **86 `/e/<slug>`**. 86 = 20 upcoming + 66 past. Per-event `<lastmod>` is a real ISO timestamp
   (unlike hackathonhub): observed spread 2026-08-20 → 2026-11-15, 9 events touched on the probe day,
   8 the day before — a publish/edit timestamp, usable as an incremental watermark.
3. `/past` gives the 66 finished events as plain anchors, no JSON-LD list, 574 KB.
4. `MCP get_hackathon {slug}` resolves past slugs too (tested
   `hacktoberfest-hack-day-berlin-2026`, 4 Oct 2026) — so past events are fully readable by slug.

`feed.xml` and `calendar.ics` are derivative (20 upcoming each) and add nothing over the MCP call.

## Fields

MCP `search_hackathons`/`get_hackathon` is the richest shape; JSON-LD on `/e/<slug>` is a strict
subset with a much worse date type and a giant duplicated description.

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `slug` | `nasa-space-apps-challenge-paris-2026` | `source_id` | `hackalendar:<slug>` |
| MCP `name`, JSON-LD `name` | `NASA Space Apps Challenge - Paris 2026` | `title` | city and year embedded in the title, also parsed into `location` |
| MCP `startsAt` | `2026-11-14T07:00:00.000Z` | `starts_at` | ISO UTC |
| MCP `endsAt` | `2026-11-15T20:00:00.000Z` **or** `2026-11-15` | `ends_at` | **date-only string** when the end time is unpublished — see `temporalPrecision` |
| MCP `temporalPrecision` | `{"start":"datetime","end":"date","timezone":"published"\|"unknown"}` | — | tells you whether a missing time is known-unknown, not midnight |
| MCP `timezone` | `Europe/Paris` (16/20), `Europe/London` (1), `Europe/Helsinki` (1), `null` (2) | `timezone` | IANA, but a **past** Berlin event (`hacktoberfest-hack-day-berlin-2026`) also reports `Europe/Paris`, so the zone is derived rather than published — downgrade confidence |
| MCP `location` + `countryCode` | `Berlin` / `DE` | `location.city`, `location.country` | no street address, no coordinates; literal values `Online` and `Location to confirm` appear as "cities", `countryCode: null` for both |
| MCP `mode` | `in_person` 19, `online` 1, `hybrid` 0 (of 20) | `format` | mirrors `eventAttendanceMode` in JSON-LD |
| MCP `themes` | `["ai","climate","data","student","creative"]` | `themes` | 8-value vocabulary; `student` doubles as eligibility |
| MCP `isFree` | `true` 16, `false` 1, `null` 3 | — | JSON-LD `offers.price: 0` + `priceCurrency: EUR` |
| MCP `prizePool` | `"€10,000+"`, `"€1,000"`, `null` 18/20 | `prize` | **free-text string, not a number** — currency symbol embedded, `+` suffix |
| MCP `organizer` | `Code Club Berlin`, `Panossian`, `null` 1/20 | `organizer` | JSON-LD `organizer.name`; a person's name is not an organization |
| MCP `registrationUrl` | `https://lu.ma/jb33a8gb` | `registration_url` | JSON-LD puts the same URL in `offers.url` and `sameAs` |
| MCP `registrationDeadline`, `deadlineNote` | `null` for all 20 upcoming | `registration_deadline` | tool `upcoming_deadlines` derives from start date when absent |
| MCP `happeningNow`, `cancelled`, `picked`, `pickedAt`, `pickNote` | `false`, `true` on a 13–15 Nov event | `status` | **`cancelled: true` is an explicit cancellation flag** |
| `description` | 1–4 KB, French **and** English concatenated | `description_md` | JSON-LD only; the MCP `get_hackathon` returns the same blob. Unstructured, contains raw "\n" and a contact email |
| — | — | `team_size_*`, `language`, `eligibility`, `areas`, `people`, `difficulty` | absent. `language` would have to be inferred from the prose (fr/en mixed) |
| `/calendar.ics` `DTSTART;VALUE=DATE` | `20261113` | `starts_at` | all-day when no time is published; the description carries the real local time text |

## Revisits and identity

- Identity: `hackalendar:<slug>`. Slug is `title-city-year`, so a title edit produces a new slug.
- Update detection, in order of quality: (a) `list_notifications` / subscriptions (private access —
  public tools expose no change feed, and the `/agents` page advertises exactly this:
  "follow new publications and important changes"); (b) sitemap `<lastmod>` watermark; (c) HTTP
  `etag` on `/e/<slug>` — `If-None-Match` returns 304 with an empty body, so tracked events can be
  revalidated for free; RSS `pubDate` as a last resort.
- Cancellation: `cancelled` is a boolean field in every MCP payload (never `true` in the 21 rows
  inspected), so cancellation is representable without the event disappearing.
- Finished events move from `/` to `/past` and stay readable by slug (verified for
  `hacktoberfest-hack-day-berlin-2026`, 4 Oct 2026), each keeping its `/e/` page.
- Results/winners/participants: **none**. No `people` data, no winner lists, no attendee names.
- The 20-item set rotates daily: `lastBuildDate` 18:10 GMT, events ending today still listed
  (`happeningNow: true`, `endsAt` 20:00Z).

## Rate and failure behavior

- 15 requests over ~6 min at ≥1 s spacing: all 200 (except the deliberate 404), 0.1–0.4 s, no 429, no
  challenge, Vercel `MISS` on new paths and `STALE` on `/`.
- Cache hints to respect: `feed.xml` `max-age=1800`, `calendar.ics` `max-age=3600`, HTML
  `max-age=0, must-revalidate` + `etag`.
- MCP: stateless JSON-RPC over POST; `initialize` is enough to get going, no session header needed
  after it. Bad tool arguments return a normal MCP error inside a 200.
- No per-request rate-limit headers.

## Volume and relevance

| scope | count |
|---|---|
| all events (sitemap) | 86 |
| upcoming (homepage / MCP / feed / ics) | 20 |
| past (`/past`) | 66 |
| country `FR` among the 20 upcoming | 13 (Paris 9, Vincennes, Le Kremlin-Bicêtre, Haguenau, Toulouse) |
| other countries among the 20 | DE 2, GB 1, FI 1, NL 1, `null` 2 (one online, one venue TBC) |
| **Serbia** | **0** — no slug, no sitemap entry, `search_hackathons(city="Belgrade")` → 0 |

Zero Serbian coverage, and heavily skewed to Paris. Its value is precisely the overlap cohort:
`HackNation`, `European Defense Tech Hackathon`, `Tech Europe`, `Mistral/Anthropic/Lovable/Cursor`
sponsored hackathons — these also appear on MLH and hackathonhub.eu, so it mostly serves as a
cross-source dedupe and a quality filter ("human-approved") rather than a volume source. Noise is
close to zero; the cost is that everything is one city or online.

## Quirks

- The MCP endpoint is the real API: `POST /api/mcp`,
  tools `search_hackathons`, `get_hackathon` (slug), `upcoming_deadlines` (`days` 1–60), plus
  private tools for submissions, subscriptions, notifications and webhooks.
- `timezone` is unreliable: a Berlin event reports `Europe/Paris`. Combined with date-only `endsAt`
  (3 of 20), any `ends_at` can be off by up to a day — treat `temporalPrecision.end == "date"` as
  23:59 local of the site's assumed zone and mark the field inferred.
- `prizePool` is a display string (`"€10,000+"`), and `isFree` is `null` for 3 of 20 — no reliable
  price model.
- `endsAt` mixes ISO datetimes and bare dates in the same array; a naive `fromisoformat` will produce
  naive datetimes.
- JSON-LD `description` is the French and English text concatenated with no separator, includes an
  email address and a raw `\n` escape; useful for `description_md` but needs splitting.
- JSON-LD `organizer.name` is sometimes a person (`Panossian`) and `offers.price` is `0` +
  `priceCurrency: EUR` even for free events — no price signal.
- `/past` is 574 KB of HTML with the 66 links not exposed as JSON-LD; JSON-LD `ItemList` exists only
  on `/`.
- City pages `/paris`, `/berlin`, `/amsterdam`, `/london`, `/toulouse` exist (priority 0.8, daily)
  — subsets of the same 86, no extra events.
- `/agents` says browsing is public and writes need private access; a parser that only reads needs no
  key, which is unusual and useful.

## Config sketch

```toml
[[source]]
id        = "hackalendar"
adapter   = "hackalendar"
interval  = "12h"
rate      = "12/min"
discover  = "https://hackalendar.com/sitemap.xml"
url_re    = "^https://hackalendar\\.com/e/.+$"
use_mcp   = true
mcp_url   = "https://hackalendar.com/api/mcp"
mcp_tools = ["search_hackathons", "get_hackathon", "upcoming_deadlines"]
past_listing = "https://hackalendar.com/past"
timezone_fallback = "Europe/Berlin"
```
