# ICPC regionals (SEERC / CERC)

Probed 2026-11-15. Adapter: `icpc` (new, JSON). Kinds: contest.

The finder at `icpc.global/regionals/finder/<slug>` is a React SPA, but every fact it shows comes from **public JSON endpoints under `icpc.global/api/contest/public/`** with no auth and no key. Rendering is unnecessary: `curl <api>` returns the same payload the page receives. The WordPress sites (SEERC per edition, CERC per year) add prose — call text, schedule with venue rooms, rules — but no data the API lacks.

## Access

`https://icpc.global/robots.txt` → 200, **0 bytes** (S3, `last-modified: 2026-11-13`). Nothing disallowed, no crawl-delay. `https://seerc.euc.icpc.global/robots.txt` → 66 bytes, `User-agent: * / Crawl-delay: 10` (nginx), Last-Modified 2025-08-02.

Finder page (`/regionals/finder/SEERC-`): 1 HTML (200), 33 static assets, ~12 JSON calls, Google fonts, reCAPTCHA, Google Analytics, Mapbox/Carto tiles. Page title is set from the data (`The 2026 ICPC Southeastern Europe Regional Contest`). Cost of rendering ≈ 45 requests and a 7 MB geojson — **do not render**.

| endpoint | status | bytes | notes |
|---|---|---|---|
| `/api/common/globals/all` | 200 | 145 | `{"worldFinalsYear":2026,"regionalsYear":2027,"worldFinalsContestId":9150,…}` — `regionalsYear` is the current ICPC season |
| `/api/contest/public/<slug>` | 200 | 2.7–3.1 KB | **the main object**. `<slug>` is the finder slug: `SEERC-`, `SEERC-2026`, `central-europe`, `Central-Europe-2025` |
| `/api/contest/public/<slug>/schedules` | 200 | 1.7 KB | per-slot `scheduleDate` + `startTime`/`endTime` (`HH:MM:SS`) + `venue` |
| `/api/contest/public/regionalresults/id/<contestId>` | 200 | 338–429 B | team/school counts, external standings URL. **The `/id/` segment is mandatory**: `/api/contest/public/regionalresults/8961` returns `200` with a 0-byte body |
| `/api/contest/public/top3/<contestId>` | 200 | 2 B … 500 B | ranked **schools** (`{"id":2762,"name":"University of Zagreb","shortName":"Zagreb","rank":1}`); `[]` while the contest has no results |
| `/api/contest/public/<Abbr>/history` | 200 | 144–639 B | array of season codes: `["SEERC-2017",…,"SEERC-2027"]` |
| `/api/contest/public/subcontests/<slug>` | 200 | 1.1 KB | qualifying national contests under the regional (e.g. `AUCPC`, `Romania`, `TRCPC`) |
| `/api/contest/public/supercontests/<slug>` | 200 | 319 B | `Europe-2027`, `EUC-2027` |
| `/api/contest/public/contests-under/<id>` | 200 | ~1 KB | sub-contests with `date`, `registrationStart/End`, `capacity`, `registered`, `contact` |
| `/api/contest/public/organizers/<id>`, `/social/<id>`, `/contestPublicPhotos/<id>` | 200 | 2 B / small | `organizers` was `[]` for both probes (person data is off: `showPublicPagesType: "WITHOUT_PEOPLE"`) |
| `/api/contest/public/<id>/teams`, `/api/contest/public/all`, `/api/contest/public/contests/<abbr>` | **404** | 53 B | no team list, no global index endpoint |
| `/data/icpc-regional-geojson.json` | 200 | **7,099,755** | 301 features, one per country: `{countryName, regionalId, regional, continent}`. `regionalId` is the finder slug — this is the only machine-readable list of region slugs |

Error shape (404): `{"message":"No contest found for contest code CERC-"}` — 53 B, `cache-control: no-cache, no-store, max-age=0, must-revalidate`, `apigw-requestid`, `x-cache: Error from cloudfront`. **No `ETag`, no `Last-Modified`** on any API response. Latency 0.29–0.35 s per call.

EU regional slugs found in the geojson: `central-europe` (CERC), `northwestern-europe` (NWERC), `seerc`, `swerc`. Note the finder slug is *not* the contest abbreviation: `central-europe` → abbr `Central-Europe`; the `history` URL uses the abbreviation.

WP side, `seerc.euc.icpc.global`: `/wp-json/wp/v2/pages` 200 (28 pages, `X-WP-Total: 28`), `/wp-json/wp/v2/posts` 200 with `X-WP-Total: 0` (no posts at all), `/wp-sitemap.xml` 200 → one child `wp-sitemap-posts-page-1.xml` (28 URLs, each with `lastmod`), `/sitemap_index.xml` 404. REST responses carry no `ETag`/`Cache-Control: no-cache`.

## Discovery

1. **Regions** — `GET /data/icpc-regional-geojson.json` (7 MB, cache for weeks) or hardcode the 4 European slugs. There is no light-weight region list.
2. **Current contest** — `GET /api/contest/public/<slug>` with the *bare* slug (`seerc`, `central-europe`) or with a trailing dash (`SEERC-`). Both resolve to the contest of the current `regionalsYear`.
3. **All seasons** — `GET /api/contest/public/<Abbr>/history` → `["SEERC-2017" … "SEERC-2027"]` (11 for SEERC, 28 for Central-Europe, 1999–2027), then one `GET /api/contest/public/<code>` per season. Next season's code 404s until the contest is created (`SEERC-2028` → 404).
4. **Per-contest detail** — `schedules`, `regionalresults/id/<id>`, `top3/<id>`, `contests-under/<id>`, `subcontests/<slug>`.
5. **Prose** — the WordPress site is per edition and per host: 2026 SEERC is `seerc.euc.icpc.global/home-2026/…`, 2025 is `/home-2025/…`, CERC moves to a new university domain each year (`cerc24.ii.uni.wroc.pl` for 2025, `cerc26@cs.uni.wroc.pl` contact for 2026, `homepage` field empty). Enumerate via `wp-json/wp/v2/pages?per_page=100` on the current host; past hosts are only reachable through `regionalresults.url`.

Counts: 2 contests relevant to Serbia (SEERC — Serbia is inside its region; CERC — no), × 1 per season, + 14 European regions globally.

## Fields

`GET /api/contest/public/SEERC-` (2026 season), real values:

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `id` | `9565` | `source_id` | `icpc:9565` — numeric and stable across seasons/renames |
| `name` | `The 2026 ICPC Southeastern Europe Regional Contest` | `title` | calendar year in the name, season year in the code |
| `abbr`, `shortname` | `SEERC`, `ICPC Southeastern Europe RC` | — | `abbr` + `-` + season = the code |
| `startDate` / `endDate` | `2026-12-11` / `2026-12-13` | `starts_at`, `ends_at` | date-only, **no time**; the contest itself is day 3, 10:00–15:00 in `schedules` |
| `timezone` | `UTC` | `timezone` | misleading: the schedule is local site time (12:00 ceremony in Thessaloniki = UTC+2). Per-site tz not published |
| `regStartDate` / `regEndDate` | `2026-10-22` / `2026-11-20` | `registration_opens_at`, `registration_deadline` | date-only. **Conflicts with the SEERC site**, which says "Registration must be complete by November 15th 2026" |
| `regActive`, `closedForRegistration` | `true`, `false` | `status` | |
| `contestDelivery` | `ONSITE` | `format` | `ONSITE`/`ONLINE`/`HYBRID` |
| `contestType` | `REGIONALS` | `kind` → `contest` | |
| `geographicArea` | `Southeastern Europe`; CERC: `Austria, Croatia, Czech Republic, Hungary, Latvia, Poland, Slovakia and Slovenia` | `eligibility` | country list as prose; SEERC's list (from the WP `call` page) includes Serbia |
| `sites[]` | `{id, name:"Thessaloniki Greece", siteDate:"2026-12-13", siteType:"NORMAL", siteDelivery:"ONSITE", locationName:"University of Macedonia, Greece", address:{city, country:{a2:"GR",a3:"GRC",name:"Greece"}}, latLong:{40.625,22.9601}}` | `location`, `format` | one object per site — SEERC 2026 has Thessaloniki (GR) and Kyiv (UA, `Mykoly Shpaka St`, 50.458/30.430). An `ADMINISTRATIVE` site with `latLong 0,0` must be filtered out |
| `schedules[]` | `{eventName:"The Contest", scheduleDate:"2026-12-13", startTime:"10:00:00", endTime:"15:00:00", venue:"TBD"}` | — | 11 slots for SEERC 2026; `venue` is `TBD` or `Main Hall`. The WP `schedule` page gives the real rooms and the tz ("All times … Athens/Greece Time") |
| `regionalresults` | `{teams:94, schools:58, sites:2, externalStandingsUrl:"http://acm.ro/results.htm", url:"http://acm.ro/", results:null}` | — | counts only. `results` was `null` for **both** finished contests probed (SEERC 2024, CERC 2024) |
| `top3/<id>` | `[{"name":"University of Zagreb","rank":1},…]` | `people` (winner, place) | **schools, not teams or persons** |
| `homepage`, `problemsUrl`, `resultsUrl`, `reportUrl`, `sponsors`, `hosts` | mostly `""`/`null` | `website_url` | all empty for SEERC 2026; `regionalresults` has the useful external URLs for past seasons |
| `showPublicPagesType` / `showTeamsInPublicPages` | `WITHOUT_PEOPLE` / `true` | — | people are hidden by policy |

WP-only fields (SEERC 2026 tree, `home-2026` = page 2620, children `about`, `organization`, `call`, `call-for-problems`, `rules`, `schedule`, `venue`, `sponsors`):

| source | example | maps to | notes |
|---|---|---|---|
| `call` body | `Period: 11-13 December 2026. Contest: 13 December 2026.` | `starts_at`/`ends_at` sanity check | the only place with the contest day spelled out |
| `call` body | `Registration must be complete by November 15th 2026, online on the ICPC website` | `registration_deadline` | contradicts `regEndDate: 2026-11-20` |
| `call` body | coach clause: `A team may only have one registered coach` | `eligibility` | team size 3 + coach is ICPC-wide, **not stated on these pages** |
| `venue` | `University of Macedonia, 156 Egnatia str., PC 54636, Thessaloniki, Greece` | `location.address` | street address exists only here |
| `schedule` | `Saturday (December 12, 2026) 09:00 – 11:00 Contestants Registration …` | — | times + rooms + tz statement |
| `organization` | `Alexandros Spitalas — SEERC Director` etc. | `people` (organizer) | 4 names, no handles, no links |
| `sponsors` | `ICPC SEERC 2026 GLOBAL SPONSORS` | — | content is **raw Divi shortcodes** (`[et_pb_section …]`) even in `content.rendered` |
| `teams` (2025 tree only) | `EUC 2025 Teams` table: rank, team name, institution, country, `team05.tar.gz` | `people` (attendee) | team names + universities + countries, no person names. The page did not exist yet in the 2026 tree |

## Revisits and identity

- `source_id` = `icpc:<contestId>` (`icpc:9565`), plus `icpc:<contestId>:<siteId>` if each site is imported as its own event (recommended: Thessaloniki and Kyiv are different cities and both are in the SEERC region).
- Updates: no `ETag`/`Last-Modified` anywhere; poll `GET /api/contest/public/<slug>` (2.7 KB) once a day until `regEndDate`, then weekly. Compare `regActive`, `closedForRegistration`, `startDate`, and `schedules` length.
- Cancellations: no `eventStatus` field. A cancelled contest is expected to disappear from `<Abbr>/history` or get `active: false` in the subcontest list — unverified.
- **Finished event**: `top3/<id>` fills in with ranked schools, `regionalresults` gains `teams`/`schools`, and `externalStandingsUrl` points at the host's own scoreboard (`acm.ro/results.htm`). Nothing in the API marks a contest as finished; `endDate < today` is the only signal.
- Results and people, per source: teams and placings are **not** in the ICPC API (`results: null`, `WITHOUT_PEOPLE`, no `/teams` endpoint). They exist as (a) `externalStandingsUrl` under the host's domain, (b) the per-edition WP `teams` page (team names + institution + country), (c) `top3` schools. No GitHub logins, no person handles.
- Season-vs-calendar year trap: `SEERC-2025` *is* `The 2024 ICPC SEERC`, held 2024-11-16; `icpcYear` is the season (2025), `actualYear` the calendar year (2024). Titles must come from `name`, never from the code.

## Rate and failure behavior

- No rate limiting observed: ~25 API calls at 1.2 s spacing, all 200/404 as expected, latency 0.29–0.35 s. CloudFront in front (`x-amz-cf-pop: BUD50-P2`), `no-cache` on every response.
- Rendering the finder costs ~45 requests including a 7 MB geojson and third-party tiles; a headless browser in the worker buys nothing.
- Missing season → 404 with a JSON message; missing contest id → 404; wrong endpoint shape (`regionalresults/<id>` without `/id/`) → **200 with an empty body**, which is the one silent failure to guard against.
- Nearly all values are nullable strings (`""`/`null`) rather than absent keys.

## Volume and relevance

| relevance | count today |
|---|---|
| Serbia | SEERC is Serbia's region (12 countries incl. Serbia); 2026 edition Thessaloniki + Kyiv, 11–13 Dec 2026, reg deadline 15–20 Nov 2026 |
| Europe | 4 European regions × current season + 28 CERC + 11 SEERC historical seasons |
| Online | 0 (`ONSITE` both sites) |

Relevance is high but volume is tiny: 1–2 importable contests per season for Serbia, and only cycle-to-cycle (SEERC once a year in December, CERC in January). Noise: none, if the `ADMINISTRATIVE` site and the `unofficial` site are dropped.

## Quirks

- Finder slug ≠ contest abbreviation: `central-europe` vs `Central-Europe`; `history` needs the abbreviation, the finder needs the slug.
- `regionalresults/id/<id>` works, `regionalresults/<id>` returns 200 with 0 bytes.
- `timezone: "UTC"` on every contest while all published times are local site time; the WP schedule says "Athens/Greece Time".
- Registration deadline differs between the API (`2026-11-20`) and the call page (`November 15th 2026`).
- Contest names carry the calendar year, codes carry the season year (`Central-Europe-2026` = "The 2026 ICPC Central Europe", held 8–10 Jan **2027**).
- `organizers/<id>` returns `[]` even though the WP `organization` page lists four named directors.
- WordPress `content.rendered` is not uniformly HTML: the SEERC 2026 `sponsors` page returns Divi shortcodes verbatim.
- Past CERC sites rotate domains (`cerc24.ii.uni.wroc.pl`), so the WP adapter can only follow the current edition; historical prose is unretrievable through the API.
- `SEERC-` (trailing dash) and `seerc` both work and both mean "current"; the finder URL and the API path are interchangeable.

## Config sketch

```toml
[[source]]
id          = "icpc"
adapter     = "icpc"
interval    = "24h"
rate        = "10/min"
base        = "https://icpc.global/api/contest/public"
regions     = ["seerc", "central-europe", "northwestern-europe", "swerc"]
season_url  = "https://icpc.global/api/common/globals/all"
sites       = "separate"
drop_site_types = ["ADMINISTRATIVE"]
wp          = "https://seerc.euc.icpc.global"
wp_pages    = ["call", "schedule", "venue", "rules", "organization"]
```

Rendering is not needed: a JSON adapter replaces both the finder and the WordPress HTML for dates, registration windows, sites and schedule.
