# 28. Core — Mentorship

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
  - Limits: one open request per area; at most `mentee_active_max` (2) active mentorships as a learner.
  - Expires after `help_request_ttl_days` (14) without an accepted offer.
- **Open request:** goes to the feed (`help_request`), and `match_mentors` (§14) picks the top `mentor_notify_top_n` (5) candidates: mentor offers in the area first, then strong members in that area. They get `help_request_match` with a **Help** button (web and Telegram).
- **Direct request** (`target_mentor_id`): sent only to that member, not to the feed. Only the target mentor can decline it, while it is in the `open` state. The learner is notified (`help_request_declined`) and can open it to everyone immediately, without the 3-day wait; otherwise they can open it after 3 days without an answer.
- **Offer:** `POST /api/v1/help-requests/{id}/offers {message}` by any onboarded member who isn't the learner and has capacity. The learner sees offers with each mentor's area score, mentor reputation and past mentorships, and accepts one. The other offers close, and their authors are notified.

## 28.4 Mentoring vs study pair

At acceptance, core classifies it once, deterministically:
- **mentoring** if the mentor's effective score in the area ≥ `mentor_min_score` (6.0) and ≥ the learner's + `mentor_min_gap` (2.0);
- otherwise a **study pair**: both sides are `peer`, nobody gets mentor reputation, and algo awards less.

The UI shows the classification before the learner accepts ("This will be a study pair: scores are close").

## 28.5 Active mentorship

- **Check-ins:** `POST /api/v1/mentorships/{id}/checkins {note, occurred_on, minutes?}` by either side; ≤ 1 per side per day. Check-ins are activity inputs.
- **Idle:** no check-in for `mentorship_idle_days` (14) → both reminded on day 10; on day 14 it becomes `stalled`. Any check-in reactivates it. A stalled mentorship closes automatically after another 14 days with no outcome.
- **Close:** the learner closes it with `outcome: reached|partial|not_reached` and an optional summary. The mentor can **propose** closing; the learner then gets 7 days to choose the outcome, after which it closes with no outcome.
  - Closing with an outcome is the learner's `trusted` confirmation of the mentor (§25.2).
  - The mentor confirms the learner's side by acknowledging the outcome or writing an endorsement.
  - Either side may cancel before the first check-in; nothing is recorded.
- **After close:** a 7-day review window (§13).
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

| Method | Path | Who |
|---|---|---|
| GET / PUT | `/api/v1/me/mentor-offers` | member (list / replace own offers and capacity: `{capacity?, offers: [{area_key, topics, active}]}`) |
| GET | `/api/v1/help-requests?area=&status=open&cursor=` | member |
| POST | `/api/v1/help-requests` | member |
| GET | `/api/v1/help-requests/{id}` | member (direct requests: learner and target only) |
| PATCH | `/api/v1/help-requests/{id}` | learner `{topic, goal, format}` while open |
| POST | `/api/v1/help-requests/{id}/cancel` | learner |
| POST | `/api/v1/help-requests/{id}/decline` | target mentor (open direct request) |
| POST | `/api/v1/help-requests/{id}/open` | learner (direct → open) |
| POST | `/api/v1/help-requests/{id}/offers` | member `{message}` |
| DELETE | `/api/v1/help-offers/{id}` | offer author |
| POST | `/api/v1/help-offers/{id}/accept` | learner |
| GET | `/api/v1/me/mentorships?role=mentor\|learner&status=` | member |
| GET | `/api/v1/mentorships/{id}` | the two sides, admins |
| POST | `/api/v1/mentorships/{id}/checkins` | either side |
| POST | `/api/v1/mentorships/{id}/close` | learner `{outcome, summary?}` |
| POST | `/api/v1/mentorships/{id}/propose-close` | mentor |
| POST | `/api/v1/mentorships/{id}/acknowledge` | mentor `{endorsement?}` |
| POST | `/api/v1/mentorships/{id}/cancel` | either side, before the first check-in |

## Done when
- A mentor can't exceed capacity, and a learner can't exceed `mentee_active_max` (concurrent accepts tested).
- Classification is stored at acceptance and never changes afterwards.
- Mentor XP never appears without the learner's outcome.
