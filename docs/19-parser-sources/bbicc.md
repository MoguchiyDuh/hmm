# BBICC (Belgrade Business International Case Competition)

Probed 2026-11-15. Adapter: `jsonld_pages` (the edition page carries Event JSON-LD). Kinds: contest.

One event a year, organised by the Faculty of Organisational Sciences (FOS), University of Belgrade. 12th edition: **7–13 March 2027, Belgrade**. Site is WordPress + Astra + Elementor widgets; every page is reachable through the WP REST API, and the edition page carries a hand-written `schema.org/Event` block.

## Access

| URL | status | bytes | notes |
|---|---|---|---|
| `/robots.txt` | 200 | 119 | `User-agent: * / Disallow: /wp-admin/ / Allow: /wp-admin/admin-ajax.php` + `Sitemap: https://bbicc.fon.bg.ac.rs/wp-sitemap.xml`. No crawl-delay |
| `/wp-sitemap.xml` | 200 | 296 | index with exactly one child: `wp-sitemap-posts-page-1.xml` |
| `/wp-sitemap-posts-page-1.xml` | 200 | 1,374 | 11 `<url>` entries with `lastmod` (12 pages exist; `coming-soon` is excluded) |
| `/wp-json/` | 200 | 418,941 | namespace discovery; `wp/v2` exposed anonymously |
| `/wp-json/wp/v2/pages` | 200 | 2.5 KB (slim) / 1,257,228 (all content) | `X-WP-Total: 12`, `X-WP-TotalPages: 1` |
| `/wp-json/wp/v2/posts` | 200 | 2 | `[]`, `X-WP-Total: 0` — **the site has no posts** |
| `/wp-json/wp/v2/pages?modified_after=<iso>` | 200 | — | works: `modified_after=2026-11-09T00:00:00` → 9 of 12 pages |
| `/`, `/bbicc-2027/` | 200 | 218,484 / 221,994 | HTML; `Link: <…/wp/v2/pages/55>; rel="alternate"; title="JSON"` in the headers gives the REST URL for any page |
| `/coming-soon/`, `/history/`, `/contact/`, `/about/`, `/about/*`, `/bbicc-2027/faq/` | 200 | 1.4 KB … 636 KB | all 12 pages |

No `ETag`, no `Last-Modified`, no `Cache-Control` on HTML **or** on REST responses (verified on `/`, `/bbicc-2027/`, `/wp/v2/pages`). `Server: FON`. Conditional GET is not possible; use REST `modified_after` for incremental pulls.

Page inventory (`id`, `parent`, slug, `modified`):

| id | parent | slug | modified |
|---|---|---|---|
| 55 | — | `home` | 2026-11-10 |
| 79 | — | `contact` | 2026-11-10 |
| 120 | — | `about` (+ `organizers` 290, `about-serbia` 291, `fos-case-achievements` 292, `testimonials` 293, `student-ambassadors` 908) | 2026-11-10 |
| 122 | — | `bbicc-2027` (+ `faq` 263) | 2026-11-10 |
| 124 | — | `history` | 2026-11-10 |
| 809 | — | `coming-soon` | 2026-10-26 |

## Discovery

`GET /wp-json/wp/v2/pages?per_page=100&_fields=id,slug,link,parent,modified,title` → 12 rows, one request, 2.5 KB. There is no listing/pagination to speak of and no filtering needed.

The edition page is identified by slug `bbicc-<year>` (`bbicc-2027` today). Next year the page will presumably be `bbicc-2028`; nothing links forward, so a parser must either poll the sitemap or accept that the edition URL changes. There is exactly one live event page at a time — `coming-soon` (1,373 B, "Coming soon...") is what sits at the slot before content lands.

`?modified_after=` gives change detection; the sitemap has no `lastmod`, so it is useless for that.

## Fields

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `id` | `122` | `source_id` | `bbicc:page/122` — but the id changes per edition page, so key on the edition: `bbicc:<year>` |
| JSON-LD `Event.name` | `BBICC 2027: Belgrade Business International Case Competition` | `title` | hand-written block on `/bbicc-2027/` only (home carries Organization + WebSite, no Event) |
| JSON-LD `startDate` / `endDate` | `2027-03-07` / `2027-03-13` | `starts_at`, `ends_at` | **date-only, no time, no timezone** — midnight must be assumed and `Europe/Belgrade` inferred |
| page prose | `From 7th to 13th of March 2027` | — | prose confirms the JSON-LD; `og:title` says `BBICC 2027 \| 7–13 March 2027 in Belgrade, Serbia` |
| JSON-LD `location` | `Belgrade, Serbia`, `addressLocality: Belgrade`, `addressCountry: RS` | `location` | no street address in JSON-LD; `/contact/` gives `Faculty of Organizational Sciences, Jove Ilića 154, Belgrade, Serbia` |
| JSON-LD `eventAttendanceMode` | `https://schema.org/OfflineEventAttendanceMode` | `format` | `offline` |
| JSON-LD `eventStatus` | `EventScheduled` | `status` | |
| JSON-LD `organizer` | `{"@id": "https://bbicc.fon.bg.ac.rs/#organization"}` | `organizer` | resolves to the `Organization` node on `home` (name `Belgrade Business International Case Competition`, `foundingDate 2013`, `addressCountry RS`, social `sameAs`) |
| page prose | `twelfth edition`, `Three real-world business cases` | — | edition number + format in the body |
| `/bbicc-2027/faq/` | `We allow four undergraduate students to participate` / `Each team must have at least one Advisor` | `team_size_min`/`max` = 4, `eligibility` | **4 undergraduates + ≥1 advisor.** FAQ answers 11 questions (case copies, presentation software, clicker, laptop, supplies, advisor observation rules) |
| `/contact/` | `Managing Director Miloš Milosavljević milos.milosavljevic@fon.bg.ac.rs` + 5 more | `people` (organizer) | 6 named organisers with e-mail addresses, no handles, no links |
| `/history/` | `Winner of BBICC 2026 — Amdo Villemonteix, Erasmus University Rotterdam` | `people` (winner, place) | testimonial attributions only: `BBICC 2026` ×3, `BBICC 2025` ×2, `BBICC 2024` ×2 — these are **individual winners, not the team name** |
| `/about/fos-case-achievements/` | `2026 — 1st place T.I.M.E.S Case Competition, Lisbon, Portugal (September 2026)` | — | FOS teams' results in *other* competitions, year by year, 2024–2026 |
| — | — | `registration_url`, `registration_deadline`, `registration_opens_at`, `prize`, `themes`, `language`, `difficulty`, `summary` | **none published anywhere.** No apply/register link on any page, no fee, no deadline, no prize. `og:description` and the JSON-LD `description` are the only summary-shaped text |

## Revisits and identity

- `source_id`: `bbicc:<year>` (the page id changes with the edition page).
- Updates: `modified` per page via `?modified_after=`; no `ETag`. Poll the page list (2.5 KB) daily, fetch content only when `modified` moved.
- Cancellation: no mechanism observed; `eventStatus` only ever `EventScheduled`. A cancelled edition would presumably leave a stale page.
- Finished event: nothing changes on the edition page — no "completed" flag, no results section, no winner page. The only trace of an edition finishing is a new testimonial block appearing on `/history/` with the year attached.
- Results/winners: **weak.** `/history/` names one winning *person* per edition inside testimonials (2026, 2025, 2024 only) and shows a world map of participating universities. There is no ranking table, no winner list per edition, no participant roster. The site has zero posts.
- Because a team of 4 + advisor competes per university, `team_size_min = team_size_max = 4` is safe and stated.

## Rate and failure behavior

- No rate limiting, no cookies, no auth; 13 requests at 1.2 s spacing, all 200. HTML latency 0.5 s; the all-pages JSON pull is 1.26 MB in one request.
- 404s return the theme's own 404 page (not JSON) even under `/wp-json/wp/v2/pages/<bad-id>` — verify the content type, not just the status.
- No conditional-request support at all: no `ETag`, no `Last-Modified`, no `304` path.
- `/wp-json/wp/v2/pages/<id>` returns a `meta` object, but it holds only Astra layout keys (`site-sidebar-layout`, `ast-*`) — no event fields.

## Volume and relevance

| relevance | count today |
|---|---|
| Serbia | 1 (Belgrade) |
| Europe / global | 0 additional — the *participants* come from 20 universities worldwide, but there is one edition |
| Online | 0 |

One importable contest per year, no noise. High value (Serbian, prestigious, undergraduate, exactly the `contest` kind), tiny volume. Worth listing as a low-frequency seed rather than a frequently crawled source.

## Quirks

- 636 KB `history` page and 31 KB `bbicc-2027` body are mostly **inline JS + CSS for scroll animations** (an envelope that opens as you scroll, a testimonials carousel, an SVG world map with clustered pins) — the interesting text is a few hundred bytes among megabytes. Strip `<script>/<style>` before extracting, or take the WP REST `content.rendered` and strip the same.
- The Event JSON-LD has no `offers`, no time, no timezone — date-only, midnight implied.
- Elementor/Astra markup means the semantic structure is `div` soup; only JSON-LD and the FAQ headings are reliably addressable.
- No registration link, no deadline, no fee, no prize anywhere on the site; participation is by invitation from the organiser.
- `fos-case-achievements` (120 KB) mixes BBICC with FOS's results at other case competitions — do not mistake it for a BBICC results page.
- `coming-soon` (1,373 B) is a placeholder that may briefly become the top-level event page between editions.

## Config sketch

```toml
[[source]]
id          = "bbicc"
adapter     = "jsonld_pages"
interval    = "24h"
rate        = "10/min"
api         = "https://bbicc.fon.bg.ac.rs/wp-json/wp/v2/pages"
detail      = "https://bbicc.fon.bg.ac.rs/bbicc-{year}/"
incremental = "modified_after"
use_jsonld  = true
default_tz  = "Europe/Belgrade"
team_size   = 4
```
