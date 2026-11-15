# Hackathon Hub (hackathonhub.eu)

Probed 2026-11-15. Adapter: `hackathonhub` (the sitemap works but is the least useful
of the available discovery paths). Kinds: hackathon, contest (challenges, game jams).

There is
**no Serbia page**. `/hackathons/serbia` is an unprerendered SPA shell that the client-side router
redirects to `/hackathons/germany`; and the sitemap's 8 Serbian URLs are 7 finished events plus 1
upcoming. Live data holds 9 Serbian events total (1 upcoming), not 8.

## Access

| URL | method | status | size | notes |
|---|---|---|---|---|
| `/robots.txt` | GET | 200 | 5,143 B | `*`: allow all, disallow `/admin /auth /review /partners /partner /profile /reset-password /unsubscribe /newsletter /setup-2fa /verify-2fa /pitch /pitch-2 /one-pager /linkedin-*`, `ambassador-*`, `beratung-checkliste`. No `Crawl-delay` |
| `/sitemap.xml` | GET | 200 | 354 B | index → `sitemap-pages.xml`, `sitemap-events.xml` (both `lastmod 2026-11-11`) |
| `/sitemap-events.xml` | GET | 200 | 257,859 B | 1,231 `<url>` |
| `/sitemap-pages.xml` | GET | 200 | 23,630 B | 137 `<url>` |
| `/events` | GET | 200 | 20,096 B | prerendered + ItemList JSON-LD |
| `/hackathons/<slug>` | GET | 200 | 24–280 KB | prerendered guide pages only (see Discovery) |
| `/events/<slug>` | GET | 200 | 8.4–9.1 KB | one `Event` JSON-LD block |
| `/events/<slug>.md` | GET | 200 | 1,064–1,085 B | `text/markdown`, YAML front matter + body; `etag`, no `Last-Modified` |
| `/events/<unknown-slug>.md` | GET | **404** | 9 B | reliable deletion signal |
| any unknown path | GET | **200** | 6,715 B | SPA shell — soft 404, indistinguishable from a real page |
| `czcrgiykicowicoufthv.supabase.co/rest/v1/events_public` | GET | 200 / 206 | 2.9 KB/row | the SPA's own backend; anon JWT is in `/assets/index-DhDhOlAv.js` |

Cloudflare in front (`__cf_bm` cookie, `cf-ray`, HTTP/2), `cache-control: no-cache, must-revalidate`
on HTML, `strict-transport-security: max-age=31536000`. No auth for HTML/Markdown/sitemaps.

Three data layers exist: prerendered SEO HTML, the SPA shell for everything else, and an
undocumented PostgREST view (`events_public`, 61 columns) that the SPA queries with a public anon
key. robots.txt does not cover the Supabase host; that endpoint is undocumented and can change or
rotate the key without notice — treat it as a bonus, not the contract.

## Discovery

Selection order of cheapness observed:

1. **`/events/<slug>.md`** is the cheapest fetch (≈1 KB) but needs a slug list first.
2. **`/events` ItemList JSON-LD** is *incomplete*: `numberOfItems: 431` but only 25 `itemListElement`
   entries. `?page=2` returns byte-identical HTML (20,096 B), so paging is client-side only.
3. **Guide pages' ItemList JSON-LD** is complete per guide, and is the only "listing" that enumerates
   all items server-side: `/hackathons/ai` → `numberOfItems: 170` with 170 items,
   `/hackathons/germany` → 67, `/hackathons/berlin` → 18, `/hackathons/blockchain` → 9. Guides are
   stale snapshots of the 2026-11-11 build (Germany prerendered 67 upcoming vs 46 live). Only
   **9 country guides** (germany, austria, switzerland, united-kingdom, france, spain, italy,
   netherlands, finland) and **20 city guides** are prerendered; **`/hackathons/serbia` is not one**,
   and `https://hackathonhub.eu/hackathons/qwertyuiop-zzz` returns the same 6,715 B shell, so the
   route set cannot be probed by status code.
4. **`sitemap-events.xml`** — 1,231 URLs, superset of the guide ItemLists. Per-URL `<lastmod>` is a
   build date, not a modification date: values are only 2026-11-08 (2), -09 (4), -10 (529),
   -11 (696), and **never** equal the row's `updated_at` in the API (0/1,231 matches; the API shows
   552 rows updated on 2026-11-15 alone). `changefreq`/`priority` do encode state at build time:
   `weekly`/`0.6` = 431 upcoming, `yearly`/`0.3` = 800 old. 8 of the 1,231 URLs no longer exist in the
   API, and the one tested (`energy-data-visualisation-hackathon-brussels-2026`) 404s on `.md`.
   572 published events are missing from the sitemap entirely.
5. **PostgREST `events_public`** — the only complete, live, filterable view: `?country=eq.RS`,
   `?end_date=gte.2026-11-15`, `?updated_at=gte.<ts>`, `?select=`, `Prefer: count=exact` →
   `content-range: 0-8/9`. Caps at 1,000 rows per response (`Range: 1000-1999` for the second page).

Counts on the probe date:

| scope | count | source |
|---|---|---|
| published events in `events_public` | 1,795 | REST count |
| of those, upcoming (`end_date ≥ today`) | 444 | REST count |
| listed as "upcoming" by `/events` | 431 | ItemList (built 11-11) |
| sitemap URLs | 1,231 (370 upcoming, 853 past, 8 deleted) | cross-joined |
| Serbia, all | 9 | `country=eq.RS` |
| Serbia, upcoming | 1 (`dev3pack-global-hackathon-belgrade-2026`, 27–29 Nov 2026) | `country=eq.RS&end_date=gte.today` |
| Online, upcoming | 14 | `location_type=eq.online` |

The 8 Serbian sitemap URLs: 5 Belgrade, 2 Novi Sad, 1 Niš (`tech-for-social-good-hackathon-nis-2026`).
Seven of them ended between 22 Aug and 8 Nov 2026, i.e. before the probe date. One Serbian event
(`bio4ai-hackathon-at-belbi-2026-belgrade-2026`, 9–10 Jun 2026) is in the API but not in the sitemap,
so the sitemap is not even a complete historical index.

## Fields

`.md` front matter (11 keys) is the compact canonical record; the API view adds structured prize
currency, deadlines and logistics booleans; JSON-LD adds nothing the other two lack.

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `slug` | `dev3pack-global-hackathon-belgrade-2026` | `source_id` | `hackathonhub:<slug>`; slug is `title-city-year`, city sometimes doubled (`…-novi-sad-novi-sad-2026`) |
| `title` (md/API), `name` (JSON-LD) | `Dev3pack Global Hackathon` | `title` | `title_orig`/`title_de` also present in the API |
| `start_date` / `startDate` | `2026-11-27T00:00:00+00:00` | `starts_at` | always `+00:00`; date-only events are `00:00:00` |
| `end_date` / `endDate` | `2026-11-29T23:59:00+00:00` | `ends_at` | same-day events exist (`cursor-hackathon…` 22 Aug 00:00→00:00) |
| `application_deadline` / `offers.validThrough` | `2026-11-01T23:59:00+00:00` | `registration_deadline` | present on 110/444 upcoming rows in the API; `validThrough` only on some JSON-LD pages |
| `location_type` / `eventAttendanceMode` | `hybrid` / `MixedEventAttendanceMode` | `format` | `online`→online, `onsite`→offline, `hybrid`→hybrid |
| `city`, `address`, `state`, `country` / `location.address` | `Belgrade`, `RS` | `location` | JSON-LD has `addressCountry: "RS"` + `addressLocality` and repeats the city in `Place.name`; no IANA timezone anywhere (`timezone` must be inferred: `Europe/Belgrade`, `Europe/Berlin`…) |
| `prize_money` + `prize_money_currency` + `prize_money_eur` | `10000` / `USD` | `prize` | 77/444 upcoming rows have an amount, 126 have a currency (EUR 93, USD 18, GBP 10, CHF 3, SEK 1, CZK 1) with `prize_money_eur` already converted. JSON-LD `offers.price` is always `"0"`; `.md` gives a formatted string (`$10,000.00`, `N/A`) |
| `type` | `hackathon`/`competition`/`challenge`/`gamejam` | `kind` | 390/19/16/19 of the upcoming rows; map `competition`+`challenge`→`contest`, `gamejam`→`contest` |
| `level` | `open`/`student`/`professional`/`school` | `eligibility` | `student` → "students only"; 75/444 |
| `max_team_size` | `4` | `team_size_max` | only 13/444 rows; no minimum anywhere |
| `tags` | `["blockchain","web3"]` | `themes` | vocabulary mirrors the `/hackathons/<theme>` guide slugs |
| `language` | `en` | `language` | single code; `en` 365, `de` 23, `fr` 17, `it` 11, `es` 6 |
| `organizer_name` / `organizer.name` | `GDG Belgrade` | `organizer` | `organizer.url` is the **registration/event** URL (luma.com, gdg.community.dev), not the org site; `performer` duplicates the organizer |
| `description`, `description_en`, `description_de` | 1–3 paragraphs | `description_md` | JSON-LD and `.md` carry the English text; German original in `description_orig` |
| `url` (md/API), `offers.url` | `https://hack.dev3pack.xyz/` | `registration_url` | canonical `https://hackathonhub.eu/events/<slug>` → `website_url` |
| `registration_status` | `open`/`closed` | — | gates the site's own visibility rules |
| `offers.availability` | `SoldOut` | — | registration closed; not a `status: cancelled` signal |
| `eventStatus` | `EventScheduled` | `status` | only value seen; cancellations disappear (`.md` 404) rather than flip |
| `travel_costs_covered`, `accommodation_provided`, `meals_included`, `has_overnight_stay_on_site`, `beginner_friendly`, `mentoring_available`, `on_site_hardware_provided`, `team_formation_supported` | `false` | — | no schema field; drop or fold into `description` |
| `expected_participants`, `reference_number`, `prize_money_eur`, `discord_thread_id` | — | — | site bookkeeping |
| — | — | `people`, `difficulty`, `registration_opens_at`, `areas` | never published |

## Revisits and identity

- Identity: `hackathonhub:<slug>`. The slug is content-derived, so a title edit mints a new id and
  orphans the old one (rename or delete + import is unavoidable).
- **Change detection**: `.md` responses carry an `etag` but ignore `If-None-Match` — a conditional
  GET returned `200` with the full body and the same etag. Poll the etag and skip re-parsing when it
  is unchanged.
- The only real per-item timestamps are in the API: `updated_at` (crawler-side, moves often — 552 rows
  changed on the probe day) and `created_at`. `?updated_at=gte.<iso>` with `Range` paging is the
  incremental path. Sitemap `lastmod` must not be used for this.
- **Deletion**: URL disappears from the sitemap *and* its `.md` returns 404. HTML alone cannot tell
  you (200 shell for everything), so look-ahead on the 404 is the only cancellation signal.
- Finished events look identical to live ones in JSON-LD except for dates; the site keeps ≈1,350
  past events in the API and 853 in the sitemap (back to 2020-09-02).
- No winners, results, attendee lists or speaker names anywhere — `people` is never populated.
  `/leaderboards` is a user gamification page, not event results.

## Rate and failure behavior

- ≈48 requests at ≥1 s spacing over 25 min: no 429, no challenge, no `cf-mitigated`; 0.10–0.18 s
  TTFB. Cloudflare bot management issues `__cf_bm` but served the custom UA fine.
- No rate-limit headers at all; per-domain limiting must be self-imposed.
- PostgREST: 1,000-row default cap, `Range` accepted, `Prefer: count=exact` returns `206` with
  `content-range: */1795`. A malformed anon key returns `401 sb-error-code: UNAUTHORIZED_INVALID_API_KEY`.
- 404 only for `.md` of deleted events (9 B body). Everything else is 200, including
  `/events/this-does-not-exist-2026` and `/hackathons/qwertyuiop-zzz`, so status-code-driven crawlers
  will silently collect shells. Detect by `Content-Length` ≈ 6,715 B.

## Volume and relevance

| scope | upcoming |
|---|---|
| all | 444 |
| Europe (single EU/EEA code; DE 85, GB 75, FR 37, IT 29, ES 28, NL 26, SE 17, CH 17, GR 8, …) | 421 |
| non-European: UA 9, TR 12, VA 1, dirty `"BG, MT"` 1 | 23 |
| RS | 1 |
| RO / HU / BG / MK / ME | 16 / 4 / 3 / 1 / 1 |
| online | 14 |
| onsite / hybrid | 374 / 56 |

Serbia is the weak spot: 1 upcoming event. Value is as a European backbone (Romania 16, Greece 8,
Hungary 4, Bulgaria 3, plus every DACH/UK event), so keep it for dedupe against MLH/Hackalendar/
HackathonRadar and for Balkan coverage, not for Serbian volume. Noise: `type=challenge`/`competition`
rows that are awards or pitch contests (e.g. "Federal Ecodesign Award", "2026 Imagine Cup
Competition", "Venture Cup") still count as `contest` but are not team coding events — the LLM
classification step has to drop those.

## Quirks

- **No Serbia page**: `/hackathons/serbia` → 200, 6,715 B shell; rendered in a browser the SPA lands on
  `/hackathons/germany` ("46 upcoming hackathons in Germany"). Unknown guide slugs are not 404s.
- Guide pages carry stale prerendered ItemLists (Germany 67 → 46 live in 4 days); never treat an
  ItemList as the current set.
- `/events` ItemList claims `numberOfItems: 431` but ships 25 items; `?page=` is ignored server-side.
- `/events?country=RS` does work client-side (one card, "1 upcoming hackathons across Europe"), but
  only after hydration — the prerendered HTML for any filter is the unfiltered page, and the count
  even starts at 0. It resolves its query to
  `start_date >= now AND (application_deadline IS NULL OR >= now) AND
  (registration_status IS NULL OR != 'closed') AND location_type != 'online'`, i.e. it hides events
  whose registration closed and every online event. Wrong rules for a catalog; use the API with
  `country=eq.RS` instead.
- Dates are tz-naive-in-practice: everything is `+00:00`, date-only events sit at `00:00:00`. No IANA
  zone in any layer.
- `offers.price` is always `"0"` and `priceCurrency` always `EUR`, so price/prize must come from the
  API or the formatted `.md` string.
- `offers.availability: SoldOut` is common on already-finished events and means "registration shut",
  not "cancelled".
- The API's `location_type` (`hybrid`) and JSON-LD's `MixedEventAttendanceMode` agree, but the `.md`
  `format: hybrid` is the field to trust.
- `additional_locations` is an array of `{country: "RS", …}` objects (visible in the SPA's own query,
  `additional_locations.cs.[{"country":"RS"}]`) and holds extra venue countries. Multi-city series are
  also split into one slug per city (Dev3pack: Belgrade, Paris, Bursa, Çanakkale, Kyiv — five slugs,
  one programme), so dedupe has to merge both shapes.
- `events_public` rows include full-page marketing text (`description` 1–3 KB) already in the source
  language plus `_en`/`_de` variants; pick `description_en` for `description_md`.
- `country` is not a clean enum: 12 upcoming rows are `TR`, 9 `UA`, 1 `VA`, and one row literally
  contains `"BG, MT"`.
- 8 sitemap URLs are stale (no such slug in the API); the one tested returns 404 on `.md` — the
  sitemap is not pruned promptly.

## Config sketch

```toml
[[source]]
id           = "hackathonhub"
adapter      = "hackathonhub"
interval     = "12h"
rate         = "30/min"
discover     = "https://hackathonhub.eu/sitemap-events.xml"
detail       = "https://hackathonhub.eu/events/{slug}.md"
api_base     = "https://czcrgiykicowicoufthv.supabase.co/rest/v1/events_public"
api_key_env  = "HACKATHONHUB_ANON_KEY"
incremental  = "updated_at.gte.{cursor}"
countries    = ["RS", "RO", "HU", "BG", "HR", "BA", "SI", "MK", "ME", "AL", "GR"]
default_tz   = "Europe/Belgrade"
```
