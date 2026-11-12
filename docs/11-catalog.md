# 11. Core — Event catalog and import

## Sources of events
- Parser import (normal path): hackathons, contests, conferences, meetups, workshops, courses.
- Manual creation by admin, `origin=manual`, published immediately.
- Member creation (§26.3), `origin=member`, published immediately, duplicate-checked.

`kind` decides defaults: `formation_enabled` is on for `hackathon` and `contest` (team formation, §12) and off otherwise. Admins can toggle it per event while no enrollments exist. Every event supports attendance (§26).

## Import semantics (`POST /internal/events/import`)
Batch ≤ 50 items, each a `EventImport` (§19.3). Per item, in its own transaction:

1. Validate schema → per-item `rejected` with errors (not whole batch).
2. Find by `source.source_id` in `event_sources`. Else if `match_hint.event_id` given, attach. Else run duplicate heuristic: same normalized title (casefold, strip punctuation/years) and overlapping `[starts_at, ends_at]` ± 3 days → create as `draft` with `review_reasons += possible_duplicate`.
3. Payload hash unchanged → `unchanged` (update `last_seen_at`).
4. `people[]` (attendee/winner lists, §19.3) are upserted into `event_source_people` and matched to members by GitHub login, then Telegram username. Matches create `trusted` system confirmations on their attendance (creating `attended` if missing), with the role from the list (`winner` → `attendee`). A matched winner on a hackathon team verifies that team's result place: `place_status = verified`, `place` set from the list (§13).
5. Map `areas[].key` to known non-archived areas; unknown keys go to `unmapped_areas` + `review_reasons += unmapped_areas`.
6. **Create**: status `published` if `confidence ≥ import_auto_publish_confidence` and no review reasons; otherwise `draft` with reasons and admin alert `import_review`.
7. **Update**: apply changed fields except those in `event_field_locks`; record revision with `ignored_fields` for locked ones. If dates, team size, format, or registration deadline changed and the event has non-terminal teams or `going` attendees → notify those team members and attendees (`event_changed`). Formation timing reacts per §12.2.
8. `status: cancelled` from parser → event `cancelled`; `going` attendances `cancelled`; all non-terminal teams → `cancelled`; live enrollments `expired`; pending invites, join requests, swaps and merges `closed`; formation phase `closed`; notify; admin alert `event_cancelled_with_teams` if any teams existed.
9. If team size changed and an existing team now exceeds new max or is below new min → admin alert (no automatic removal).

Response:
```json
{ "results": [
  { "index": 0, "source_id": "devpost:123", "result": "created|updated|unchanged|draft|rejected",
    "event_id": "…", "review_reasons": [], "ignored_fields": [], "errors": [] } ] }
```

Parser support endpoints:
- `GET /internal/events/tracked?cursor=` → source_ids + urls + `ends_at` of non-archived events not yet ended, plus ended events within 30 days (for attendee/winner lists) (parser revisits these).
- `POST /internal/parser/heartbeat` `{run_id, started_at, finished_at, stats: {sources_checked, candidates, imported, errors}, errors: [...]}`. No heartbeat for `parser_heartbeat_max_age_hours` (26) → admin alert `parser_down`.

## Admin edits
- `PATCH` any field → field auto-locked against imports. `DELETE .../locks/{field}` unlocks; the next import may overwrite it.
- Review queue: drafts with reasons. Actions: publish, reject (→ archived), merge.
- Merge `{into: id}`: moves sources to target, archives the draft. Only allowed if the draft has no teams/enrollments; attendances are moved to the target.
- Cancel: same effects as parser cancellation, with `reason`.
- Archive: events with `ends_at` < now − 180 d are auto-archived by nightly job (hidden from catalog, history kept).

## Catalog for members
- Only `published` and `cancelled` (latter shown only if user had involvement).
- Filters: `kind`, `phase`, `formation` (bool), `participation_phase` (`open|review|locked`), `area`, `format`, `q`, `from`, `to`, `mine` (going/attended/enrolled/on a team), `origin`.
- `eligibility` is shown as text and never enforced (§4.4).
- Detail includes: all fields, derived `phase`, areas with importance, difficulty + rationale, attendance counts and my attendance, members going, and for formation events the participation block from `GET /events/{id}/formation` (§12.11) and my lead fit. `allowed_actions`: `going`, `attended`, `enroll`, `withdraw`, `claim_lead`, `request_join`, `edit`, `cancel`.

## Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/events` | member |
| GET | `/api/v1/events/{id}` | member |
| GET | `/api/v1/events/{id}/projects` | member |
| POST | `/api/v1/admin/events` | admin create manual |
| PATCH | `/api/v1/admin/events/{id}` | admin edit (+locks) |
| GET | `/api/v1/admin/events/{id}/locks` | admin |
| DELETE | `/api/v1/admin/events/{id}/locks/{field}` | admin |
| GET | `/api/v1/admin/events/{id}/revisions` | admin |
| GET | `/api/v1/admin/events/review-queue` | admin |
| POST | `/api/v1/admin/events/{id}/publish` | admin |
| POST | `/api/v1/admin/events/{id}/reject` | admin |
| POST | `/api/v1/admin/events/{id}/merge` | admin `{into}` |
| POST | `/api/v1/admin/events/{id}/cancel` | admin `{reason}` |
| POST | `/internal/events/import` | parser |
| GET | `/internal/events/tracked` | parser |
| POST | `/internal/parser/heartbeat` | parser |
| GET | `/internal/areas` | parser |

## Done when
- Re-importing identical payload yields `unchanged`; changed payload updates without duplicating.
- Admin-locked fields survive imports; ignored changes visible in revisions.
- Cancellation cascades correctly.
