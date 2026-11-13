# 28. Mentorship

## 28.1 Flow

```text
help request ──► mentors notified ──► offers ──► learner accepts one ──► mentorship (active)
   (open)          (algo-matched)                                      check-ins
                                                                           │
                       closed (outcome + ratings) ◄── learner closes ◄──────┘
                       stalled ◄── no check-in for mentorship_idle_days
```

## 28.2 Mentor availability

- `mentor_offers(user_id, area_id, topics text, active)`: "I can help with X". One per (member, area).
- Shown on the profile and in the directory filter "mentors in area X". Used by matching.
- `users.mentor_capacity` (null = `mentor_capacity_default`, 3) caps a member's active mentorships as mentor across all areas, with or without offers.
- DND hides mentor offers and pauses matching notifications.

## 28.3 Help requests

- `POST /api/v1/help-requests {area_key, topic, goal, format: online|offline|any, target_mentor_id?}`.
  - `topic`: what to learn ("FastAPI auth", "first Kaggle competition"), ≤ 200 chars. `goal`: what "done" looks like, ≤ 1000 chars.
  - Limits: one open or direct request per area (409 `help_request_open`); at most `mentee_active_max` (2) active mentorships as a learner (409 `mentee_limit_reached`).
  - Expires after `help_request_ttl_days` (14) without an accepted offer; pending offers close, and the learner gets `help_request_expired`.
- **Open request:** goes to the feed (`help_request`), and `match_mentors` (§14) picks the top `mentor_notify_top_n` (5) candidates: mentor offers in the area first, then strong members in that area. They get `help_request_match` with a **Help** button (web and Telegram).
- **Direct request** (`target_mentor_id`): status `direct`, sent only to that member (`help_request_direct`), not to the feed.
  - Only the target mentor can decline it, while it is `direct`. The learner gets `help_request_declined` and can open it to everyone at once.
  - Without a decline, the learner can open it to everyone 3 days after creating it if no offer came.
  - A target on DND → 409 `user_dnd` (§7.4).
- **Offer:** `POST /api/v1/help-requests/{id}/offers {message}` by any member who isn't the learner, has free capacity (409 `mentor_capacity_reached`) and has no `mentoring` restriction (403 `restricted`).
  - The learner gets `help_offer_received` and sees offers with each mentor's area score, mentor reputation and past mentorships, then accepts one.
  - Capacity and `mentee_active_max` are checked again at acceptance.
  - The other offers close, and their authors get `help_offer_closed`.

## 28.4 Mentoring vs study pair

At acceptance, core classifies it once, deterministically:
- **mentoring** if the mentor's effective score in the area ≥ `mentor_min_score` (6.0) and ≥ the learner's + `mentor_min_gap` (2.0);
- otherwise a **study pair**: both sides are `peer`, nobody gets mentor reputation, and algo awards less.

The UI shows the classification before the learner accepts ("This will be a study pair: scores are close").

## 28.5 Active mentorship

- **Check-ins:** `POST /api/v1/mentorships/{id}/checkins {note, occurred_on, minutes?}` by either side; at most one per side per day (409 `checkin_limit`). Check-ins are activity inputs.
- **Idle:** both sides get `mentorship_idle` 4 days before `mentorship_idle_days` (14) pass without a check-in; at `mentorship_idle_days` it becomes `stalled` (`mentorship_stalled`). Any check-in reactivates it. A stalled mentorship closes automatically with no outcome after another 14 days.
- **Close:** the learner closes it with `outcome` (`reached`, `partial` or `not_reached`) and an optional summary. The mentor can **propose** closing (status `closing`, the learner gets `mentorship_close_proposed`); the learner then has 7 days to choose the outcome, after which it closes with no outcome.
  - Closing with an outcome is the learner's `trusted` confirmation of the mentor (§25.2).
  - The mentor confirms the learner's side by acknowledging the outcome or writing an endorsement.
  - Either side may cancel before the first check-in: status `cancelled`, nothing is recorded, no review window.
- **After close** (with or without an outcome): a review window of `review_window_days` (7) (§13.2).
  - The learner rates the mentor 1–10 with a comment (context `mentor`).
  - The mentor writes an optional endorsement for the learner: text only, no number.
  - In a study pair, both rate each other (context `member`).
  - Everything is revealed together when the window closes.

## 28.6 Points (inputs for algo)

Core passes for each side: role, area, classification, duration, check-in counts per side, outcome, the learner's area score at start and now, the counterpart, and how many earlier mentorships the same pair had. Algo decides the awards (§18.3):
- the mentor earns XP scaled by outcome and effort;
- the learner earns activity, plus a small XP bonus for `reached` when the mentor confirmed it;
- repeat pairs and reciprocal pairs (A mentors B in X, B mentors A in Y) earn sharply less, and algo flags rings.

## 28.7 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/me/mentor-offers` | member | Own offers and capacity |
| PUT | `/api/v1/me/mentor-offers` | member | Replace: `{capacity?, offers: [{area_key, topics, active}]}` |
| GET | `/api/v1/help-requests?area=&status=open&cursor=` | member | Requests |
| POST | `/api/v1/help-requests` | member | `{area_key, topic, goal, format, target_mentor_id?}` |
| GET | `/api/v1/help-requests/{id}` | member; direct requests: learner and target only | Request |
| PATCH | `/api/v1/help-requests/{id}` | learner, while `open` or `direct` | `{topic, goal, format}` |
| POST | `/api/v1/help-requests/{id}/cancel` | learner | – |
| POST | `/api/v1/help-requests/{id}/decline` | target mentor, while `direct` | – |
| POST | `/api/v1/help-requests/{id}/open` | learner | `direct` → `open` |
| POST | `/api/v1/help-requests/{id}/offers` | member | `{message}` |
| DELETE | `/api/v1/help-offers/{id}` | offer author | Withdraw |
| POST | `/api/v1/help-offers/{id}/accept` | learner | → mentorship |
| GET | `/api/v1/me/mentorships?role=&status=` | member | `role` = `mentor` or `learner` |
| GET | `/api/v1/mentorships/{id}` | learner, mentor, admin | Mentorship |
| POST | `/api/v1/mentorships/{id}/checkins` | learner, mentor | `{note, occurred_on, minutes?}` |
| POST | `/api/v1/mentorships/{id}/close` | learner | `{outcome, summary?}` |
| POST | `/api/v1/mentorships/{id}/propose-close` | mentor | – |
| POST | `/api/v1/mentorships/{id}/acknowledge` | mentor | `{endorsement?}` |
| POST | `/api/v1/mentorships/{id}/cancel` | learner, mentor; before the first check-in | – |

## 28.8 Done when

- A mentor can't exceed capacity, and a learner can't exceed `mentee_active_max` (concurrent accepts tested).
- Classification is stored at acceptance and never changes afterwards.
- Mentor XP never appears without the learner's outcome.
