# 25. Facts, confirmations, trust, feed

This section is the shared layer under events (§26), projects (§27) and mentorship (§28). Hackathon teams (§12) feed into it through their project rows.

## 25.1 Facts

A **fact** is one member's participation in something, with a role. Core stores facts and confirmations; algo turns them into points (§18).

| Fact | Row | Roles |
|---|---|---|
| Event attendance | `event_attendances` | attendee, speaker, organizer, host, volunteer |
| Project membership (incl. hackathon teams) | `project_members` | lead, member |
| Mentorship | `mentorships` (one fact per side) | mentor, learner, peer (study pair) |
| Solo log entry | `activity_entries` | – |

Every fact carries `trust` (`self`, `peer` or `trusted`), recomputed by core whenever its confirmations change. Only confirmations that are neither `ignored` nor withdrawn count:

- `trusted` if any confirmation comes from a trusted source for that fact type (§25.2), from the system, or from an admin;
- `peer` if at least one co-participant confirmed it;
- `self` otherwise.

## 25.2 Confirmations

`confirmations(fact_type, fact_id, confirmer_id | null, source, created_at)`. One per (fact, confirmer). Members can't confirm their own facts (403 `cannot_confirm_own_fact`).

| Fact | `peer` sources | `trusted` sources |
|---|---|---|
| Attendance | another member who marked `attended` at the same event; the creator of a member-created event (§26.2) | parser attendee lists (§19); a `finished` hackathon result for its participants (system) |
| Project membership | another member of the project | the project lead's contribution confirmation (§27.5); a `finished` hackathon result (system) |
| Project lead | a project member | ≥ 2 project members (together they make the lead `trusted`); a `finished` hackathon result (system) |
| Mentor side | – | the learner closing the mentorship with an outcome (§28.5) |
| Learner side | – | the mentor acknowledging the outcome or writing an endorsement (§28.5) |
| Study pair | the other peer | – |
| Solo entry | – | – (always `self`) |

- The confirmer can withdraw a confirmation within `confirmation_withdraw_days` (7); later → 409 `confirmation_withdraw_closed`.
- A confirmation from a member under a `confirming` restriction or with a `to_check` flag (§10.4) is stored but `ignored`.
- Confirmation requests are never one-by-one spam. They're batched as digests: "Confirm who you saw at X", "Confirm contributions to project Y".

## 25.3 Points

- Core never assigns point values. `compute_member` (§18) gets the member's full history: all facts with role, trust, confirmations (with each confirmer's account age, XP and restriction state), outcomes, durations, check-in counts and counterparts.
- Algo returns per-fact awards (`fact_awards`: xp, activity, reason) plus the totals.
- Required behavior (owned by algo, §18.3):
  - **Second-party rule:** `self` facts give activity and at most a token amount of XP;
  - trust, role, effort and outcome scale the award, so speaking > attending, shipped > abandoned, and goal reached > not reached;
  - **diminishing returns:** repeating the same counterpart, same kind or same short window earns less;
  - confirmations from new, low-XP or restricted accounts weigh less;
  - ratings never affect XP.
- Facts that are `to_check` (§10.4) get zero awards until resolved.

## 25.4 Feed

- `feed_items(kind, subject_type, subject_id, actor_id, area_ids[], created_at, hidden_at)`, written in the same transaction as the cause.

| kind | Created when |
|---|---|
| `project_recruiting` | a project opens or changes its "looking for" |
| `project_shipped` | a project becomes `shipped`: a release (§27.5) or a `finished` hackathon result with a link (§13.1) |
| `help_request` | a help request is opened |
| `event_going` | a member marks "going" (aggregated per event per day: "3 members are going to X") |
| `event_new` | a published event gets its first "going" from a member |
| `mentorship_completed` | a mentorship closes with "goal reached" (both sides named, no rating) |

- `GET /api/v1/feed?kind=&area=&mine_areas=true&cursor=` returns items, newest first. `mine_areas=true` keeps items matching the member's top 3 areas plus all `help_request` items they could mentor.
- Items are hidden when the subject becomes invisible (deleted, cancelled, request closed, item to check).
- DND members don't appear in `event_going` aggregates.

## 25.5 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/feed?kind=&area=&mine_areas=&cursor=` | member | Feed items, newest first |
| GET | `/api/v1/me/confirmations/pending` | member | Facts they can confirm, grouped by event or project |
| POST | `/api/v1/confirmations` | member | `{fact_type, fact_id}` |
| DELETE | `/api/v1/confirmations/{id}` | confirmer, within `confirmation_withdraw_days` | Withdraw |
| GET | `/api/v1/users/{id}/facts?type=&cursor=` | member | Unified history with trust badges and awards |

## 25.6 Done when

- Trust levels are always consistent with stored confirmations (property test over random confirmation sequences).
- A member can never raise their own trust level.
- Feed items never reference invisible subjects.
