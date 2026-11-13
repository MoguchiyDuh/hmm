# 11. Event catalog and import

## 11.1 Sources of events

| Source | `origin` | Status on creation |
|---|---|---|
| Parser import (normal path): hackathons, contests, conferences, meetups, workshops, courses | `import` | per §11.2 |
| Admin | `manual` | `published` |
| Member (§26.3) | `member` | `published`, after a duplicate check |

- `formation_enabled` (team formation, §12) defaults to on for `hackathon` and `contest` and off otherwise.
- Formation needs teams of at least 2: it defaults to off when `team_size_max` < 2, and turning it on for such an event → 409 `invalid_state`.
- Admins can toggle `formation_enabled` per event while the event has no enrollments.
- Every event supports attendance (§26).

## 11.2 Import (`POST /internal/events/import`)

A batch has ≤ 50 items, each an `EventImport` (§19.3). Each item runs in its own transaction:

1. Validate the schema. A bad item gets `rejected` with errors; the rest of the batch continues.
2. Find the event:
   - by `source.source_id` in `event_sources`;
   - else by `match_hint.event_id`, attaching the source;
   - else run the duplicate heuristic: same normalized title (casefolded, punctuation and years stripped) and `[starts_at, ends_at]` overlapping within ± 3 days → create as `draft` with `review_reasons += possible_duplicate`.
3. Payload hash unchanged → `unchanged` (only `last_seen_at` is updated).
4. `people[]` (attendee and winner lists, §19.3) are upserted into `event_source_people` and matched to members by GitHub login, then by Telegram username. Each match:
   - creates a `trusted` system confirmation on the member's attendance (creating `attended` if missing), with the role from the list (`winner` → `attendee`);
   - for a winner on a hackathon team, verifies that team's result: `place_status = verified`, `place` taken from the list (§13.1).
5. Map `areas[].key` to known non-archived areas. Unknown keys go to `unmapped_areas` with `review_reasons += unmapped_areas`.
6. **Create:** `published` if `confidence ≥ import_auto_publish_confidence` (0.8) and there are no review reasons; otherwise `draft` with the reasons and admin alert `import_review`.
7. **Update:** apply changed fields except those in `event_field_locks`; record a revision with `ignored_fields` for the locked ones. If dates, team size, format or registration deadline changed and the event has non-terminal teams or `going` attendees, notify those team members and attendees (`event_changed`). Formation timing reacts per §12.2.
8. **Cancel** (`status: cancelled` from the parser):
   - event → `cancelled`; `going` attendances → `cancelled`;
   - non-terminal teams → `cancelled`; live enrollments → `expired`;
   - pending invites, join requests, swaps and merges → `closed`;
   - formation phase → `closed`;
   - notify (`event_cancelled`); admin alert `event_cancelled_with_teams` if any teams existed.
9. Team size changed and an existing team is now above the new max or below the new min → admin alert `team_size_conflict`. Nobody is removed automatically.

Response:

```json
{ "results": [
  { "index": 0, "source_id": "devpost:123", "result": "created|updated|unchanged|draft|rejected",
    "event_id": "…", "review_reasons": [], "ignored_fields": [], "errors": [] } ] }
```

Parser support:
- `GET /internal/events/tracked?cursor=` → `source_id`s, URLs and `ends_at` of non-archived events that haven't ended, plus events that ended within the last 30 days (for attendee and winner lists). The parser revisits these.
- `POST /internal/parser/heartbeat {run_id, started_at, finished_at, stats: {sources_checked, candidates, imported, errors}, errors: [...]}`. No heartbeat for `parser_heartbeat_max_age_hours` (26) → admin alert `parser_down`.

## 11.3 Admin edits

- `PATCH` any field → that field is locked against imports. `DELETE .../locks/{field}` unlocks it; the next import may overwrite it.
- **Review queue:** every event with non-empty `review_reasons`: import drafts and published member events flagged as possible duplicates. Actions:
  - publish (a draft) or keep (a published event); either clears the reasons;
  - reject → `archived`;
  - merge `{into: id}`: moves sources and attendances to the target and archives this event. Only allowed if it has no teams or enrollments.
- **Cancel** `{reason}`: same effects as a parser cancellation (§11.2 step 8).
- **Archive:** the nightly job archives events whose `ends_at` is more than `event_archive_after_days` (180) ago. They leave the catalog; history is kept.

## 11.4 Catalog for members

- Shows `published` events, and `cancelled` ones only to members who were involved.
- Filters: `kind`, `phase`, `formation` (bool), `participation_phase` (`open`, `review`, `locked`), `area`, `format`, `q`, `from`, `to`, `mine` (going, attended, enrolled or on a team), `origin`.
- `eligibility` is shown as text and never enforced (§4.4).
- Detail: every field, the derived `phase`, areas with importance, difficulty with rationale, attendance counts and my attendance, members going; for formation events also the participation block from `GET /api/v1/events/{id}/formation` (§12.11) and my lead fit.
- `allowed_actions`: `going`, `attended`, `enroll`, `withdraw`, `claim_lead`, `request_join`, `edit`, `cancel`.

## 11.5 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/events` | member | Catalog (§11.4 filters) |
| GET | `/api/v1/events/{id}` | member | Detail |
| GET | `/api/v1/events/{id}/projects` | member | Teams and projects of the event |
| POST | `/api/v1/admin/events` | admin | Manual event |
| PATCH | `/api/v1/admin/events/{id}` | admin | Any field; locks it |
| GET | `/api/v1/admin/events/{id}/locks` | admin | Locked fields |
| DELETE | `/api/v1/admin/events/{id}/locks/{field}` | admin | Unlock |
| GET | `/api/v1/admin/events/{id}/revisions` | admin | Revisions |
| GET | `/api/v1/admin/events/review-queue` | admin | §11.3 |
| POST | `/api/v1/admin/events/{id}/publish` | admin | Publish or keep |
| POST | `/api/v1/admin/events/{id}/reject` | admin | → `archived` |
| POST | `/api/v1/admin/events/{id}/merge` | admin | `{into}` |
| POST | `/api/v1/admin/events/{id}/cancel` | admin | `{reason}` |
| POST | `/internal/events/import` | parser | §11.2 |
| GET | `/internal/events/tracked` | parser | §11.2 |
| POST | `/internal/parser/heartbeat` | parser | §11.2 |
| GET | `/internal/areas` | parser | §8.3 |

## 11.6 Done when

- Re-importing an identical payload yields `unchanged`; a changed payload updates without duplicating.
- Admin-locked fields survive imports; ignored changes are visible in revisions.
- Cancellation cascades correctly.
