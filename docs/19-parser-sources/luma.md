# Luma

Probed 2026-11-15. Adapter: `luma`. Kinds: meetup, workshop, conference (whatever a calendar hosts).

Luma is a ticketing/RSVP platform. There is no public discovery API and no Serbian city page, so events are reached through **curated calendar ids** (`cal-…`). The endpoints used here are the site's own undocumented JSON API, not the official one (the official Luma API needs Luma Plus and only exposes calendars you own).

## Access

| URL | method | status | size | notes |
|---|---|---|---|---|
| `api.lu.ma/calendar/get-items?calendar_api_id=cal-…&period=future` | GET | 200 | 31 B … 428 KB | `application/json; charset=utf-8`, `cf-cache-status: DYNAMIC`; 31-byte body = `{"entries":[],"has_more":false}` |
| `…&period=past` | GET | 200 | same shape | past events only |
| `…&pagination_limit=50&pagination_cursor=<next_cursor>` | GET | 200 | — | paging works |
| `…&period=all` | GET | **400** | 42 B | `{"message":"Invalid request.","code":null}` — `future` / `past` only |
| `api.lu.ma/event/get?event_api_id=evt-…` | GET | 200 | 16.6 KB | full event incl. description, tickets, hosts, FAQ |
| `api.lu.ma/event/get?api_id=evt-…` | GET | **400** | 70 B | wrong param name — must be `event_api_id` |
| `api.lu.ma/search/get-results?query=belgrade` | GET | **401** | 48 B | `{"message":"You are not signed in."}` — search needs a session |
| `api.lu.ma/calendar/get?calendar_api_id=…` | GET | **400** | 42 B | needs more params; unused |
| `api.lu.ma/user/get-items`, `api.lu.ma/user/get` | GET | **404** | 24 B | no public user endpoints |
| `luma.com/<slug>` | GET | 200 / 404 | 118–682 KB HTML | Next.js page; `__NEXT_DATA__` carries the payload |
| `luma.com/user/<username>` | GET | 200 | 43 KB | only the user object — **no** `cal-…`/`evt-…` ids |
| `luma.com/<cal-slug>/ical`, `…/garaza.ics` | GET | 200 HTML / 404 | — | no iCal feed: `/ical` returns the HTML page |
| `lu.ma/robots.txt` | GET | 301 | 167 B | redirects to `luma.com` — use `luma.com` hostnames |

No auth, no API key, no visible rate-limit headers, no cookies. `api.lu.ma/robots.txt` disallows only `/insights/`; `luma.com/robots.txt` only names Googlebot (`/social-share`, `/in/`, `/company/`, `/session-*`). Sitemap advertised at `https://sitemap.luma.com/sitemap.xml` (not probed). Undocumented endpoint: it has moved once already, so treat the shape as unstable and pin the adapter to a version.

## Discovery

- **No geographic discovery for Serbia.** `luma.com/belgrade` resolves to `/discover`, whose `initialData.places` is a global nearest-city list (87 places: Budapest, Vienna, Rome, …, Berlin — Belgrade is not among them, no Serbian city at all). `luma.com/serbia` and `luma.com/novisad` resolve to whatever *calendar* happens to own that slug (`luma.com/serbia` → "Notion Serbia"), and `hub201`, `novisad` → 404. So calendars must be curated.
- **Slug → calendar id:** `GET https://luma.com/<slug>` → parse `__NEXT_DATA__.props.pageProps.initialData`; `kind == "calendar"` → `data.calendar.api_id` (plus `data.calendar.slug`, `name`, `timezone`, `upcoming_event_count`, and an `upcoming` map). The same page exists as `luma.com/calendar/<api_id>` and `luma.com/calendar/<api_id>/map`. `luma.com/<slug>` may also be an **event** (`kind == "event"`, e.g. `luma.com/startit` is an event on the Belgian `startitx` calendar) — always check `kind`.
- Calendars found for Serbia (each verified through `get-items` on the probe date):

| calendar | api_id | slug | events seen | notes |
|---|---|---|---|---|
| NS Tech Talks | `cal-46L4tD8g5AzO0l9` | — (slug null) | 2 past (2026-10-09, 2026-06-27), 0 future | Novi Sad meetup; hosts `Slava Oleynik`, `NS Tech Talks Meetup` |
| Garaža | `cal-WcgY6wHZ5tA68xg` | `garaza` | 22 past (2023-06-21 … 2026-11-12), 0 future | Belgrade 21 / Novi Sad 1; "Community of Tech Startup Founders in Serbia"; also the org behind hack.garaza.org |
| AI Salon (global) | `cal-xfRCBuSHDpIoEmp` | `aisalon` | 17 future, 46+ past | worldwide chapters; exactly **1** Serbian event among the 17 (Belgrade, 2026-11-26) |
| AI Salon Belgrade Chapter | `cal-xinIURuDxMnP0eo` | — | 1 past (2024-11-27), 0 future | dormant; the newer Belgrade editions are published on the global calendar |
| "Personal" (Claude AI Builders Belgrade) | `cal-y3HeOwgA9JHbih6` | — | 4 past (2026-06-01, 2026-05-30, 2026-04-06, 2025-06-05), 0 future | the community's Belgrade/Niš events sit on a host's **personal** calendar, mixed with unrelated events (`ZK Belgrade`) |
| Data Sciences Institute | `cal-CRjA3cUzyOBOdhU` | `dsi` | 1 future | **not Serbia** — Toronto. `luma.com/dsi` is not Digital Serbia Initiative |
| Start it X | `cal-jDysoiOmClPa0KQ` | `startitx` | 2 past (2024) | Belgian accelerator, not Serbian Startit |

- **Paging:** response is `{"entries": […], "has_more": bool}`, and `next_cursor` appears when `has_more` is true. Cursor is an opaque base64 JSON (`{"sv":"2026-11-03T16:30:00.000Z","fb":"calev-…"}`) and is passed back as `pagination_cursor` together with `pagination_limit`. Page size: `pagination_limit=5` → exactly 5; `pagination_limit=50` → 46 entries with `has_more: true`, so the server may return fewer than requested while more remain — always follow `next_cursor` until `has_more` is false. Default (no param) returned all 17 upcoming AI Salon events, so the default page is ≥ 17.
- **Boundary overlap:** page 2 started with the same event that ended page 1 (2026-11-03) — dedupe by `event.api_id` while paging.
- **Ordering:** `period=future` ascending by `start_at`, `period=past` descending. `past` holds events that already ended, including ones from earlier the same day (a `past` page started with a 2026-11-14 event while the probe date was 2026-11-15), so the union of `future` + `past` is the whole calendar.
- No server-side filter by city, country or topic: everything must be filtered after download (`event.geo_address_info.country`, `event.timezone`, `categories[].slug`).
- Volume per calendar is small (0–22 entries), so the whole calendar can be pulled every run.

## Fields

### `calendar/get-items` entry

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `event.api_id` | `evt-o9DNH6twxVs8voS` | `source.source_id` suffix | stable, `evt-` prefix |
| `event.url` | `"revenue-day-bg-5"` | `website_url` → `https://luma.com/revenue-day-bg-5` | human slug, may change; API id is the identity |
| `event.name` | `"AI Salon Belgrade powered by Wonderful"` | `title` | emoji and marketing noise common (`📣 Claude AI Builders Belgrade Meetup #3`) |
| `event.start_at` / `end_at` | `"2026-11-26T17:00:00.000Z"` | `starts_at` / `ends_at` | **UTC** (`Z`), not local |
| `event.timezone` | `"Europe/Belgrade"` | `timezone` | **always present**, IANA; use it to render local time (17:00Z = 18:00 Belgrade) |
| `event.location_type` | `offline`, `zoom`, `unknown` | `format` | from event pages (the API list only ever showed `offline`, 96/96 entries). Observed literals: `offline`, `zoom`, `unknown`; `online`/`hybrid` unverified. Map anything ≠ `offline` to `online` unless `geo_address_info` is set |
| `event.geo_address_info` | `{"city":"Beograd","region":null,"address":"AI Hub Belgrade by Startit","country":"Serbia","place_id":"ChIJ…","type":"google","localized":{"sr":{"city":"Нови Сад"}}}` | `location.city`, `location.country`, `location.address` | `null` for online events. Country is a **name** (`Serbia`, `Netherlands`), city in the local script (`Beograd`, `Novi Sad`) |
| `event.geo_address_visibility` | `public` (25), `guests-only` (9) | — | `guests-only` hides the street address even from the API |
| `event.coordinate` | `{lat, lng}` or `null` | — | null when hidden/online |
| `event.virtual_info` | `{"has_access": false}` | — | never carried a join URL anonymously; per-guest join links are `luma.com/join/g-…` |
| `event.event_type` | `independent` | — | all 96 observed |
| `event.visibility` | `public` | — | |
| `event.waitlist_status` | `disabled` / `enabled` / `active` | — | |
| `event.recurrence_id` | `null` | — | recurring events expose the parent |
| `entry.start_at` | `"2026-11-26T17:00:00.000Z"` | — | duplicate of `event.start_at` |
| `entry.status` | `"approved"` | — | |
| `entry.registration_availability` | `open` (31), `waitlist` (2), `sold-out` (1) | `registration_url` (`https://luma.com/<url>#register`) | only signal that registration is closed |
| `entry.ticket_info` | `{"price":null,"is_free":true,"max_price":null,"is_sold_out":false,"spots_remaining":null,"is_near_capacity":false,"require_approval":true}` | — | `is_free`, price in cents (absent here), `require_approval` |
| `entry.ticket_count` / `guest_count` | `0` / `0` | — | often 0 even for past events |
| `entry.hosts[]` | `{"api_id":"usr-…","name":"Romina","username":"rominad","timezone":"Europe/Belgrade","linkedin_handle":"/in/…"}` | `people` (role `organizer`) | `username` is a **Luma** handle, not Telegram/GitHub; `people.telegram_username`/`github_login` stay `None` |
| `entry.tags[]` / `event.categories[]` | `{"api_id":"cat-ai","name":"AI","slug":"ai"}` | `themes` | tags are rare; the calendar's own `categories` are per page, not per event |
| `entry.calendar` | `{"api_id","name","slug","description_short","timezone","avatar_url"}` | `organizer` | the calendar is the organiser |
| `entry.featured_guests` | `[{"api_id":"usr-…","name":"Aleks"}]` | `people` (role `speaker`?) | only when the host lists speakers; usually empty |
| `entry.cover_image.palette` | colour hexes | — | ignore |
| — | — | `summary`, `description_md`, `prize`, `team_size_*`, `eligibility`, `language`, `registration_deadline` | **absent** from the list response |

### `event/get` adds

| source field | example | maps to | notes |
|---|---|---|---|
| `description_mirror` | ProseMirror/Tiptap doc: `{"type":"doc","content":[{"type":"paragraph",…}]}` | `description_md` | the only place the description exists; needs a rich-text → Markdown converter (headings, bullets, links, bold) |
| `ticket_info` + `ticket_types[]` | `{"price":null,"is_free":true}`, `{"api_id":"ttype-…","name":"Standard","cents":null,"currency":null}` | `prize`? no → ignore; price shows on `registration_url` | paid events carry `cents`/`currency` |
| `registration_availability`, `waitlist_active`, `sold_out` | `open`, `false`, `false` | — | |
| `faqs`, `sessions`, `registration_questions` | mostly empty / `[]` | — | `sessions` is an agenda when the host fills it |
| `guest_count`, `ticket_count` | `0`, `0` | — | |
| `locale` | `"sr"` | `language` | returned `sr` for a Belgrade event without any `Accept-Language` header — treat as the event's audience language, not the UI locale |
| `hosts[]`, `featured_guests[]` | as above | `people` | |
| `refund_policy`, `payment_methods`, `tax_config` | — | — | |
| — | — | `organizer` | no explicit organiser field beyond `calendar.name` |

No field carries team size, prize, eligibility, registration deadline or a start time separate from `start_at`. Timezone is never missing, which makes Luma the most reliable source for `starts_at` precision.

## Revisits and identity

- `source_id` = `luma:<event.api_id>` (`evt-…`). `evt-` ids are stable across calendar moves; `event.url` is not (slug edits).
- No `updated_at`/`etag`/`last-modified` anywhere in the API or the HTML: change detection means re-pulling every calendar (cheap: 31 B–130 KB each). Cancel a tracked event only when the id disappears from both `future` and `past` lists of its calendar, and re-check the public page before pushing `status = cancelled` — a moved event also disappears from the old calendar.
- Past events remain listed in `period=past` indefinitely (AI Salon lists 46+ back to July 2026; Garaža keeps 2026), so the same pull serves backfill and revisit.
- No results, winners or attendee lists: `guest_count` is a number, `guest_data` in `event/get` is empty for anonymous callers, `show_guest_list` is a flag only. `people` is limited to hosts/featured guests.
- Rate-limit headers: none of `x-ratelimit-*`, `retry-after`, `x-luma-*` appeared. Latency 150–500 ms; the largest response (`pagination_limit=50` on a busy calendar) was 428 KB.

## Rate and failure behavior

- ~50 requests over the probe, no 429, no CAPTCHA, no Cloudflare challenge (`cf-cache-status: DYNAMIC`), no cookie needed.
- Unknown calendar id → 200 with `{"entries":[],"has_more":false}` (not 404) — a typo silently looks like an empty calendar. Verify against `calendar.name` on the HTML page instead.
- Wrong parameter name → 400 `"Sorry, there was an issue with your request."`; wrong `period` value → 400 `"Invalid request."`; signed-in-only endpoints → 401.
- A calendar page slug that is really an event still returns 200 — check `initialData.kind`.
- The payload is untrusted HTML/ProseMirror text from organisers, and `virtual_info` may later carry join URLs that must never be imported as `registration_url` (SSRF/credential leak).

## Volume and relevance

| calendar | future (real Serbian signal) | past |
|---|---|---|
| Garaža | 0 | 22 (21 Belgrade, 1 Novi Sad, 2023–2026) |
| NS Tech Talks | 0 | 2 |
| AI Salon (global) | 17 (1 Belgrade) | 46+ |
| AI Salon Belgrade Chapter | 0 | 1 (2024) |
| Claude AI Builders (personal cal) | 0 | 4 (3 Belgrade/Niš meetups + 1 unrelated) |

On the probe date the whole known Serbian set yields **1 upcoming event** (AI Salon Belgrade, 26 Nov 2026) plus 28 past Serbian events worth revisiting (22 Garaža, 2 NS Tech Talks, 3 Claude AI Builders, 1 AI Salon Belgrade). Volume is therefore tiny but precise: every entry in a Serbian calendar is relevant, `kind` is `meetup`/`workshop` (occasionally `conference`), and `format` is almost always `offline` in Belgrade/Novi Sad. Noise comes only from the shared calendars: the global AI Salon calendar (16 of its 17 future events are abroad) and the "Personal" one (unrelated personal events) — filter by `geo_address_info.country == "Serbia"` or `timezone == "Europe/Belgrade"` and drop the rest.

## Quirks

- `luma.com/belgrade` is **not** a Belgrade page; Luma has no Serbian place in discovery, which is why the survey's "calendar IDs are curated" is correct and why the id list has to be found by hand.
- `luma.com/dsi` is a Toronto "Data Sciences Institute" calendar, not Digital Serbia Initiative; the DSI "5 upcoming" figure in the survey does not reproduce — `cal-CRjA3cUzyOBOdhU` has **1** future event and it is in Canada.
- The survey's "AI Salon Belgrade 17 upcoming" is the **global** `aisalon` calendar's 17 upcoming events, only 1 of which is in Belgrade; the Belgrade chapter calendar `cal-xinIURuDxMnP0eo` has one past event and nothing upcoming.
- Claude AI Builders Belgrade has no community calendar: its events sit on `cal-y3HeOwgA9JHbih6`, named "Personal", which holds 4 events including the meetups — importing that calendar wholesale also imports the host's unrelated personal events.
- `luma.com/<slug>` is ambiguous across event / calendar / user; `luma.com/user/<name>` exposes no ids at all, so a user profile is not a discovery path.
- `location_type` values seen: `offline`, `zoom`, `unknown`. `unknown` still had `timezone: America/Mexico_City` and no geo — treat as online only if the description says so.
- City names come back in the local script (`Beograd`), while `country` is English (`Serbia`) — normalisation needed for `location.city`.
- Cursor paging repeats the boundary event; `next_cursor` is only present when `has_more` is true.
- Times are UTC with a reliable IANA `timezone`, so no guessing like other sources; whether `end_at` stays consistent for multi-day events with `sessions` is untested.
- `luma.com/<slug>/ical` returns HTML (200), not an ICS feed — no calendar subscription shortcut.

## Config sketch

```toml
[[source]]
id = "luma-rs"
adapter = "luma"
interval = "6h"
rate = "20/min"
calendars = [
  "cal-WcgY6wHZ5tA68xg",
  "cal-46L4tD8g5AzO0l9",
  "cal-xfRCBuSHDpIoEmp",
  "cal-y3HeOwgA9JHbih6",
]
countries = ["RS"]
include_online = false
default_timezone = "Europe/Belgrade"
detail = "event_get"
pagination_limit = 50
```
