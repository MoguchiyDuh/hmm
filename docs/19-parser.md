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
