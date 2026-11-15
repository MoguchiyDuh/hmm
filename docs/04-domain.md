# 4. Domain glossary and rules

## 4.1 Community and scoring

| Term | Meaning |
|---|---|
| **User** | Anyone who signed in with GitHub. |
| **Member** | A user who finished onboarding (`completed` or `reopened`). |
| **Admin** | User with `is_admin`. Handles exceptions; never approves routine actions. |
| **Area** | Broad skill area: Backend, Frontend, Mobile, ML/AI, Data, Design, Product, DevOps, Security, GameDev, Hardware, QA (§8). Admin-managed list of about 8–12 items. Scores exist **only per area**; specific technologies live in free text. |
| **Area score** | 0.0–10.0 per (member, area). `effective = override ?? calculated`. |
| **XP** | Lifetime experience number. Algo recomputes it from all facts, so it can drop when a fact loses its confirmations or is excluded; admin penalties also lower it. |
| **Activity** | Recent-engagement number over a rolling window, `activity_window_days` (14); decays. |
| **Reputation** | Average revealed rating per context: `member`, `lead`, `mentor`, `host` (which includes speaker) (§13.2). |
| **Fact** | Anything that can earn points: an event attendance, a project membership, a mentorship side, a solo log entry. Core stores facts; algo turns them into XP, activity and area scores (§25). |
| **Confirmation** | Another party vouching for a member's fact (§25.2). |
| **Trust level** | Per fact: `self` (no confirmation) < `peer` (a co-participant confirmed) < `trusted` (a trusted source for that fact type, the system or an admin confirmed; §25.2). |
| **To check** | A fact, confirmation or review flagged by algo as suspicious. It earns nothing until an admin pardons it or punishes the member (§10.4). |
| **Feed** | Community stream: projects looking for people, open help requests, who's going where, what shipped (§25.4). |

## 4.2 Events, projects, mentorship

| Term | Meaning |
|---|---|
| **Event** | Catalog entry with a date: hackathon, contest, conference, meetup, workshop, course, other. Imported by the parser, created by an admin, or created by a member (§11). |
| **Hackathon** | Any event with `formation_enabled` (default for `hackathon` and `contest`). Members enroll and get packed into teams (§12). |
| **Attendance** | A member's "going" or "attended" record for an event, with a role: attendee, speaker, organizer, host, volunteer (§26). |
| **Project** | A group effort with a lead, members, a goal, a plan (milestones), updates and results. Standalone or linked to an event (§27). |
| **Team** | A project linked to a hackathon and formed through §12. It has a formation lifecycle (`team_status`) on top of the project lifecycle, and can continue as a regular project after the event. |
| **Help request** | "I want to learn X": area, topic, goal. Mentors offer help; the learner picks one (§28). |
| **Mentorship** | An accepted help request between a mentor and a learner, with check-ins and an outcome. If the score gap is too small, it's a **study pair** instead (§28.4). |

## 4.3 Team formation

| Term | Meaning |
|---|---|
| **Enrollment** | A member's place in a hackathon queue: status `active` before formation, `reserve` after, `placed` once on a team, with an optional preferred area, a note and "I can lead if needed". |
| **Queue** | Enrollments waiting to be placed, ordered by `queued_at`. Formation fills teams from it, and refills keep drawing on it until `reg_deadline`. |
| **Party** | Friends who queue together and are never split. **Open** (the queue adds people up to team size) or **closed** (the party is the whole team). Owned by one member, who invites, kicks and toggles `closed`. |
| **Unit** | What packing places: a solo member or an open party, with its size and queue time. |
| **Packing** | `pack()`: fills open teams, then builds new ones from the queue. Never splits a unit, never builds a team outside [`min`, `max`] (§12.3). |
| **Placement** | A proposed seat on a team. The member accepts or declines by `accept_by`; silence removes them (`lapsed`) and the seat goes back to the queue. |
| **Lead** | One member per team. Picked at placement by a fixed rule; transfer is immediate, and the role passes at once when it empties (§12.7). |
| **Open seat** | A team below target size `T` with seats open. Refills fill it from the queue until `reg_deadline`; the lead may close the seats (§12.6). |
| **Target size `T`** | Preferred team size. Default `team_size_max`; an admin may override it per hackathon. |
| **Reserve** | The queue after formation: enrollments waiting for an open seat or a new team. |
| **Quick-pick** | Algo-ranked candidates for a project: reserve or enrolled members first, then everyone else to invite. |
| **DND** | Do not disturb: hidden from quick-pick and mentor matching, cannot be invited. Own actions are still allowed. |

Participation phases per hackathon:

```text
open ──► forming ──► formed ──► closed
```

## 4.4 Global invariants

1. At most one live enrollment per member per hackathon.
2. A member is on at most one team per hackathon (proposed or confirmed).
3. A team's size is within [`min`, `max`] when a plan or refill creates it and never above `max`; a party is never split.
4. The queue builds teams, not people acting one at a time: one formation run places everyone it can, and refills keep filling open seats until `reg_deadline`.
5. Formation places at least as many people as `pack()`; an algo plan that places fewer is rejected (§12.5).
6. Every unit appears once, whole, on one team or in the queue.
7. Algorithms propose, people decide. Nobody is on a team, project or mentorship without an explicit action of their own. Silence on a placement counts as a decline: the member is removed when `accept_by` passes.
8. Reviews do not affect XP. XP, activity, area scores and reputation are independent outputs.
9. The system acts, admins get warned. No admin approval on the routine path. Admin queues exist only for low-confidence imports, items to check, results to verify and stuck states.
10. Every team has a lead from the moment it exists. When the lead leaves, declines, lapses or is banned, the role passes at once (§12.7).
11. **Second-party rule.** XP needs someone else: a confirmation, a co-participant, a learner's outcome, or system evidence. Facts at trust level `self` earn activity and almost no XP.
12. Anything marked "to check" earns nothing until an admin resolves it. Members are told only when they're punished.
13. No eligibility guards. Event `eligibility` (age, student status, country) is informational text, and members may join teams in overlapping events. Members decide.
