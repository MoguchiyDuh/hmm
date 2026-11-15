# 23. Delivery plan

Full scope, one release. Work runs in five phases; inside a phase every group works in parallel against fakes (§2.5, CONTRIBUTING.md) until the real dependency lands. A phase is done when its checkpoint passes on the dev stack. Each phase is a GitHub milestone; each issue names its spec sections, dependencies and done-when.

## 23.1 Phase 1: Foundation

- `contracts/`: common types, `algo.py`, `formation.py` (`pack()` + §12.3 example and property tests), `imports.py`, `notifications.py`, fixtures (members, events, formation inputs, mentorship cases, community graphs with planted rings, imports, notifications, visibility cases).
- core: schema baseline, settings, audit, job worker, stub `openapi.json` covering every endpoint (unimplemented handlers return 501), GitHub sign-in, sessions, bans, Telegram linking, service tokens.
- Fakes: `hmm_algo.testing.FakeAlgorithm` with conformance tests, `hmm-bot-stub-core`, MSW mocks.
- Every group has its skeleton running in the dev stack (or `pnpm dev` for web); CI green; production stack and release workflow in place (§22).
- After this phase, `contracts/` changes only through a contract-change issue (CONTRIBUTING.md).

## 23.2 Phases 2–5

| Phase | core | algo | parser | bot | frontend |
|---|---|---|---|---|---|
| 1. Foundation | schema, settings, audit, jobs, stub OpenAPI, sign-in, sessions, bans, Telegram linking, service tokens | `FakeAlgorithm`, conformance tests | source survey, fetch + classify dry-run | aiogram skeleton, stub core | app shell, routing, generated client, MSW, i18n (en/ru) |
| 2. Registration and catalog | job registry + algorithm wiring, areas, quiz builder + onboarding, profiles, GitHub sync, CV, scores, member review, solo log, catalog + import + review queue, queue and parties, enrollments, invites | `score_onboarding` + evaluation harness, `compute_member`, `recommend` | extraction + normalization, area mapping, push to core, revisit loop, heartbeat, dedup | linking, notification loop, templates, inline actions, `/events`, `/enroll`, `/invites` | auth flow, onboarding wizard (incl. CV step), settings, profile, directory, catalog, event page, participation panel (open phase, party lobby) |
| 3. Hackathon lifecycle | `run_formation` + `pack()` fallback, placement accept/decline, refills, lead rule, `event_tick`, results (participants, member fallback, auto-close, place verification), reviews, deliveries, admin alerts, admin endpoints | `form_teams`, points over all facts (§18.3), LLM reasons, caching | difficulty, all event kinds, people lists | `/team`, placement accepts, throttling, error texts | formation panel (forming, formed, accept), results + reviews, notifications, admin panel |
| 4. Community | attendance, member events, standalone projects, mentorship, confirmations + trust, feed, to-check + penalties + restrictions | `match_mentors`, `scan_community`, abuse flags, project quick-pick | revisit ended events for results | `/ask`, `/requests`, `/mentorships`, `/confirm`, new templates | home feed, attendance panel, projects, mentorship, profile history + reputation, admin to-check queue |
| 5. Release | property, concurrency and timer tests; bug fixes only | performance + eval report; `algo/README.md` release notes | quality report, full run against real sources | full run on the dev stack | end-to-end pass, mobile layout |

Phase 5 ends with the acceptance scenario (§23.4) passing and a `v*` release tag deployed with the release workflow (§22.3).

## 23.3 Checkpoints

1. **Foundation:** contracts frozen; stub OpenAPI published; `docker compose up` brings up every service healthy; CI green; the bot links an account through core.
2. **Registration and catalog:** frontend signs in via real GitHub; full registration (quiz + CV) → scores on profile; admin completes a member review with an override; parser imports real events; members enroll and create a party.
3. **Hackathon lifecycle:** queue and parties → formation → accepts → refills until `reg_deadline` → results, reviews, XP; placement accepted from Telegram; admin handles every alert type.
4. **Community:** a project ships with confirmed contributions; a mentorship runs from request to reviews; attendance gets confirmed; a planted ring lands in the to-check queue and gets punished.
5. **Release:** the scenario below passes on the dev stack with real GitHub OAuth and real parser sources; the release is tagged and deployed (§22).

A checkpoint at risk is raised in the group's issue with `status:blocked`, and people move from groups that are ahead.

## 23.4 End-to-end acceptance scenario

1. Parser imports a hackathon: primary ML/AI, required Backend + Frontend, team 3–6, registration deadline in 6 days.
2. Fourteen members sign in via GitHub, complete onboarding along different branches, half upload CVs, some link Telegram. An admin reviews two of them and overrides one score with `evidence=interview`.
3. Twelve members enroll for the hackathon: a closed party of 4, two open parties of 4, and four solos. A member creates the open parties and invites friends; one invitee lets the invite expire.
4. The projection on the event page shows "3 teams (6, 6, 4), nobody queued" before formation.
5. `event_tick` runs `run_formation` at `formation_at`: the closed party becomes one team of 4, and the two open parties plus the solos pack into two teams of 6.
6. Placed members accept within `accept_hours`; one declines and goes back to the queue, and the next refill places them on a team with an open seat.
7. The lead of the smallest team closes its seats (size ≥ `min`); the other teams keep seats open, so a member who leaves is refilled from the queue (§12.6).
8. Teams are led by the owner of their biggest party (§12.7); when a lead leaves, the role passes at once to a confirmed member.
9. A member placed by a refill shortly before the start is still `proposed` at `starts_at` → removed (`lapsed`), and that team plays one short. Teams become `active`.
10. The hackathon ends → a lead submits the result with the participants checklist, a retrospective and a claimed 2nd place (`unverified`). Another team's lead stays silent; after 7 days a confirmed member submits instead. The parser's winner list then matches the first team and verifies the place.
11. Members rate each other; the window closes; reviews are revealed on profiles.
12. XP, activity and area scores update with reasons; profile history shows the hackathon.
13. A second hackathon (team 2–4) gets 8 solo enrollments. Formation packs them into 2 teams (4, 4); the leads are the earliest-queued volunteers.
14. A member creates a standalone project, recruits 2 people through the feed, completes milestones and ships a release; the lead confirms contributions; the 7-day review window reveals ratings; XP lands with reasons.
15. A member asks for help with ML via the bot's `/ask`; 5 mentors are notified on Telegram; one offers; it's classified as mentoring; after 3 check-ins the learner closes with "goal reached" and rates the mentor; the mentor gets XP and mentor reputation.
16. Three members mark a meetup as attended and confirm each other; trust becomes `peer`. Two fresh accounts confirm each other at five member-created events in a week; `scan_community` flags the ring; an admin punishes with exclusion and a `confirming` restriction.
