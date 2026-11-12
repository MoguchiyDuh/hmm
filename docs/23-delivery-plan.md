# 23. Delivery plan

Two weeks, full scope, one release. Day 1 is 2026-11-13, day 14 is 2026-11-26.

## Days 1–2 — contracts freeze (all groups)
- `contracts/`: common types, `algo.py`, `formation.py` (`placement_bounds()` + tests), `imports.py`, `notifications.py`, fixtures (members, events, formation inputs, mentorship cases, community graphs with planted rings, imports, notifications, visibility cases).
- core publishes a stub `openapi.json` covering all §6–§16 and §25–§28 endpoints (handlers may return 501). Frontend and bot start against it plus MSW/stub server.
- Error `code` list agreed.
- Every group has its skeleton running in compose (or `pnpm dev` for web) and CI green.
- After day 2, `contracts/` changes only through a contract-change issue (CONTRIBUTING.md).

## Parallel tracks

| Days | core | algo | parser | bot | frontend |
|---|---|---|---|---|---|
| 1–2 | schema baseline + migrations, settings, audit, job worker, stub OpenAPI | contract review, `FakeAlgorithm`, contract tests | source survey, fetch + classify pipeline, dry-run | aiogram skeleton, stub server | app shell, routing, generated client, MSW, i18n (en/ru) |
| 3–4 | auth, sessions, users, areas, Telegram linking, quiz (builder API, evaluator, onboarding) | `score_onboarding`, evaluation harness | extraction + normalization, area mapping | linking, notification loop, templates | auth flow, onboarding wizard (incl. CV step), settings |
| 5–7 | GitHub sync, CV, scores, member review, solo log, catalog + import + review queue, enrollments, claims, cores, invites, join requests | `compute_member`, `lead_fit`, `recommend` | push to core import, revisit loop, heartbeat, dedup | inline actions, `/events`, `/enroll`, `/invites` | profile, directory, catalog, event page, participation panel (open phase) |
| 8–10 | `placement_bounds`, fallback planner, `run_formation`, review actions (incl. re-confirmation), lock, timers, swaps, merges, open seats + autofill, late round, presence check, lead offers, results (participants, member fallback, auto-close, place verification), generic reviews, deliveries, admin alerts | `form_teams`, `rank_reserve`, points over all facts (§18.3 #13), LLM reasons, caching | difficulty, all event kinds, people lists | `/team`, lead offers, reserve offers, throttling, error texts | lead console (forming/review/locked), results + reviews, notifications, admin panel (new members, quiz builder, import queue, formation, users, teams, alerts) |
| 11–12 | attendance, member events, standalone projects (milestones, updates, releases, contributions), mentorship, confirmations + trust, feed, to-check + penalties + restrictions | `match_mentors`, `scan_community`, abuse flags, project quick-pick, tuning on fixtures | revisit for results, quality report | `/ask`, `/requests`, `/mentorships`, `/confirm`, new templates | home feed, attendance panel, projects, mentorship, profile history + reputation, admin to-check queue |
| 13 | code freeze: property + concurrency + timer tests, bug fixes only | performance + eval report on fixtures | full run against real sources on the dev stack | full run on the dev stack | end-to-end pass, mobile layout |
| 14 | release tag, handoff notes (env, migrations, images) | release notes for `algo/README.md` | release | release | release build |

## Checkpoints
1. **Day 2:** contracts frozen; stub OpenAPI published; `docker compose up` brings up every service; CI green.
2. **Day 4:** frontend logs in via real GitHub against core; bot links an account; quiz can be completed.
3. **Day 7:** full registration (quiz + CV) → scores on profile; admin completes a member review with an override; parser imports real events; members enroll and claim lead.
4. **Day 10:** complete hackathon lifecycle: formation → review → lock → open seats, late round, presence check → results, reviews, XP; placement confirmed from Telegram; admin handles all alert types.
5. **Day 12:** a project ships with confirmed contributions; a mentorship runs from request to reviews; attendance gets confirmed; a planted ring lands in the to-check queue and gets punished.
6. **Day 13:** code freeze; the end-to-end scenario below passes on the dev compose stack with real GitHub OAuth and real parser sources.
7. **Day 14:** release tagged and handed off. Deployment happens outside this repo (§22).

A missed checkpoint is raised the same day in the group's issue with `status:blocked`, and people are moved from groups that are ahead.

## End-to-end acceptance scenario
1. Parser imports a hackathon: primary ML/AI, required Backend + Frontend, team 3–6, registration deadline in 6 days.
2. Fourteen members sign in via GitHub, complete onboarding along different branches, half upload CVs, some link Telegram. An admin reviews two of them and overrides one score with `evidence=interview`.
3. Twelve enroll. Two high-fit members claim lead; a low-fit member is refused with a reason and ticks "I can lead if needed" instead. Lead A invites a friend into the core; lead B accepts a join request. With 14 people involved, the projection shows "2 teams (6, 6), 2 on reserve".
4. Both leads press Ready → formation starts an hour later. The plan fills both cores to 6, covers ML/Backend/Frontend in each, pairs beginners with strong members, and puts 2 people on the reserve.
5. During review, both claimed leads and their core members re-confirm. One placed member declines and stays on the reserve → lead A adds the top reserve member; lead B swaps one member with team A (lead A and both moved members accept). One member of team A stays silent despite reminders.
6. Review ends → the silent member is removed (`lapsed`) and teams lock 48 h before the organizer's deadline. Lead A doesn't act within the 12 h head start → a reserve offer fills the seat.
7. Lead B transfers the lead role to a teammate, who accepts.
8. A member of team B leaves while locked; nobody fills it in 12 h → the top reserve member gets an offer and confirms.
9. Presence check 48 h before the start: everyone confirms except one member of team A, who is removed at the deadline; team A plays one short.
10. Hackathon starts → teams `active`; ends → lead A submits the result with the participants checklist, a retrospective and a claimed 2nd place (`unverified`). Lead B stays silent; after 7 days a team B member submits instead. The parser's winner list then matches team A and verifies the place.
11. Members rate each other; the window closes; reviews are revealed on profiles.
12. XP, activity and area scores update with reasons; profile history shows the hackathon.
13. A second hackathon (team 2–4) gets 1 claimed lead and 6 more enrollments, one of them a volunteer. Formation makes 2 teams of 4 and 3 with the volunteer on the new team. The volunteer declines the lead offer, the next offer goes to the highest-fit teammate, and they accept.
14. A member creates a standalone project, recruits 2 people through the feed, completes milestones and ships a release; the lead confirms contributions; the 7-day review window reveals ratings; XP lands with reasons.
15. A member asks for help with ML via the bot's `/ask`; 5 mentors are notified on Telegram; one offers; it's classified as mentoring; after 3 check-ins the learner closes with "goal reached" and rates the mentor; the mentor gets XP and mentor reputation.
16. Three members mark a meetup as attended and confirm each other; trust becomes `peer`. Two fresh accounts confirm each other at five member-created events in a week; `scan_community` flags the ring; an admin punishes with exclusion and a `confirming` restriction.
