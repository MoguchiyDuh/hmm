# 25. Core — Facts, confirmations, trust, feed

This section is the shared layer under events (§26), projects (§27) and mentorship (§28). Hackathon teams (§12) feed into it through their project rows.

## 25.1 Facts

A **fact** is one member's participation in something, with a role. Core stores facts and confirmations; algo turns them into points (§18).

| Fact | Row | Roles |
|---|---|---|
| Event attendance | `event_attendances` | attendee, speaker, organizer, host, volunteer |
| Project membership (incl. hackathon teams) | `project_members` | lead, member |
| Mentorship | `mentorships` (one fact per side) | mentor, mentee, peer (study pair) |
| Solo log entry | `activity_entries` | – |

Every fact carries `trust` (`self|peer|trusted`), recomputed by core whenever its confirmations change:

- `trusted` if any confirmation comes from a trusted source for that fact type (below), from the system, or from an admin;
- `peer` if confirmed by at least one co-participant;
- `self` otherwise.

## 25.2 Confirmations

`confirmations(fact_type, fact_id, confirmer_id | null, source, created_at)`. One per (fact, confirmer). Members can't confirm their own facts.

| Fact | `peer` sources | `trusted` sources |
|---|---|---|
| Attendance | another member who marked `attended` at the same event | the event's member organizer/host; parser attendee lists (§19); a finished hackathon team (system) |
| Project membership | another member of the project | the project lead's contribution confirmation (§27.5); a submitted hackathon result (system) |
| Project lead | a project member | – (a lead is `trusted` once ≥ 2 members confirmed them) |
| Mentor side | – | the learner closing the mentorship with an outcome (§28.5) |
| Mentee side | – | the mentor confirming the outcome or writing an endorsement |
| Study pair | the other peer | – |
| Solo entry | – | – (always `self`) |

- Confirmations can be withdrawn by the confirmer within `confirmation_withdraw_days` (7).
- A confirmation from a member under a `confirming` restriction (§10) is stored but ignored.
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
- Facts that are `to_check` (§10) get zero awards until resolved.

## 25.4 Feed

- `feed_items(kind, subject_type, subject_id, actor_id, area_ids[], created_at, hidden_at)`, written in the same transaction as the cause.

| kind | Created when |
|---|---|
| `project_recruiting` | a project opens or changes its "looking for" |
| `project_shipped` | a project ships a result, or a hackathon team submits one |
| `help_request` | a help request is opened |
| `event_going` | a member marks "going" (aggregated per event per day: "3 members are going to X") |
| `event_new` | a published event gets its first "going" from a member |
| `mentorship_completed` | a mentorship closes with "goal reached" (both sides named, no rating) |

- `GET /api/v1/feed?kind=&area=&mine_areas=true&cursor=` returns items, newest first. `mine_areas=true` keeps items matching the member's top 3 areas plus all `help_request` items they could mentor.
- Items are hidden when the subject becomes invisible (deleted, cancelled, request closed, item to check).
- DND members don't appear in `event_going` aggregates.

## 25.5 Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/feed` | member |
| GET | `/api/v1/me/confirmations/pending` | member → facts they can confirm, grouped by event/project |
| POST | `/api/v1/confirmations` | member `{fact_type, fact_id}` |
| DELETE | `/api/v1/confirmations/{id}` | confirmer, within 7 days |
| GET | `/api/v1/users/{id}/facts?type=&cursor=` | member → unified history with trust badges and awards |

## Done when
- Trust levels are always consistent with stored confirmations (property test over random confirmation sequences).
- A member can never raise their own trust level.
- Feed items never reference invisible subjects.
