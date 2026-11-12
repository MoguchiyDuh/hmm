# 4. Domain glossary and rules

## 4.1 Community and scoring

| Term | Meaning |
|---|---|
| **Member** | Any signed-in, onboarded user. |
| **Admin** | User with `is_admin`. Handles exceptions; never approves routine actions. |
| **Area** | Broad skill area: Backend, Frontend, ML/AI, Design, Mobile, DevOps, Product/PM, Data, Security, GameDev… Admin-managed list, ~8–12 items. Scores exist **only per area**; specific technologies live in free text. |
| **Area score** | 0.0–10.0 per (member, area). `effective = override ?? calculated`. |
| **XP** | Lifetime, monotonic-ish number (only admin penalties can lower it). |
| **Activity** | Recent-engagement number over a rolling window (default 14 days); decays. |
| **Reputation** | Average revealed rating per role context: teammate, lead, mentor, host/speaker (§13). |
| **Fact** | Anything that can earn points: an event attendance, a project membership, a mentorship, a solo log entry. Core stores facts; algo turns them into XP, activity and area scores (§25). |
| **Confirmation** | Another party vouching for a member's fact (§25). |
| **Trust level** | Per fact: `self` (no confirmation) < `peer` (a co-participant confirmed) < `trusted` (lead, organizer, host, mentee, the system, or an admin confirmed). |
| **To check** | A fact, confirmation or review flagged by algo as suspicious. It earns nothing until an admin pardons it or punishes the member (§10). |
| **Feed** | Community stream: projects looking for people, open help requests, who's going where, what shipped (§25). |

## 4.2 Events, projects, mentorship

| Term | Meaning |
|---|---|
| **Event** | Catalog entry with a date: hackathon, contest, conference, meetup, workshop, course, other. Imported by the parser, created by an admin, or created by a member (§11). |
| **Hackathon** | Any event with `formation_enabled` (default for `hackathon` and `contest`). Members enroll and get packed into teams (§12). |
| **Attendance** | A member's "going" or "attended" record for an event, with a role: attendee, speaker, organizer, host, volunteer (§26). |
| **Project** | A group effort with a lead, members, a goal, a plan (milestones), updates and results. Standalone or linked to an event (§27). |
| **Team** | A project linked to a hackathon and formed through §12. It has a formation lifecycle (`team_status`) on top of the project lifecycle, and can continue as a regular project after the event. |
| **Help request** | "I want to learn X": area, topic, goal. Mentors offer help; the learner picks one (§28). |
| **Mentorship** | An accepted help request between a mentor and a learner, with check-ins and an outcome. If the score gap is too small, it's a **study pair** instead (§28). |

## 4.3 Team formation

| Term | Meaning |
|---|---|
| **Enrollment** | A member's declared intent to join a hackathon through formation ("I'm going"), with an optional preferred area. |
| **Lead claim** | An eligible member (lead fit ≥ threshold) claims to lead a team. Creates a team in `forming`. |
| **Lead volunteer** | An enrolled member who ticked "I can lead if needed". Any fit; no core. Ranked above non-volunteers in the lead order. |
| **Lead order** | Ranking used whenever the system picks a lead: claimed lead > volunteer with fit ≥ threshold > volunteer below threshold > anyone else (§12.8). |
| **Lead offer** | A pending offer of the lead role to one member (formation, succession or transfer). Accepting is the only way to become lead, except a forced assignment. |
| **Proposal** | A placement made by the system or a lead. The member must confirm by `confirm_by`, or they are removed (`lapsed`). |
| **Core** | People a lead gathers before formation via invites and accepted join requests. Capped at `core_max` including the lead. |
| **Formation round** | One algorithm run that packs every unplaced enrollment into complete teams around the cores, proposes leads for extra teams, and ranks leftovers into the reserve. |
| **Review** | Window after formation: leads adjust rosters, members must confirm their placements. |
| **Lock** | End of review; rosters are fixed for participation. |
| **Reserve** | Ranked list of enrolled members not on a team. First source for vacancies and late teams. |
| **Target size `T`** | Preferred team size. Default `team_size_max`; admin may override per hackathon. |
| **Viable size `V`** | Smallest acceptable formed team: `max(team_size_min, ceil(T × viable_ratio))`. |
| **Lead fit** | Algo score 0–1 + reason for (member, hackathon). Above threshold → may claim lead with one click. |
| **Quick-pick** | Algo-ranked candidates for a team or project: enrolled/reserve members first, then everyone else to invite. |
| **DND** | Do-not-disturb: hidden from quick-pick and mentor matching, cannot be invited. Own actions still allowed. |

Participation phases per hackathon:

```text
open ──► forming ──► review ──► locked ──► closed
```

## 4.4 Global invariants

1. At most one live enrollment per member per hackathon.
2. A member is on at most one team per hackathon (proposed or confirmed).
3. Team size (the lead counts) ≤ `team_size_max`. Core size ≤ `core_max` until formation.
4. Teams are completed by the formation round, not one by one. Before formation only cores exist.
5. Formation never splits a core and never removes a lead who claimed.
6. Fewer complete teams beat more partial teams: formation maximizes placed members first, then minimizes the number of teams.
7. Algorithms propose, people decide. Nobody is on a team, project or mentorship without an explicit action of their own. Silence on a proposal counts as a decline: the member is removed when `confirm_by` passes.
8. Reviews do not affect XP. XP, activity, area scores and reputation are independent outputs.
9. The system acts, admins get warned. No admin approval on the routine path. Admin queues exist only for low-confidence imports, items to check and stuck states.
10. Every team that reaches `locked` has a lead. The role is offered down the lead order and forced only as a last resort (§12.8).
11. **Second-party rule.** XP needs someone else: a confirmation, a co-participant, a learner's outcome, or system evidence. Facts at trust level `self` earn activity and almost no XP.
12. Anything marked "to check" earns nothing until an admin resolves it. Members are told only when they're punished.
