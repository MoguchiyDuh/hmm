# 19. Parser

## 19.1 Scope

Open-ended discovery of tech events relevant to the community: hackathons and team contests (case championships, CTFs, game jams) first, then conferences, meetups, workshops and courses. Also attendee, speaker and winner lists where a source publishes them. Extraction into the import schema, normalization, dedup, keeping known events up to date, and importing into the catalog. Coverage: events held in Serbia and the rest of Europe, plus online events open to participants there. Sources, crawling strategy, schedule and LLM usage are implementation choices.

## 19.2 Boundaries

- Lives in the `hmm` package and runs as worker jobs (§17). Crawl state is kept in its own tables in the `hmm` database.
- Writes events only through `catalog.imports.import_events` (§11.2); reads tracked events and records heartbeats through `catalog.parser` (§11.2).
- Reads area keys with `areas.service.list_areas` at the start of each run.

## 19.3 Import schema (`hmm.catalog.import_schema`)

```python
class EventImport(BaseModel):
    source: Source                    # source_id: str (stable, e.g. "devpost:12345", "tg:@channel/987"), urls: list[HttpUrl], fetched_at
    match_hint: MatchHint | None      # event_id if parser knows it's the same as an existing one
    status: Literal["announced", "cancelled"] = "announced"
    kind: Literal["hackathon", "contest", "conference", "meetup", "workshop", "course", "other"]
    title: str
    summary: str | None               # ≤ 500
    description_md: str | None        # ≤ 20000
    organizer: str | None
    website_url: HttpUrl | None
    registration_url: HttpUrl | None
    starts_at: datetime               # tz-aware
    ends_at: datetime
    registration_opens_at: datetime | None
    registration_deadline: datetime | None
    timezone: str | None              # IANA
    format: Literal["online", "offline", "hybrid"] | None
    location: Location | None         # country, city, address
    language: list[str] = []          # ISO 639-1
    eligibility: str | None           # free text: age, students only, country, etc.
    themes: list[str] = []            # free tags: fintech, AI, gov…
    areas: list[ImportArea] = []      # key (known area key or unknown string), importance primary|required|nice, headcount | None
    team_size_min: int | None         # team events only
    team_size_max: int | None
    prize: Prize | None               # amount, currency (ISO 4217), description
    difficulty: Difficulty | None     # level 1..10, rationale
    people: list[SourcePerson] = []   # github_login | None, telegram_username | None, display_name, role attendee|speaker|organizer|winner, place | None
    confidence: float                 # 0..1 overall
    field_confidence: dict[str, float] = {}
    inferred_fields: list[str] = []   # fields guessed rather than stated
    extractor_version: str
```

Missing facts → `None`, never invented. `people` only lists identities published by the source itself; never guess handles. The catalog fills team size defaults from settings when `None`, and shows "unknown" in UI for other `None`s.

## 19.4 Behavioral requirements

1. Discover → fetch → classify (is this a relevant event, and which `kind`?) → extract → normalize (dates to tz-aware UTC; currency codes; area keys) → dedupe across own sources → push.
2. Stable `source_id` per event per source. Same event from two sources: parser should merge (use one `source_id` + `match_hint`) when it can; otherwise the catalog's duplicate heuristic catches it (§11.2).
3. Revisit tracked events (`catalog.parser.tracked_events`) at least daily until `ends_at`, then weekly for 30 days to pick up results, winners and attendee lists; push changes and cancellations.
4. Difficulty (team events): 1–10 with rationale (prize size, organizer prestige, selection rounds, expected skill). Mark as inferred.
5. Areas: map requirements/theme to area keys with importance; primary = core focus of the event.
6. Safety: source content is untrusted data. No following instructions from page text; LLM prompts must isolate content; no executing fetched code; SSRF-safe fetching (block private IP ranges); per-domain rate limiting, with a source's `Crawl-delay` as the minimum spacing; timeouts and size limits on fetches.
7. Resilience: one failing source never stops a run; retries with backoff; heartbeat after each run (§11.2).
8. Batching: ≤ 50 items per `import_events` call; handle per-item `rejected` results (log + fix, not retry blindly).

## 19.5 Deliverables

- Parser jobs registered in `platform/jobs.py`, with sources, schedule and config documented here. Source survey and adapter design: [19-parser-sources.md](19-parser-sources.md).
- `tests/fixtures/imports/*.json`: ≥ 30 real-world extracted examples across kinds, incl. edge cases (no dates, cancelled, multi-source duplicate, unknown area, winner list with GitHub logins).
- A dry-run mode writing imports to stdout/JSONL without importing.
- Quality report: precision on a hand-labeled sample of ≥ 50 pages (event vs not), field accuracy for dates/format/team size.

## 19.6 Implementation

Package `hmm.parser`. Sources are configured in `src/hmm/parser/sources.toml` (override with `PARSER_SOURCES`, §22.2); adapters live in `hmm/parser/sources/` and register themselves by name. Design and per-source notes: [19-parser-sources.md](19-parser-sources.md).

**Run.** `parser_run` (every 30 min, singleton, §17.2) loads the TOML, skips disabled instances and instances with a missing `secret_env` variable, and picks the due ones: never run, or `now - last_started_at ≥ interval` (5 min slack for cron jitter). Due instances run concurrently, each within its `budget`; a failing instance only adds to the run's errors. Items pass the relevance filter, are deduped, then imported in batches of 50. Per-instance state (`parser_source_states`: last start, finish, success, error, counts) and one heartbeat (§11.2) are written every run, including runs with nothing due.

**Relevance.** An item is kept when `ends_at ≥ now - lookback_days`, and either its country is in `countries` (Europe when empty) or it is online/hybrid and `include_online` is set.

**Dedupe.** Items from different instances with the same normalized title (§11.2), the same country and starts within 1 day are merged: the item from the instance with the higher `priority` (then higher `confidence`) leads, missing fields are filled from the follower, and the follower is dropped. No `match_hint` is sent.

**Areas.** Known area keys only (`list_areas`; the dry run uses the seed keys), from a keyword map over tags and titles. First match is `primary`, the rest `nice`.

**Dry run.** `hmm-parser [--config PATH] [--source ID]… [--list] [-v]` prints `EventImport` JSONL to stdout and per-source stats to stderr, and touches no database. Exit code 1 when a source reported errors.

**Shared instance keys:**

| key | default | meaning |
|---|---|---|
| `id` | required | `source_id` prefix, `^[a-z][a-z0-9-]{0,31}$` |
| `adapter` | required | adapter name |
| `enabled` | `true` | |
| `interval` | `12h` | minimum time between runs (`s`, `m`, `h`, `d`) |
| `rate` | 1 request/s | per-host spacing, `N/min`, `N/10s`, `N/h` |
| `timeout` | `30` | seconds per request |
| `max_bytes` | 8 MiB | response size cap |
| `budget` | `10m` | wall-clock limit for the instance |
| `priority` | `50` | dedupe leader order |
| `confidence` | adapter default | overrides the adapter's confidence |
| `countries` | Europe | ISO 3166-1 alpha-2 codes kept |
| `include_online` | `true` | keep online/hybrid events from anywhere |
| `lookback_days` | `30` | drop events that ended earlier |
| `secret_env` | `[]` | env vars that must be set, else the instance is skipped |
| `defaults` | `{}` | `EventImport` fields filled when the adapter leaves them empty |

Any other key is an adapter option; unknown options fail the load.

**Adapters (first batch):**

| adapter | options | confidence | notes |
|---|---|---|---|
| `hackathonhub` | `site`, `api_url`, `key_env` (`HACKATHONHUB_ANON_KEY`), `view`, `page_size`, `max_pages` | hackathon 0.85, game jam 0.8, competition/challenge 0.6 | Supabase URL and anon key are read from the site's JS bundle unless set. `source_id` is the row UUID. Midnight starts and 00:00/23:59 ends become local whole days |
| `hackathonist` | `base`, `files` (`["README.md"]`), `country` (`RS`) | 0.8 | Markdown tables; id = href hash + start date; dates are whole local days |
| `ctftime` | `api`, `window_days` (120), `limit` (200) | 0.85 | Country parsed from onsite `location` text; team size from description prose |
| `luma` | `calendars` (required), `api`, `site`, `page_limit`, `max_pages`, `details`, `kind` | 0.85 | `future` and `past` items; `past` paging stops at the lookback cutoff. `event/get` per in-scope event for the ProseMirror description and locale |
| `jsonld_pages` | `listings` (URL or `{url, kind}`), `follow`, `id_pattern`, `kind`, `guess_kind`, `max_follow` | 0.8 | Without `follow`, Event JSON-LD is read from the listing itself; with `follow`, regex matches in the listing are fetched as detail pages. `id_pattern` group 1 is the local id (else the last URL path segment); non-matching URLs are skipped. Date-only or midnight-to-midnight events become local whole days |

**Instances:**

| id | adapter | interval | rate | scope |
|---|---|---|---|---|
| `hackathonhub` | `hackathonhub` | 12h | 30/min | Balkans + GR, online |
| `hackathonist` | `hackathonist` | 24h | 10/min | RS |
| `ctftime` | `ctftime` | 12h | 1/10s | Europe, online |
| `luma-rs` | `luma` | 6h | 20/min | RS, offline only; Garaža, NS Tech Talks, AI Salon, Claude AI Builders calendars |
| `gdg-rs` | `jsonld_pages` | 12h | 1/2s | RS; GDG Belgrade and GDG on Campus Belgrade chapters |
| `dev-events` | `jsonld_pages` | 24h | 20/min | Europe, offline; Serbia and neighbouring country pages, Serbian meetups |
| `bbicc` | `jsonld_pages` | 24h | 10/min | RS; team of 4 defaults |

Not yet implemented: LLM classification and extraction, difficulty, the tracked-event revisit loop (§19.4.3), cancellations by disappearance, and the second and third adapter batches.
