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
| 8–10 | `placement_bounds`, fallback planner, `run_formation`, review actions, lock, timers, swaps, merges, vacancies + autofill, late round, lead offers, results, generic reviews, deliveries, admin alerts | `form_teams`, `rank_reserve`, points over all facts (§18.3 #13), LLM reasons, caching | difficulty, all event kinds, people lists | `/team`, lead offers, reserve offers, throttling, error texts | lead console (forming/review/locked), results + reviews, notifications, admin panel (new members, quiz builder, import queue, formation, users, teams, alerts) |
| 11–12 | attendance, member events, standalone projects (milestones, updates, releases, contributions), mentorship, confirmations + trust, feed, to-check + penalties + restrictions | `match_mentors`, `scan_community`, abuse flags, project quick-pick, tuning on fixtures | revisit for results, quality report | `/help`, `/requests`, `/mentorships`, `/confirm`, new templates | home feed, attendance panel, projects, mentorship, profile history + reputation, admin to-check queue |
| 13 | code freeze: property + concurrency + timer tests, bug fixes only | performance + eval report on fixtures | staging run against real sources | staging run | end-to-end pass, mobile layout |
| 14 | backups, production deploy, smoke test | – | production run on VPS | production run | production smoke test |

## Checkpoints
1. **Day 2:** contracts frozen; stub OpenAPI published; `docker compose up` brings up every service; CI green.
2. **Day 4:** frontend logs in via real GitHub against core; bot links an account; quiz can be completed.
3. **Day 7:** full registration (quiz + CV) → scores on profile; admin completes a member review with an override; parser imports real events; members enroll and claim lead.
4. **Day 10:** complete hackathon lifecycle: formation → review → lock → vacancies, late round → results, reviews, XP; placement confirmed from Telegram; admin handles all alert types.
5. **Day 12:** a project ships with confirmed contributions; a mentorship runs from request to reviews; attendance gets confirmed; a planted ring lands in the to-check queue and gets punished.
6. **Day 13:** code freeze; the end-to-end scenario below passes on staging.
7. **Day 14:** production deploy on the VPS.

A missed checkpoint is raised the same day in the group's issue with `status:blocked`, and people are moved from groups that are ahead.

## End-to-end acceptance scenario
1. Parser imports a hackathon: primary ML/AI, required Backend + Frontend, team 3–6, registration deadline in 6 days.
2. Fourteen members sign in via GitHub, complete onboarding along different branches, half upload CVs, some link Telegram. An admin reviews two of them and overrides one score with `evidence=interview`.
3. Twelve enroll. Two high-fit members claim lead; a low-fit member is refused with a reason and ticks "I can lead if needed" instead. Lead A invites a friend into the core; lead B accepts a join request. With 14 people involved, the projection shows "2 teams (6, 6), 2 on reserve".
4. Both leads press Ready → formation starts an hour later. The plan fills both cores to 6, covers ML/Backend/Frontend in each, pairs beginners with strong members, and puts 2 people on the reserve.
5. During review, one placed member declines → lead A adds the top reserve member; lead B swaps one member with team A (lead A and both moved members accept). One member stays silent despite reminders.
6. Review ends → the silent member is removed (`lapsed`), a reserve offer fills the seat, and teams lock.
7. Lead B transfers the lead role to a teammate, who accepts.
8. A member of team B leaves while locked; nobody fills it in 24 h → the top reserve member gets an offer and confirms.
9. Hackathon starts → teams `active`; ends → leads get `result_due` and submit results with retrospectives.
10. Members rate each other; the window closes; reviews are revealed on profiles.
11. XP, activity and area scores update with reasons; profile history shows the hackathon.
12. A second hackathon (team 2–4) gets 1 claimed lead and 6 more enrollments, one of them a volunteer. Formation makes 2 teams of 4 and 3 with the volunteer on the new team. The volunteer declines the lead offer, the next offer goes to the highest-fit teammate, and they accept.
13. A member creates a standalone project, recruits 2 people through the feed, completes milestones and ships a release; the lead confirms contributions; the 7-day review window reveals ratings; XP lands with reasons.
14. A member asks for help with ML; 5 mentors are notified on Telegram; one offers; it's classified as mentoring; after 3 check-ins the learner closes with "goal reached" and rates the mentor; the mentor gets XP and mentor reputation.
15. Three members mark a meetup as attended and confirm each other; trust becomes `peer`. Two fresh accounts confirm each other at five member-created events in a week; `scan_community` flags the ring; an admin punishes with exclusion and a `confirming` restriction.
