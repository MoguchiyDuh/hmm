# 14. Core — Algorithm integration (lead fit, quick-pick, formation, reserve, mentors, community scan)

Core's job here: build snapshots, call algo in the worker, validate outputs, store, serve. Algo behavior is in §18.

## Lead fit
- Stored per (hackathon, user) in `lead_fits`.
- `compute_lead_fits(hackathon)`: candidates = onboarded, non-banned, non-DND members not on a team for the hackathon. Triggered on publish or area changes, nightly for hackathons in `open`/`locked`, and by `compute_lead_fits_for_member(user)` after that member's metrics change.
- `eligible = fit ≥ lead_fit_threshold`.

## Quick-pick
One `recommendation_sets` row per computation. `input_hash` covers roster, candidate ids, candidate metric versions, hackathon revision and algo version.

| Context | `enrolled` candidates | `others` candidates |
|---|---|---|
| team `forming` (core building) | `active` enrollments | everyone else eligible |
| team `review` / `locked` (filling) | reserve | everyone else eligible |
| standalone project with `recruiting` set | members who sent a join request | everyone else eligible |

"Everyone else eligible" means onboarded, not banned, not DND, not already on this project (or on a team for this hackathon), and not already invited.

- `refresh_recommendations(project)` is enqueued when the project, its `recruiting`, the enrollments/reserve or the event change. It's debounced with procrastinate `queueing_lock = project_id`. A matching `input_hash` → no-op.
- Validation: every returned `user_id` is in its candidate set; scores are clamped; reasons are non-empty. Bad items are dropped and logged. An exception → set `failed`, alert `algo_failure` (deduped hourly).

## Formation
- `run_formation` (§12.5) calls `form_teams`. Core validates hard constraints and falls back to its deterministic planner on failure. Both the algo plan and the fallback plan are stored in `formation_runs` for inspection.

## Reserve ranking
- `rank_reserve(hackathon)` runs after formation, on every reserve change and nightly while `locked`. It calls algo `rank_reserve` and writes `enrollments.reserve_rank`. On failure it keeps the previous order and appends new entries by enrollment time.

## Mentor matching
- `match_mentors(request)` runs when a help request opens (or turns from direct to open). Candidates: onboarded, non-banned, non-DND members with free mentor capacity, not under a `mentoring` restriction, not the learner. Mentor offers in the area are flagged in the input.
- Core notifies the top `mentor_notify_top_n` and stores the set in `recommendation_sets` (`subject_type=help_request`) so the learner sees "suggested mentors" and can send a direct request.
- Failure → no notifications, the request still shows in the feed; alert `algo_failure`.

## Community scan
- Nightly `scan_community` sends algo the confirmation graph (who confirmed whom, where), mentorship pairs, review pairs and member-created events from the last 180 days. Returned flags go to the to-check queue (§10).

## Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/projects/{id}/recommendations` | lead/admin → `{status, stale, computed_at, enrolled: [Rec], others: [Rec]}` with embedded mini-profiles |
| POST | `/api/v1/projects/{id}/recommendations/refresh` | lead → 202 (rate limit 1/min) |
| GET | `/api/v1/help-requests/{id}/suggested-mentors` | learner → `[Rec]` |

`Rec`: `{user_id, score 0..1, reasons: string[], tags: string[], suggested_area: area_key|null}`. Tags are free strings from algo (e.g. `newcomer`, `covers:ml_ai`, `mentor`). Frontend renders them as chips.
