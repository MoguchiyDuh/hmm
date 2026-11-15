# GDG (gdg.community.dev, Bevy)

Probed 2026-11-15. Adapter: `jsonld_pages`. Kinds: conference, meetup, hackathon, workshop.

Bevy-hosted chapter platform. Detail pages carry `schema.org/Event` JSON-LD **and** a Next.js `__NEXT_DATA__` blob with more fields than the JSON-LD. The chapter listing does load through robots-disallowed `/api/`, but the same data is server-rendered into the chapter page's HTML and `__NEXT_DATA__`, so no rendering and no `/api/` call is needed.

## Access

`https://gdg.community.dev/robots.txt` → 4,055 B, `Crawl-delay: 2`, `Disallow: /health/ /accounts/ /api/ /newsletter/ /gql/ /triggers/ /u/ /viewserver-health/`, and the same block repeated for 20 AI crawler UAs (GPTBot, ClaudeBot, anthropic-ai, Claude-Web, PerplexityBot, CCBot, Bytespider, …). `Sitemap: https://gdg.community.dev/sitemap.xml` on the last line. Everything under `/gdg-<slug>/` and `/events/details/` is allowed; **`/api/` is off-limits.**

| URL | status | bytes | notes |
|---|---|---|---|
| `/sitemap.xml` | 200 | 27,505 | `Cache-Control: public,max-age=1800`, `etag: "5741428a…"`. Index of `sitemap-chapters.xml` ×5, `sitemap-pages.xml`, `sitemap-forums.xml` ×33, `sitemap-posts-YYYY-MM.xml` ×18 (2025-06 … 2026-11) |
| `/sitemap-chapters.xml` (+`?p=2..5`) | 200 | 58,626 / 76,327 / 83,586 / 72,684 / 5,767 | **2,048 chapter URLs**, listed alphabetically, `<changefreq>weekly</changefreq><priority>0.6</priority>`, **no `lastmod`** |
| `/sitemap-posts-2026-11.xml` | 200 | 28,669 | forum topics (`/forum/…/topic/…`), not events. The `posts` sitemaps contain **no event URLs at all** |
| `/gdg-belgrade/` | 200 | 728,906 | chapter page, fully server-rendered |
| `/events/details/<slug>/` | 200 | 1,393,369 | event page; JSON-LD + `__NEXT_DATA__` |
| `/events/details/<slug>` (no slash) | **308** | 69 | → trailing-slash URL |
| `/gdg-novi-sad/` | **301** → `/` | 0 | chapter does not exist |
| `/gdg-novisad/` | 404 | — | |
| `/events/` | 200 | 47,907 | global listing is client-rendered: no `__NEXT_DATA__`, zero event links in HTML |
| `/gdg-belgrade/past-events/` | 404 | 45,320 | |
| `/gdg-belgrade/?past=true` | 200 | 728,926 | query ignored, identical payload to `/gdg-belgrade/` |

HTML responses carry `etag: "22b6445d…"` (nginx) but **no `Last-Modified` and no `Cache-Control`** on chapter/event pages; sitemaps do have `max-age=1800` + ETag. iCal/RSS: none found — no `.ics`, `rss`, `ical` or `feed` link in either the chapter or the event page. `Globals.algolia_application_id = 'AMJ1KW79KP'` is published, but no search-only key is, so the Algolia index is not usable.

## Discovery

Server-rendered, no browser needed:

1. **Chapters** — `/sitemap-chapters.xml?p=1..5` (5 requests, 300 KB) → every chapter URL. Serbian chapters in that list (2,048 total, 2 for RS):
   - `https://gdg.community.dev/gdg-belgrade/` — chapter id **3053**, 1,049 members, `Belgrade, Central Serbia (RS)`, website `gdg-belgrade.com`
   - `https://gdg.community.dev/gdg-on-campus-university-of-belgrade-belgrade-serbia/` — chapter id **2411**, 885 members
   - **No `gdg-novi-sad`**: absent from the sitemap and `301`s to the homepage. The Novi Sad chapter no longer exists on this platform (where it moved is unverified).
2. **Events per chapter** — `GET /gdg-<slug>/` and read `__NEXT_DATA__.props.pageProps.prerenderData`:
   - `upcomingEvents`: `{count, pagination:{page_size:500}, results:[…]}` — **all** upcoming events, each with `title`, `start_date` (UTC), `description` (HTML), `description_short`, `url`, `cohost_registration_url`, `event_type_title`, `is_conference`, `chapter_id`
   - `pastEvents`: same shape but only **4** rendered (`page_size: 4`) out of `count: 20`; `links.next` points at `http://testserver/api/event_slim/for_chapter/3053/?…&page=2&status=Completed` — an internal `/api/` URL, **not to be fetched** (robots) and not resolvable from outside anyway (it leaks the internal hostnames `testserver` and `platform-google:80`)
   - `chapterData`: `id`, `title`, `city`, `country`, `country_name`, `language`, `latitude`, `longitude`, `members_count`, `website`, socials
   - the same 4 past events are also plain `<a href>` links in the chapter HTML (`data-testid="data-block-for-pastEvents-…"`), so a link-follower gets the same 5 URLs (1 upcoming + 4 past for Belgrade)
3. **Event detail** — follow each `/events/details/<slug>/` link from step 2.

Counts on the probe date: GDG Belgrade 1 upcoming (DevFest in the Cloud, 2026-12-12) + 20 past; GDG on Campus University of Belgrade 2 upcoming (DevFest 2026 GameJam, 2026-12-03) + 21 past. Global chapter count 2,048; event volume is not enumerable globally without `/api/`.

## Fields

`GET /events/details/google-gdg-belgrade-presents-devfest-2026-conference/`, real values:

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `eventData.id` | `128909` | `source_id` | `gdg:128909` — numeric, stable, also in the JSON-LD `@id` fragment |
| `eventData.slug` / `relative_url` | `google-gdg-belgrade-presents-devfest-2026-conference` / `/events/details/…/` | `urls` | slug is `google-<chapter>-presents-<event>`; naive and can collide only on identical event names |
| `title` (JSON-LD `name`) | `DevFest 2026 Conference` | `title` | |
| `start_date` / `start_date_iso` | `2026-10-09T14:00:00Z` / `2026-10-09T16:00:00+02:00` | `starts_at` | both present; use `_iso` (already offset-local) |
| `end_date` / `end_date_iso` | `2026-10-09T17:15:00Z` / `2026-10-09T19:15:00+02:00` | `ends_at` | |
| `event_timezone`, `timezone_abbreviation` | `Europe/Belgrade`, `CEST` | `timezone` | IANA name published |
| `venue_name`, `venue_address`, `venue_city`, `venue_state` | `Naučno-tehnološki park`, `54 Veljka Dugoševića`, `Belgrade`, `None` | `location` | JSON-LD repeats them as `PostalAddress` incl. `postalCode 11050`, `addressRegion "Central Serbia"`, `addressCountry "RS"` |
| `audience_type`, `is_virtual_event`, `join_virtual_event_url` | `IN_PERSON`, `false`, `None` | `format` | JSON-LD `eventAttendanceMode` = `OfflineEventAttendanceMode` (`OnlineEventAttendanceMode`/`MixedEventAttendanceMode` expected otherwise — unverified) |
| `registration_required`, `rsvp_only`, `custom_tickets_url`, `use_external_ticketing`, `cohost_registration_url` | `true`, `true`, `""`, `false`, `https://gdg.community.dev/events/details/…/` | `registration_url` | registration is on the same page (`#/purchase`) or via `custom_event_url`; no deadline field |
| JSON-LD `offers` | `{"price": 0, "availability":"LimitedAvailability", "validFrom":"2026-09-03T15:10:38Z", "validThrough":"2026-10-09T17:15:00Z"}` | `registration_opens_at` / `registration_deadline` | `validFrom` looks like the registration opening; `validThrough` equals the event end, **not** a real deadline. Treat as inferred |
| `total_attendees` / `total_capacity` | `213` / `225` | — | also as `attendeeCount` on the page; useful for popularity, not in the schema |
| `tags` | `["AI - Gemma","Conference","DevFest","Workshop / hands-on session"]` | `themes` | free-form, chapter-authored |
| `event_type_title` | `Free registration` | — | pricing hint, not a price |
| `currency` | `EUR` | `prize.currency`? | present but there is no prize amount; the field is the ticket currency |
| `agenda` | `{"multiday":false,"days":[{"title":"Conference","agenda":[{"time":"4:00 PM","activity":"Check-in",…}]}]}` | — | per-day, per-slot times as **display strings** (`4:00 PM`), no dates |
| `chapter_id`, `chapter_title`, `chapter_url` | `3053`, `GDG Belgrade`, `https://gdg.community.dev/gdg-belgrade/` | `organizer` | JSON-LD instead gives `organizer`/`performer` as **Person** objects with `jobTitle` — e.g. `Vanja Milutinović, Managing Director`, `Marijana Hristić, Events Prorgam Manager` (sic), plus speakers (`Goran Minov` GDE, `Bianca Ximenes`) |
| JSON-LD `description` | 3 sentences | `summary` | the same text is `eventData.description` in HTML and `description_short` |
| `completed`, `is_hidden`, `hide_location`, `sharing_disabled` | `true`, `false`, … | `status` | `completed: true` for a past event |
| — | — | `team_size_*`, `prize`, `eligibility`, `difficulty`, `language`, `areas` | never published. Eligibility sometimes appears in the description prose (`Participants must be at least 18…`) |

Chapter-level fields (`chapterData`): `id 3053`, `city Belgrade`, `country RS`, `country_name Serbia`, `language`, `latitude`/`longitude`, `members_count`, `website`, `description` (HTML, mentions the chapter launched 2025-06-12), `parent_chapter_name` (`GDG on Campus` chapters have a parent).

## Revisits and identity

- `source_id` = `gdg:<eventData.id>` (`gdg:128909`). The slug is stable too but the numeric id is the safe key.
- Updates: `ETag` on HTML allows conditional GET on the *event* page; the chapter page's ETag changes whenever any event/description changes, so chapter-page ETag is a cheap "anything new?" check. No `Last-Modified`.
- Revisit an event page until `end_date`, then weekly: `completed` flips to `true`, `total_attendees` grows, and JSON-LD `offers.availability` can become `SoldOut`/gone.
- Finished events stay online at the same URL (the 4 rendered past events for the on-campus chapter go back to 2026-04-25 and are still reachable) — but the chapter page only links the **4 most recent past** events, so anything older must already be tracked.
- Results/winners: none. GDG publishes no placings, no project lists, no rosters. Speakers appear as JSON-LD `performer` (name + photo, no handles). `people` can only ever be speakers/organisers, never winners.

## Rate and failure behavior

- `Crawl-delay: 2` — all requests were spaced ≥ 2.1 s. ~20 requests, no 429, no 403, no challenge.
- Pages are heavy: chapter 730 KB, event page 1.39 MB (mostly Cloudinary image URLs and inline page-builder CSS/JS). Event page TTFB 0.54 s.
- 404 → a 45 KB themed page; 301/308 for slug variants, so follow redirects.
- The listing that the brief flagged (`/api/`) is disallowed and also returns internal-host URLs — never needed, since the same payload is in the HTML.
- Sitemaps are cheap to poll (`max-age 1800`, ETag) but contain no events; polling a chapter page is the real change check.

## Volume and relevance

| relevance | count today |
|---|---|
| Serbia | GDG Belgrade: 1 upcoming (DevFest in the Cloud 2026-12-12) + 20 past; GDG on Campus University of Belgrade: 2 upcoming (2026-12-03) + 21 past → **3 upcoming Serbian events**, ~5–8 a year per chapter |
| Europe | out of scope unless more chapters are configured; 2,048 chapters exist but only 2 are Serbian |
| Online | some chapters run virtual events (`is_virtual_event`), Belgrade's current ones are all `IN_PERSON` |

Low volume, high precision: every URL under `/events/details/` is a real event. Noise is the DevFest family being split into conference/hackathon/gamejam sub-events plus dev.events duplication — dedupe against `dev-events` and `hackathonhub`.

## Quirks

- `__NEXT_DATA__` on the chapter page leaks internal hostnames (`http://testserver/api/…`, `http://platform-google:80/api/…`); they are not fetchable externally, which incidentally enforces the robots rule.
- `pastEvents.count` (20) is the true total but only 4 are rendered; **there is no robots-allowed way to page the rest** (no past-events route, `?past=true` ignored, `/events/` is client-rendered, sitemaps carry no events). Historical backfill is capped at 4 per chapter page.
- The JSON-LD `organizer` is a `Person` (the chapter's managing director), not the chapter — use `chapter_title` for `organizer`.
- `offers.validThrough` equals the event end, so it is not a registration deadline; only `validFrom` is plausible as a registration opening.
- Event JSON-LD times are offset-local (`+02:00`) while `__NEXT_DATA__.start_date` is UTC — same instant, two encodings.
- Agenda slots are display strings (`4:00 PM`), in the chapter's local time, with no date attached.
- `gdg-novi-sad` is gone: 301 to `/`, absent from a 2,048-entry chapter sitemap. Novi Sad coverage has to come from elsewhere (Telegram/Luma/dev.events).
- Chapter sitemap has `<changefreq>weekly</changefreq>` but no `lastmod`, so it cannot drive incremental crawling.
- Trailing slash required (308 otherwise); event URLs in the embedded JSON miss the trailing slash, in JSON-LD and canonical form they have it.

## Config sketch

```toml
[[source]]
id          = "gdg-rs"
adapter     = "jsonld_pages"
interval    = "12h"
rate        = "25/min"
crawl_delay = 2
chapters    = [
  "https://gdg.community.dev/gdg-belgrade/",
  "https://gdg.community.dev/gdg-on-campus-university-of-belgrade-belgrade-serbia/",
]
link_pattern = "/events/details/"
discover     = "next_data.prerenderData.upcomingEvents"
past_links   = "html"
countries    = ["RS"]
render       = false
```

`render` is not needed: the chapter page and the event page both contain their payload server-side. Only `/events/` (the global listing) is client-rendered, and it is not needed for two chapters.
