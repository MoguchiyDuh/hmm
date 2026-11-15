# 19. Parser: quality report

Measured on 2026-11-15 against the first full import on the dev stack (run `parser-20261115T223000-c89be11e`: 13 sources, 1,788 candidates, 563 items, 15 merged, 563 imported). Data: [19-parser-quality/sample.json](19-parser-quality/sample.json). Labeling rules: [19-parser-quality/labeling-instructions.md](19-parser-quality/labeling-instructions.md).

> **Agent-labeled.** Every label was produced by AI agents reading the event's own pages. No label was checked by a human. Treat the numbers as an estimate with that bias, not as ground truth.

## Method

- **Sample.** 60 of the 563 imported items, stratified by source in proportion to volume (at least 1 per source), drawn in seeded random order (`setseed(0.42)`). Added: the 3 items the LLM step rejected (n 61–63), to check the rejections.
- **Labeling.** Four labeler agents split the 63 items; each opened the event's own pages (organizer site, source detail page, Luma, CTFtime, Kaggle API) with `curl` or a text fetcher and recorded per item: is it one scheduled event, is it relevant (a tech event for developers, designers, data or security people or tech students), and whether kind, dates (local calendar dates; times within 1 h when the page gives times), format, country and team size match. All 63 items were verifiable.
- **Review.** A fifth agent re-checked 10 labels against the pages (4 confirmed correct items, 6 flagged ones). 9 agreed; 1 was overridden: BBICC was labeled not relevant because the rule asked for a tech focus, but case competitions are in scope (§19.1). The override is stored next to the original label.

## Results

Accepted items (n = 60). Ratios exclude `null` labels (online events have no country; a non-event has no kind or dates).

| Measure | Result |
|---|---|
| One scheduled event | 59/60 (98%) |
| Relevant | 57/60 (95%) |
| Kind | 59/59 |
| Dates | 51/59 (86%) |
| Format | 58/58 |
| Country | 38/38 |
| Team size, when the page states it | 8/18 found, 0 wrong |

LLM rejections: 3/3 correct (two program or funding pages that are not events, one sales meetup).

| Source | n | Event | Relevant | Dates | Team size stated → found | Date or relevance misses |
|---|---|---|---|---|---|---|
| hackathonhub | 12 | 11/12 | 10/12 | 7/11 | 0/2 | 4 date errors in HackathonHub's own data; a funding call; an idea/entrepreneurship hackathon |
| confs-tech | 9 | 9/9 | 9/9 | 8/9 | — | conference-data lists 2027 dates the site does not show yet |
| devpost | 9 | 9/9 | 9/9 | 9/9 | 0/5 | — |
| dev-events | 6 | 6/6 | 6/6 | 6/6 | — | — |
| ctftime | 5 | 5/5 | 5/5 | 5/5 | 2/2 | — |
| kaggle | 4 | 4/4 | 4/4 | 3/4 | 3/3 | API deadline differs from the host's prose |
| luma-rs | 3 | 3/3 | 2/3 | 3/3 | — | AI-compliance founder talk |
| mlh | 3 | 3/3 | 3/3 | 1/3 | 0/1 | times shifted 4–5 h |
| hackalendar | 3 | 3/3 | 3/3 | 3/3 | 0/2 | — |
| icpc | 2 | 2/2 | 2/2 | 2/2 | 2/2 | — |
| gdg-rs | 2 | 2/2 | 2/2 | 2/2 | — | — |
| bbicc | 1 | 1/1 | 1/1 | 1/1 | 1/1 | — |
| hackathonist | 1 | 1/1 | 1/1 | 1/1 | — | — |

Kind and format were never wrong; country was never wrong. Per-source counts are small; a single item moves a ratio a lot.

## Errors

**Dates (8).**
- MLH, 2 items: the listing's `startsAt`/`endsAt` are local wall-clock times entered as New York time and stored as UTC. IKU Womxn in STEM (09:00–16:00 on its site) came out as 14:00–21:00 local. Cross-checks on items outside the sample agree: HackSheffield, DurHack and HackTheBurgh start at 09:00–09:30 local, and Junction and Since AI finish Sunday at 16:00 local instead of 23:00. **Fixed:** wall-clock times are re-read in the event's zone; the `entry_zone` option defaults to `America/New_York`. Placeholder times and online events are unchanged.
- HackathonHub, 4 items: the error is in HackathonHub's own data. Its `.md` export and API show the same values: an opening day instead of a 7-day sprint (Cambridge EduX), a time 2 h later than the organizer's Luma page (KVA Turin), a Friday-evening start for an event the page lists as Saturday–Sunday (Startup Weekend Iasi; arguable for a 54-hour format), and a window that ends before registration closes. Not fixed: the confidence of 0.6 already keeps HackathonHub items below auto-publish.
- confs-tech, 1 item: conference-data has IT Security Summit Munich 2027 dates the organizer has not published yet. The parser is faithful to the source.
- Kaggle, 1 item: the API deadline (Nov 16 11:59 UTC) and launch date differ from the host's overview text (Nov 15 23:59 CDT, launch Jul 10). The parser uses the API values.

**Relevance (3).**
- HackathonHub lists SPRIND's 20-month funding challenge as a 2-day hybrid event in Leipzig. The LLM step did not drop it: it was either judged relevant or past the 200-call limit, which 30 items hit in that run.
- Hackathon Geopark Kras – Carso is an idea and business-model hackathon for young people, job seekers and NGOs. Borderline.
- Luma's "Garaža Meetup w/ HeyData" is a founder and legal talk on AI compliance at a startup hub. Borderline.

**Team size (10 missed, 0 wrong).**
- Devpost (5): the adapter read only the 300-character JSON-LD description, and the rules sit in the page's `#challenge-description` and `#challenge-requirements` sections. **Fixed:** the requirements section, then the description, is parsed; the full description is now imported as `description_md`.
- Hackalendar (2): the description stated team size but was not parsed. **Fixed.**
- Phrasing: "two to four", "up to five", "Teams: 3 to 4" and "team size: 2-5" were not matched. **Fixed** in `team_size`.
- HackathonHub (2) and MLH (1): the stored text does not mention team size. Not fixable without fetching organizer sites.

**Dedupe.** The import produced 2 `possible_duplicate` drafts. Update Conference Krakow 2027 was listed twice by dev.events under two ids; same-source exact twins now merge. IJS Munich 2027 has conflicting dates in dev.events (8–12 Nov, matches the organizer site) and confs-tech (15–19 Nov); the catalog flag is the right outcome. Since both sources had priority 30, dev-events now gets 35 so it leads merges with confs-tech.

## After the fixes

Dry run of `devpost`, `hackalendar` and `mlh` on the same day, compared with the labels (not relabeled):

- Team size: all 7 Devpost and Hackalendar misses now match the page, and the 5 sampled items without a stated limit stay empty. Across the run, 25 of 90 Devpost items and 5 of 18 Hackalendar items now have a team size, against 0 before; the one outlier, a max of 20 (OBSCURA), is what its page says. 89 of 90 Devpost items now have a full description.
- MLH: IKU now reads 09:00–16:00 local, an exact match. HackTheBurgh now starts at 09:00 as its site says but still ends at 15:00 instead of 17:00, which is in MLH's own data. Since AI keeps its dates but moves to 16:00–16:00 Helsinki; the page its labeler read showed 21:00 UTC, so that item is unresolved.
- Expected effect on the sample: team size found 15/18 (was 8/18) and dates 52/59 (was 51/59).

## Fixtures

`tests/fixtures/imports/` holds 6 synthetic payloads (one per kind) and 40 real ones exported from the same run, chosen to cover every source and known edge cases. Two of the 40 are derived and say so in their names:

- `hackalendar-launch-day-cancelled.json`: status set to `cancelled`. No source had a real cancellation on 2026-11-15.
- `hackathonhub-qhack-unknown-area.json`: area replaced with `quantum_computing`, which is not a known area key, to exercise `unmapped_areas`.

`devpost-hackupc-winners.json` is the HackUPC 2026 event (`devpost:29519`) taken from the Devpost revisit path, with winners added by hand: the HackUPC-track 1st–3rd place projects from the gallery's `challenge-winners-json-ld`, their team members from the project pages, and GitHub logins only where the member's Devpost profile links one (`m31-galaxy`, `ksambhi`, `Schebbair`, `mserra0`). The other four winners have no published login and are stored with a display name only.
