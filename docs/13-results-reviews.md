# 13. Core — Results, reviews, retrospectives

## Hackathon results
`POST /projects/{id}/result` by lead, team `active` and the event `phase = ended` (or admin anytime).

```json
{ "outcome": "finished|withdrawn", "place": 2, "award": "2nd place + sponsor prize",
  "repo_url": "...", "demo_url": "...", "presentation_url": "...",
  "member_areas": { "<user_id>": "backend" },
  "retrospective_md": "what was done / not done / problems / mistakes / notes per member" }
```

Effects:
- `team_status` → `finished|withdrawn`. Project `status` → `shipped` if a repo or demo link is present, otherwise `abandoned` (§27.1). The lead can continue it as a regular project later.
- A `project_releases` row is created from the result, and every team member's `project_members` fact gets a system confirmation (`trusted`).
- Every team member's attendance becomes `attended` with a system confirmation (§26.1).
- Enqueue `recompute_member` for all members.
- `finished` only: open a review window, notify all members `reviews_open`.
- Lead may edit the result while the window is open; edits re-trigger recompute.
- Retrospective visible to team members + admins only.

## Reviews (all contexts)

| Subject | Opens when | Who rates whom | Context |
|---|---|---|---|
| Hackathon team | result `finished` | every member ↔ every member present at `ends_at` | `member`, `lead` |
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
| POST | `/api/v1/projects/{id}/result` | lead/admin |
| PATCH | `/api/v1/projects/{id}/result` | lead within window, admin |
| GET | `/api/v1/projects/{id}/result` | member (retrospective field only for team/admin) |
| GET | `/api/v1/me/reviews/pending` | member → open windows with `to_review`, `submitted`, `closes_at` |
| PUT | `/api/v1/review-windows/{id}/reviews/{reviewee_id}` | participant `{rating?, comment}` |
| PUT | `/api/v1/events/{id}/host-reviews/{reviewee_id}` | attendee with `attended` |
| GET | `/api/v1/users/{id}/reviews?context=&cursor=` | member (revealed, non-removed) |
| POST | `/api/v1/reviews/{id}/report` | member `{reason}` |
| GET | `/api/v1/admin/reviews?reported=&cursor=` | admin |
| POST | `/api/v1/admin/reviews/{id}/remove` | admin `{reason}` |
