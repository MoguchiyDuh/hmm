# Hackathonist

Probed 2026-11-15. Adapter: `hackathonist`. Kinds: hackathon.

Curated Serbian hackathon list by Željko Šević (`zsevic`), MIT, branch `gh-pages` (a Jekyll site:
`raw.githubusercontent.com/zsevic/hackathonist/gh-pages/{README,2017…2025}.md`, rendered at
`https://zsevic.github.io/hackathonist/`). Upcoming + recent events live only in `README.md`;
finished years are frozen in `<year>.md`. There is no `2026.md` (404) — 2026 lives in `README.md`.

## Access

| URL | method | status | size | notes |
|---|---|---|---|---|
| `raw.githubusercontent.com/zsevic/hackathonist/gh-pages/README.md` | GET | 200 | 2,001 B | `etag`, `cache-control: max-age=300`, no `Last-Modified` |
| same with `If-None-Match: "<etag>"` | GET | 304 | 0 B | conditional revalidation works |
| `…/2017.md` … `…/2025.md` | GET | 200 | 480–5,387 B | each has its own `etag` |
| `api.github.com/repos/zsevic/hackathonist/git/trees/gh-pages` | GET | 200 | 4.3 KB | anonymous, `X-RateLimit-Limit: 60` |
| `api.github.com/repos/…/commits?per_page=N[&path=README.md]` | GET | 200 | 42 KB / 10 commits | `path=` filter returned **422** "request is taking too long to process" |
| `github.com/zsevic/hackathonist/commit/<sha>.patch` | GET | 200 | 0.7–1.5 KB | unified diff of one commit, no API quota |
| `github.com/zsevic/hackathonist/commits/gh-pages.atom` | GET | 200 | 14 KB | 20 entries, `<updated>` per commit |

No auth needed. `github.com/robots.txt` allows the paths used (only `/search`, `/gist`, `/account-login` etc. disallowed); it has `Crawl-delay: 1` for named AI crawlers, not for our UA. `raw.githubusercontent.com/robots.txt` is **404** (no robots file). Response is `text/plain; charset=utf-8`; there is no HTML wrapper to render — parse Markdown directly. MIT licence permits reuse with attribution.

## Discovery

- Repo root tree on `gh-pages` is flat and small: 9 year files (`2017.md`…`2025.md`), `README.md`, `CONTRIBUTING.md`, `LICENSE`, `_config.yml`, `_layouts/`, `favicon.svg`, `Gemfile`, `Setup.md`, `.gitignore`. 20 entries, `truncated: false`.
- `README.md` has exactly two tables: `#### :boom: Upcoming hackathons` and `#### :computer: Recent hackathons`, plus a promolink line and a `2025.md` link. Year files have one table under `#### :white_check_mark: <year>`.
- Table shape is fixed: `| Hackathon | Location | Date |`, separator row, one row per event:
  `| [<title>](<url>) | <location> | <date text> |`.
- Row counts observed: 2017 29, 2018 44, 2019 37, 2020 16, 2021 22, 2022 26, 2023 22, 2024 6, 2025 2, README (2026) 15. Total 219 rows. 2024–2026 volume collapsed versus 2018 (`6`, `2`, `15`), so a full historical backfill is ~220 events, a recurring crawl adds ≤ 1–3 a month.
- Only filter is the table's `Date` column: to get upcoming, take rows from "Upcoming" and any row whose end date is in the future. No paging, no API, no sitemap.

Cheap change detection (no API quota): poll `commits/gh-pages.atom`, stop when the newest `<id>` matches the stored one; then diff `README.md` via `If-None-Match`. When a new commit exists, fetch `<sha>.patch` (a few hundred bytes) instead of the whole file — rows are added as one-line insertions and the "move to Recent" edit is two deletions plus two insertions.

## Fields

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| link text | `Frontier Hackathon` | `title` | free text, mixed Serbian/English/Latin/Serbian-Cyrillic (`Има на ћирилици`), typos (`ABB Robo Challange`, `DesCon`/`DESCON`) |
| link href | `https://hack.garaza.org/` | `website_url` | often the organiser's landing page or a Google Form / Facebook event / Instagram post, not an event page |
| Location cell | `Belgrade`, `Startit Centar, Belgrade`, `online`, `Belgrade/online`, `Online/Rivian HQ`, `UVC Šumice`, `  ` | `location.city` + `location.address`, `format` | venue-level free text, no country ever. Only city name where the venue is omitted. `online` / `…/online` marks online or hybrid; empty cell means unknown, not online |
| Date cell | `6-7 November 2026`, `12th September 2026`, `24th April - 29th May 2026`, `27th November 2021`, `March 2020`, `` | `starts_at`, `ends_at` | see below |
| — | — | `timezone` | absent; use `Europe/Belgrade` for offline rows, UTC for online-only. No start times |
| — | — | `registration_deadline`, `team_size_*`, `prize`, `description_md`, `themes`, `language`, `people` | absent from every row |
| section heading | `:boom: Upcoming hackathons` | `status` hint | no cancelled marker anywhere: a cancelled event is simply deleted |
| `2017`-`2025` file name | `2023.md` | — | year of the event, useful as a sanity check on the date parsing |

Date text grammar (all formats observed across 219 rows):

| pattern | example | rows (of 219) |
|---|---|---|
| `D-D Month YYYY` | `6-7 November 2026` | 176 |
| `Dth Month YYYY` | `12th September 2026`, `1st December 2023` | 23 |
| `D Month - D Month YYYY` | `31 May - 2 June 2024` | 12 |
| `Dth Month - Dth Month YYYY` | `29th November - 1st December 2022` | 2 |
| `Month YYYY` | `March 2020` | 1 |
| `D Month YYYY` | `29 February 2020`, `10 June 2021` | 2 |
| cross-year range | `28 December 2020 - 4 January 2021` | 1 |
| `` (empty) | `Srbija hakuje Koronu`, `Game jam` | 2 |

`starts_at` = first day 00:00 local, `ends_at` = last day 23:59:59 local (single-day rows get the same day). Two rows have no date at all (`Srbija hakuje Koronu`, `Game jam`) and both also lack a location — treat as non-importable. Serbian month names never appear — months are English. Year mismatches exist: `2019.md` contains `| … | 23rd November 2018 |` (copy-paste of the 2018 row), and `2021.md` contains `28 December 2020 - 4 January 2021`. Prefer the in-text year and only fall back to the file name.

Location free-text values that matter for relevance: Belgrade (and `Beograd`), Novi Sad, Niš, Subotica, Šabac, Čačak, Kragujevac, Pančevo, Inđija, Zlatibor, Novi Pazar, Kovačica, `online`, `virtual event`. Everything else is a venue inside one of those.

## Revisits and identity

- No ids. Stable `source_id`: `hackathonist:<slug of title>` or, better, `hackathonist:<sha1 of normalised link href>` — the href is the only identifier that survives a title edit (`Hackathon` → `DESCON 8.0`, `DesCon 5.0`). Title alone is not unique: `Climathon` 6 rows, `5 dana na Javi` 4, `FON hakaton` 4, `MatHackathon`/`EESTech Challenge`/`Global Game Jam` 3 each, and `Climathon`/`Global Game Jam`/`5 dana na Javi` recur yearly with the same href.
- Same event can appear twice in one year file with different hrefs (`The Great Hacka` 2023 → same Google Form both times).
- Lifecycle: added to the `Upcoming` table, later moved to the `Recent` table (row deleted from one table, inserted into the other — verified in commit `346bbb3f` 2026-06-17), and once the year is over moved into `<year>.md` (last edit of `2025.md`: `9116d968` 2026-04-09). No status field, so a disappearing row is the only cancellation signal, indistinguishable from "pruned". Only push `cancelled` on manual review.
- Finished events stay as one line: no winners, results, participants or links to them anywhere. `people` is never populated.
- Author cadence: 10 most recent commits span 2026-05-25 → 2026-10-29 (~1–3 edits/month, cluster around event season). Repo `last-modified` of the tree: `Fri, 13 Nov 2026 16:55:12 GMT` (a non-Markdown change). Committer `Željko Šević <zsevic95@gmail.com>`.

## Rate and failure behavior

- No rate limiting observed on `raw.githubusercontent.com`: ~15 sequential GETs, all 200, ~200 ms each, no 429.
- `api.github.com` anonymous: `x-ratelimit-limit: 60`, `x-ratelimit-remaining` dropped 19 → 17 → 16 over three calls (another process was draining the same IP budget at probe time, `used` already 41). 304s also count. Unauthenticated `path=` filtered commit lists can 422 — retry or use the `.atom` feed instead.
- 404 for `2026.md` and for `raw.githubusercontent.com/robots.txt`. No redirects, no cookies, no JS.
- Missing event: N/A (no per-event endpoint).

## Volume and relevance

| scope | count |
|---|---|
| all rows, 2017–2026 | 219 |
| 2026 (README) | 15 (2 upcoming, 13 recent) |
| 2025 | 2 |
| 2024 | 6 |
| rows marked `online` or `/online` (Location cell) | 31 |
| rows whose href is a Google Form / bit.ly / Facebook / Instagram / Office Form (registration-only link) | 21 |
| rows with an empty Location cell | 11 |
| rows with a stray `*` after the link | 4 |

Relevance to §19.1: essentially 100 % Serbian hackathons, therefore all relevant; noise is a single Scrimba promo line inside `README.md` (must be skipped — it is a paragraph, not a table row) and rows about foreign-hosted online events (`Microsoft Women in AI Hackathon`, `NLB Group Hackathon`, `#hiljadekamera`). Classification is free: everything is `kind = "hackathon"`; only `format`/`location` need work. No filtering by country needed.

## Quirks

- 2026 has no year file; the current year is only in `README.md`, so "parse year files" alone loses the live events.
- Four of 219 rows carry a stray `*` after the link (`| [Summer hackathon](https://…)* | ICT Hub | 15-16 July 2017 |`, also `Cloud & IoT Hackathon`, `MatHackathon`, `Telenor`) — a naive `^\[.*\]\(.*\)$` cell parser drops them.
- `2017.md` row `| [Telenor](https://www.telenor.rs/…)* | Telenor (Airport city) |  24-25 February 2017 |` has a double space before the date.
- 11 rows have an empty Location cell (`| [BizKod](https://bizkod.rs/) |   | 16-17 March 2024 |`); don't infer Belgrade from emptiness.
- Many hrefs are the organiser's homepage, so the same href hosts different events in different years (`hack.garaza.org` = Frontier Hackathon 2025, FinTech AI Hackathon 2026, Frontier Hackathon 2026) — a href-derived id must include the date.
- Cyrillic IDN href (`https://иманаћирилици.срб/`) and non-ASCII titles; keep normalisation of the URL (punycode) and NFC text.
- Some entries reference Luma (`https://luma.com/hk4wt5jj` — SuperPlane Hackathon) → candidate `match_hint` against the Luma adapter.
- Organiser domains usable as `pages` seeds (frequency over 219 rows): `startit.rs` 22, `hakaton.fonis.rs` 7, `brandnewengineers.rs` 7, `hackathon.icthub.rs` 4, `hzs.fonis.rs` 4, `hakaton.sicef.info` 4, `aibg.best.rs` 5 + `aibg.bestns.org.rs` 3, `descon.me` 4, `mathack.rs` 3, `bizkod.rs` 3, `sumamatf.rs` 3, `ethbelgrade.rs` 3, `hack.garaza.org` 3, `datathon.eu` 3, `metropolitan.ac.rs` 3, plus `climathon.climate-kic.org` 6 (dead series), `tadhack.com`, `nf-co.re`, `hub-201.com`, `ntpark.rs`. 117 distinct domains in total.

## Config sketch

```toml
[[source]]
id = "hackathonist"
adapter = "hackathonist"
interval = "24h"
rate = "10/min"
repo = "zsevic/hackathonist"
branch = "gh-pages"
files = ["README.md", "2025.md", "2024.md"]
backfill = ["2023.md", "2022.md", "2021.md", "2020.md", "2019.md", "2018.md", "2017.md"]
watch_atom = "https://github.com/zsevic/hackathonist/commits/gh-pages.atom"
default_timezone = "Europe/Belgrade"
default_country = "RS"
```
