# 13. Results, reviews, retrospectives

## 13.1 Hackathon results

`POST /api/v1/projects/{id}/result` needs team status `active` and event phase `ended`. Who may submit:
- the lead, from `ends_at`;
- any confirmed team member, once `results_due_days` (7) have passed without a result;
- an admin, without waiting.

```json
{ "outcome": "finished|withdrawn", "place": 2, "award": "2nd place + sponsor prize",
  "repo_url": "...", "demo_url": "...", "presentation_url": "...",
  "member_areas": { "<user_id>": "backend" },
  "participants": { "<user_id>": true },
  "retrospective_md": "what was done / not done / problems / mistakes / notes per member" }
```

- `participants` covers every team member: who actually took part. Members marked `false` get no system confirmation; teammates can still confirm them as `peer` (§25.2).
- The retrospective is visible to team members and admins only.

Effects by outcome:

| | `finished` | `withdrawn` |
|---|---|---|
| `team_status` | `finished` | `withdrawn` |
| Project `status` | `shipped` if a repo or demo link is present, else `abandoned` (§27.1) | `abandoned` |
| `project_releases` row | Created when shipped | None |
| Participants' `project_members` fact | System confirmation (`trusted`) | None |
| Participants' attendance | `attended` with a system confirmation (§26.1) | Unchanged |
| Review window | Opened among participants; they get `reviews_open` | None |

- Both outcomes enqueue `recompute_member` for every team member.
- The lead can continue the project as a regular project later (§27.1).
- The submitter or the lead may edit a `finished` result while its review window is open; edits re-trigger recompute. Admins can edit any result.

**Place and award verification.** A claimed `place` or `award` starts as `place_status = unverified`.
- A parser winner list matching a team member sets it `verified` and takes the place from the list (§11.2).
- Otherwise an admin verifies or rejects it in the "Results to verify" queue (dashboard counter and weekly digest, no alert).
- Algo receives `place_status`; only a `verified` place earns a place bonus (§18.3).

**No result.** `results_close_days` (30) after `ends_at` without a result, the team auto-closes: `team_status = withdrawn`, project `abandoned`, `project_results.auto_closed = true`, no system confirmations, no review window. Members can still mark `attended` themselves within the attendance window (§26.1).

## 13.2 Reviews (all contexts)

| Subject | Opens when | Who rates whom | Context |
|---|---|---|---|
| Hackathon team | Result `finished` | Every participant ↔ every participant (`participants = true`) | `member`, `lead` |
| Project | Release (§27.5) | Members present ≥ `project_review_min_days` ↔ each other | `member`, `lead` |
| Mentorship | Close (§28.5) | Learner → mentor (rating); mentor → learner (endorsement) | `mentor` |
| Study pair | Close | Both ways | `member` |
| Member-hosted event | After `starts_at` | Attendee → host or speaker | `host`, `speaker` |

- **Windows** (`review_windows`) last `review_window_days` (7) from opening. Reviews are editable until the window closes.
- **Blind until close:** windowed reviews stay hidden from reviewees until the window closes, then are revealed together (`revealed_at`), and reviewees get a `review_received` digest.
- **Instant:** host and speaker ratings have no window. They are visible immediately, but only as an aggregate (average and count) once there are ≥ 3; individual comments are shown without reviewer names.
- Ratings are `1..10` with a comment ≤ 2000. Endorsements have no rating. One review per (window, reviewer, reviewee).
- **Reputation** per context (`member`, `lead`, `mentor`, `host` including `speaker`): plain average of revealed, non-removed ratings, with counts, computed by core. Algo receives raw reviews for matching (lead fit, recommendations, mentor matching), **never** for XP.
- **Reports:** any member can report a revealed review → admin alert `review_reported`. An admin may remove it (soft, with a reason; the reviewer gets `review_removed`). Algo may also mark reviews `to_check` (rating rings, §10.4).
- Reminder `reviews_pending` 2 days before the window closes, to members who haven't submitted.

## 13.3 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| POST | `/api/v1/projects/{id}/result` | lead; confirmed team member after `results_due_days`; admin | Result (§13.1) |
| PATCH | `/api/v1/projects/{id}/result` | submitter or lead while the window is open; admin | Result fields |
| GET | `/api/v1/projects/{id}/result` | member | Retrospective only for team members and admins |
| GET | `/api/v1/admin/results?place_status=unverified&cursor=` | admin | Results to verify, with parser winner candidates |
| POST | `/api/v1/admin/projects/{id}/result/verify` | admin | `{decision: verify \| reject, note?}` |
| GET | `/api/v1/me/reviews/pending` | member | → open windows with `to_review`, `submitted`, `closes_at` |
| PUT | `/api/v1/review-windows/{id}/reviews/{reviewee_id}` | window participant | `{rating?, comment}` |
| PUT | `/api/v1/events/{id}/host-reviews/{reviewee_id}` | member with `attended` | `{rating, comment}` |
| GET | `/api/v1/users/{id}/reviews?context=&cursor=` | member | Revealed, non-removed reviews |
| POST | `/api/v1/reviews/{id}/report` | member | `{reason}` |
| GET | `/api/v1/admin/reviews?reported=&cursor=` | admin | Reviews |
| POST | `/api/v1/admin/reviews/{id}/remove` | admin | `{reason}` |
