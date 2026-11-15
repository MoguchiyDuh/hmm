# 12. Hackathon participation and team formation

This section applies to events with `formation_enabled` ("hackathons"). A team is a `projects` row with `team_status` set. After the result, the team continues under §27.

Formation works like a matchmaking queue: you queue alone or with a party of friends, and the queue builds teams. All mutations follow §3.4 (hackathon row lock) and §4.4.

## 12.1 Overview

```text
publish ─► OPEN ─────────────────► FORMING ─► FORMED ──────────────────────────► CLOSED
           queue solo or in a      one run    accept within 24 h,                 starts_at
           party, invite friends   builds     open seats and new teams keep
                                   teams      filling from the queue until reg_deadline
```

- A **party** is a group of friends who queue together. It is never split.
- A party may queue **open** (the queue adds people to reach the team size) or **closed** (the party is the whole team, nobody is added).
- Everyone placed on a team accepts or declines. Silence removes you, and the seat is refilled.

## 12.2 Sizes and timing

| Value | Rule |
|---|---|
| `max` | `hackathon.team_size_max ?? team_size_default_max` |
| `min` | `hackathon.team_size_min ?? team_size_default_min` |
| `T` target | `target_override ?? max`, within [`min`, `max`] (else 422) |

- `reg_deadline = registration_deadline ?? starts_at`.
- `formation_at = formation_at_override ?? reg_deadline − formation_lead_hours (72)`, and never earlier than `published_at + 1 h`. While `open`, it is recomputed when the dates change.
- `accept_by` of a placement = `min(placed_at + accept_hours (24), starts_at)`.
- Admins can "form now" while `open`.

## 12.3 Packing (`hmm.algo.pack.pack`)

A pure function. Core uses it for the live projection, as the fallback planner, and for every refill after formation. Algo plans must place at least as many people as it does.

```python
def pack(units: list[Unit], teams: list[OpenTeam], t: int, min_size: int, max_size: int) -> Packing
# Unit(id, size, queued_at); OpenTeam(id, size, banned_unit_ids)
# -> Packing(fills: dict[team_id, list[unit_id]], new_teams: list[list[unit_id]], queue: list[unit_id])
```

```text
queue order: (queued_at, id)
1. fill: for each open team, oldest first, take queue units in order whose size fits t − size
   (skip units in banned_unit_ids: people who already left that team)
2. new teams, over the units still queued, P = their total size:
   n = round(P / t), clamped to [ceil(P / max_size), floor(P / min_size)]; if that range is empty, n = floor(P / min_size)
   sort units by size desc, then queue order; put each into the smallest of the n teams it fits (size + unit ≤ max_size),
   else it stays queued
   if a unit stayed queued, run the same pass again putting each unit into the fullest team it fits instead,
   and keep whichever pass places more people (tie: the first)
   a team that ends below min_size returns its units to the queue
3. queue = every unit not placed, in queue order
```

Examples, `T = max = 5`, `min = 3`, no open teams:

| Units | Result |
|---|---|
| 7 solos | 2 teams (4, 3) |
| 8 solos | 2 teams (4, 4) |
| 11 solos | 3 teams (4, 4, 3) |
| 2 solos | 0 teams, 2 queued |
| parties 3 + 3 | 2 teams (3, 3) |
| parties 3 + 2 | 1 team (5) |
| party 4 + 4 solos | 2 teams (4, 4) |
| party 4 + party 3 + 1 solo | 2 teams (4, 4) |
| parties 2 + 2 + 2 | 1 team (4), one party of 2 queued |
| parties 3 + 3 + 2 + 2 + 2 with `T = max = 6` | 2 teams (6, 6) |
| 7 solos with `min = 4` | 1 team (5), 2 queued |

Closed parties are not units: each becomes a team as is. Lead choice (§12.7) is separate from packing.

## 12.4 Queue and parties (phase `open`, and later until `reg_deadline`)

**Enroll** (join the queue). `POST /api/v1/events/{id}/enroll {preferred_area_key?, note?, willing_to_lead?}`.
- Allowed while `published` and `now < reg_deadline`. In `forming` → 409 `formation_in_progress`.
- `open` → `active` (queued for the run). After formation → `reserve` (queued for refills).
- Re-enrolling after `withdrawn` or `lapsed` reuses the row and goes to the back of the queue.
- Withdraw any time before `starts_at` while not on a team; it also leaves your party.
- Other 409: `already_enrolled`, `already_in_team`, `enrollment_closed`.

**Parties.**
- **Create** `POST /api/v1/events/{id}/party`: an enrolled member not on a team creates a party and owns it. Creating or joining a party enrolls you if you aren't yet.
- **Invite** `POST /api/v1/parties/{id}/invites {user_id}`: any party member. The invitee must be onboarded, not banned, not DND, not on a team for this hackathon. Expires at `min(now + invite_ttl_days (7), reg_deadline)`.
- **Accept** an invite: you join the party; if you were in another party you leave it first. Party size ≤ `max` (409 `party_full`).
- **Leave** `POST /api/v1/parties/{id}/leave`. The owner leaving passes ownership to the earliest joined member; the last member leaving deletes the party.
- **Kick** `DELETE /api/v1/parties/{id}/members/{user_id}`: owner only.
- **Open/closed** `PATCH /api/v1/parties/{id} {closed: bool}`: owner only. `closed` needs size ≥ `min` (409 `party_too_small`). A closed party that drops below `min` reopens.
- A party's queue position is its `created_at`.
- A party exists until it is placed. Placed members are ordinary team members; the party row is deleted.

Allowed in `open`, and after formation until `reg_deadline` (a party queued after formation is placed by refills, §12.6). Every participation mutation during `forming` → 409 `formation_in_progress`.

## 12.5 Formation run

Job `run_formation(hackathon)`, enqueued by `event_tick` at `formation_at` or by "form now".

1. **Start:** lock the hackathon, phase `forming`. Pending party invites close.
2. **Plan:** closed parties become teams as they are. Open parties and solos go to `algo.form_teams` (§18.2, timeout 120 s). Core validates the plan:
   - every unit appears once, whole, on one team or in the queue;
   - every team size is in [`min`, `max`];
   - placed people ≥ `pack()` placed people.
   An invalid plan or algo failure → `pack()` is used, and admins get alert `formation_failed`.
3. **Apply** (one transaction): teams are created (`team_status = formed`, `origin = formation`) with a lead (§12.7). Every placed member is `proposed` with `accept_by`. Enrollments become `placed`; queued ones become `reserve`. Phase `formed`.
4. **Notify:** placed members get `team_proposed` (accept / decline). The queue gets `reserve_placed`.

If the job fails for good, the phase returns to `open` with `formation_at = now + 1 h`, and admins get `formation_failed`. With nobody enrolled, the phase goes straight to `formed`.

## 12.6 After formation (phase `formed`, until `starts_at`)

**Placed member:**
- `accept` → `confirmed`.
- `decline {withdraw: bool}` → leaves the team; back to the queue (`reserve`, back of the line) or `withdrawn`.
- No answer by `accept_by` → removed (`lapsed`), and they get `placement_lapsed`.
- One reminder (`accept_reminder`) 6 h before `accept_by`.

**Team (lead):** rename, set member areas, transfer the lead (immediate, to a confirmed member), kick a member (→ queue), invite anyone eligible until `reg_deadline` (they join `confirmed` on accept; size ≤ `max`), and **close/open seats** (`closed` needs size ≥ `min`; closed seats stop refills only, not the lead's invites). Members may leave at any time (→ queue or withdraw).

**Refill** (`event_tick`, until `reg_deadline`): `pack()` runs over the queue with every non-closed team below `T` as an open team. Fills and new teams are applied like §12.5 step 3 (placed members accept), and the team's lead gets `team_member_joined` when someone accepts. Nobody is placed back on a team they left.

**At `reg_deadline`:** refills and invites stop, pending invites expire. Placements already proposed keep their `accept_by`; a seat freed after this stays empty. Teams below `min` are kept, and admins get alert `team_below_min` (info).

**Dates changed after formation:** existing `accept_by` values stay; new placements and the `starts_at` step use the new dates.

**At `starts_at`:** members still `proposed` are removed (`lapsed`); empty teams are `disbanded`; the others become `active`; the phase becomes `closed`; queued enrollments become `expired`. From then on, adding members is admin-only; members may still leave.

## 12.7 Lead

- **At placement:** the owner of the biggest party on the team (ties: the earliest party); otherwise the earliest-queued member who ticked "I can lead if needed"; otherwise the earliest-queued member.
- **Transfer** `POST /api/v1/projects/{id}/transfer-lead {user_id}`: immediate, to any confirmed member.
- **When the lead leaves, declines, lapses or is banned:** the role passes at once by the same rule among the remaining members, confirmed members first. The new lead gets `lead_assigned`.
- A team without members has no lead and is disbanded.
- Every lead change sends `lead_assigned` to the new lead.

**Bans** (§6.4): the user leaves their party and the queue, their pending invites close, and they are removed from `formed` teams (the lead role passes). On an `active` or later team they stay for history; if they lead it, the role passes.

**Attendance** (§26.1): `withdrawn`, `lapsed` and `expired` enrollments cancel a `going` attendance; going back to the queue keeps it.

## 12.8 Team states

| From | Event | To |
|---|---|---|
| – | Formation or refill creates the team | `formed` |
| `formed` | `starts_at` | `active` |
| `formed` | No members left; admin disband | `disbanded` |
| `active` | Result submitted | `finished` / `withdrawn` (§13) |
| `active` | No result by `results_close_days` | `withdrawn` |
| any non-terminal | Hackathon cancelled | `cancelled` |

## 12.9 Timers (`event_tick`, every 5 min)

| Condition | Action |
|---|---|
| `open`, `now ≥ formation_at` | Enqueue `run_formation` |
| `open`, `now ≥ formation_at − 24 h` | `formation_soon` to the queue, once |
| Proposed member, `accept_by − 6 h` | `accept_reminder` |
| Proposed member past `accept_by` | Remove (`lapsed`) |
| `formed`, `now < reg_deadline` | Refill (§12.6) |
| `reg_deadline` | Expire invites; alert teams below `min` |
| `starts_at` | §12.6 "At `starts_at`" |
| Invite past `expires_at` | `expired` |

## 12.10 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/events/{id}/formation` | member | phase, `T`, `min`, `max`, `formation_at`, counts, live projection (`pack()`), my state, `allowed_actions` |
| POST | `/api/v1/events/{id}/enroll` | member | `{preferred_area_key?, note?, willing_to_lead?}` |
| PATCH | `/api/v1/events/{id}/enroll` | enrolled member | same fields |
| DELETE | `/api/v1/events/{id}/enroll` | enrolled member not on a team | withdraw |
| GET | `/api/v1/events/{id}/enrollments?cursor=` | member | queue and teams |
| GET | `/api/v1/me/enrollments?status=` | member | own enrollments |
| POST | `/api/v1/events/{id}/party` | member | create a party → `Party` |
| GET | `/api/v1/parties/{id}` | party member, admin | `Party` (members, owner, closed, pending invites) |
| PATCH | `/api/v1/parties/{id}` | owner | `{closed}` |
| POST | `/api/v1/parties/{id}/invites` | party member | `{user_id}` |
| POST | `/api/v1/parties/{id}/leave` | party member | – |
| DELETE | `/api/v1/parties/{id}/members/{user_id}` | owner | kick |
| GET | `/api/v1/me/invites?status=` | member | own invites (party and team) |
| POST | `/api/v1/invites/{id}/accept` | invitee | – |
| POST | `/api/v1/invites/{id}/decline` | invitee | – |
| DELETE | `/api/v1/invites/{id}` | inviter | withdraw |
| POST | `/api/v1/projects/{id}/placement/accept` | proposed member | – |
| POST | `/api/v1/projects/{id}/placement/decline` | proposed member | `{withdraw}` |
| POST | `/api/v1/projects/{id}/invites` | lead, `formed` | `{user_id}` |
| PATCH | `/api/v1/projects/{id}/seats` | lead, `formed` | `{closed}` |
| POST | `/api/v1/projects/{id}/transfer-lead` | lead | `{user_id}` |
| DELETE | `/api/v1/projects/{id}/members/{user_id}` | lead, `formed` | kick → queue |
| PATCH | `/api/v1/projects/{id}/members/{user_id}` | lead | `{area_key}` |
| POST | `/api/v1/projects/{id}/leave` | member | `{withdraw}` |
| PATCH | `/api/v1/admin/events/{id}/formation` | admin | `{target_override, formation_at_override}` |
| POST | `/api/v1/admin/events/{id}/formation/run` | admin | form now (`open`) |
| GET | `/api/v1/admin/events/{id}/formation/runs` | admin | input, output, planner, errors |
| POST | `/api/v1/admin/projects/{id}/members` | admin | `{user_id}` → confirmed (ignores closed seats, size ≤ `max`) |
| DELETE | `/api/v1/admin/projects/{id}/members/{user_id}` | admin | remove → queue before `starts_at`, `withdrawn` after |
| PUT | `/api/v1/admin/projects/{id}/lead` | admin | `{user_id}` |
| POST | `/api/v1/admin/projects/{id}/disband` | admin | `{reason}` |

## 12.11 Done when

- `pack()` matches the examples above and, for random inputs (≤ 40 people, parties 1…`max`), never splits a unit, never builds a team outside [`min`, `max`], and never fills an open team past `T`.
- Every applied plan passes the §12.5 checks; a failing algo falls back to `pack()`.
- Concurrency: parallel invite accepts and refills never exceed `max` or put a member on two teams; mutations during `forming` get 409.
- Nobody is `confirmed` on a team without their own action (accepted placement or invite), except admin adds.
- Time-travel tests cover every row of §12.9.
