# 13. Core — Results, reviews, retrospectives

## Hackathon results
`POST /projects/{id}/result`, team `active` and the event `phase = ended`. Who may submit:
- the lead, from `ends_at`;
- any confirmed member, after `results_due_days` (7) without a result;
- an admin, anytime.

```json
{ "outcome": "finished|withdrawn", "place": 2, "award": "2nd place + sponsor prize",
  "repo_url": "...", "demo_url": "...", "presentation_url": "...",
  "member_areas": { "<user_id>": "backend" },
  "participants": { "<user_id>": true },
  "retrospective_md": "what was done / not done / problems / mistakes / notes per member" }
```

- `participants` covers every team member: who actually took part. Members marked `false` (ghosts) get no system confirmation; teammates can still confirm them as `peer` (§25).

Effects:
- `team_status` → `finished|withdrawn`. Project `status` → `shipped` if a repo or demo link is present, otherwise `abandoned` (§27.1). The lead can continue it as a regular project later.
- A `project_releases` row is created from the result, and the `project_members` fact of every participant gets a system confirmation (`trusted`).
- Every participant's attendance becomes `attended` with a system confirmation (§26.1).
- Enqueue `recompute_member` for all members.
- `finished` only: open a review window among participants, notify them `reviews_open`.
- The submitter or the lead may edit the result while the window is open; edits re-trigger recompute.
- Retrospective visible to team members + admins only.

**Place and award verification.** A claimed `place` or `award` starts as `place_status = unverified`.
- A parser winner list matching a team member sets it `verified` and takes the place from the list (§11).
- Otherwise an admin verifies or rejects it in the "Results to verify" queue (dashboard counter + weekly digest, no alert).
- Algo receives `place_status`; only a `verified` place earns a place bonus (§18.3).

**No result.** After `results_close_days` (30) from `ends_at` without a result, the team auto-closes: `team_status = withdrawn`, project `abandoned`, `project_results.auto_closed = true`, no system confirmations, no review window. Members can still claim `attended` themselves within the attendance window (§26).

## Reviews (all contexts)

| Subject | Opens when | Who rates whom | Context |
|---|---|---|---|
| Hackathon team | result `finished` | every participant ↔ every participant (`participants = true`) | `member`, `lead` |
| Project | release (§27.5) | members present ≥ `project_review_min_days` ↔ each other | `member`, `lead` |
| Mentorship | close (§28.5) | learner → mentor (rating); mentor → learner (endorsement) | `mentor` |
| Study pair | close | both ways | `member` |
| Member-hosted event | after `starts_at` | attendee → host/speaker | `host`, `speaker` |

- **Windows** (`review_windows`): `review_window_days` (7) from opening. Reviews are editable until the window closes.
- **Blind until close:** windowed reviews are hidden from reviewees until the window closes, then revealed together (`revealed_at`), and reviewees get a `review_received` digest.
- **Instant:** host/speaker ratings have no window. They're visible immediately, but only as an aggregate (average + count) once there are ≥ 3; individual comments are shown without reviewer names.
- `rating 1..10` + `comment ≤ 2000`. Endorsements have no rating. One review per (window, reviewer, reviewee).
- **Reputation** per context (`member`, `lead`, `mentor`, `host` incl. `speaker`): plain average of revealed, non-removed ratings, with counts. Computed by core. Algo receives raw reviews for matching (lead fit, recommendations, mentor matching), **never** for XP.
- Report: any member can report a revealed review → admin alert `review_reported`. Admin may remove it (soft, with reason; reviewer notified). Algo may also mark reviews `to_check` (rating rings, §10).
- Reminder `reviews_pending` 2 days before close to members who haven't submitted.

## Endpoints

| Method | Path | Who |
|---|---|---|
| POST | `/api/v1/projects/{id}/result` | lead; any confirmed member after `results_due_days`; admin |
| PATCH | `/api/v1/projects/{id}/result` | submitter or lead within window, admin |
| GET | `/api/v1/admin/results?place_status=unverified&cursor=` | admin (results to verify, with parser winner candidates) |
| POST | `/api/v1/admin/projects/{id}/result/verify` | admin `{decision: verify\|reject, note?}` |
| GET | `/api/v1/projects/{id}/result` | member (retrospective field only for team/admin) |
| GET | `/api/v1/me/reviews/pending` | member → open windows with `to_review`, `submitted`, `closes_at` |
| PUT | `/api/v1/review-windows/{id}/reviews/{reviewee_id}` | participant `{rating?, comment}` |
| PUT | `/api/v1/events/{id}/host-reviews/{reviewee_id}` | attendee with `attended` |
| GET | `/api/v1/users/{id}/reviews?context=&cursor=` | member (revealed, non-removed) |
| POST | `/api/v1/reviews/{id}/report` | member `{reason}` |
| GET | `/api/v1/admin/reviews?reported=&cursor=` | admin |
| POST | `/api/v1/admin/reviews/{id}/remove` | admin `{reason}` |
