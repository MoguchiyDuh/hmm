# 23. Delivery plan

Full scope, one release, five phases. Work runs against fakes (`hmm.algo.fake.FakeAlgorithm`, the fixtures in `tests/fixtures/`) until the real piece lands. A phase is done when its checkpoint (§23.3) passes on the dev stack.

## 23.1 Phases

1. **Foundation.**
   - Server: schema baseline, settings, audit, job worker, GitHub sign-in, sessions, bans, Telegram linking.
   - Algorithm: `Algorithm` protocol and I/O models, `pack()` with the §12.3 example and property tests, `FakeAlgorithm` with conformance tests.
   - Fixtures: members, events, formation inputs, mentorship cases, community graphs with planted rings, imports, notifications, visibility cases.
   - Parser: source survey, fetch + classify dry-run. Bot: aiogram skeleton. Web: app shell, routing, generated client, i18n (en/ru).
   - Local gate green; production stack and release script in place (§22).
2. **Registration and catalog.**
   - Server: job registry and algorithm wiring, areas, quiz builder and onboarding, profiles, GitHub sync, CV, scores, member review, solo log, catalog with import and review queue, queue and parties, enrollments, invites.
   - Algorithm: `score_onboarding` with an evaluation harness, `compute_member`, `recommend`.
   - Parser: extraction and normalization, area mapping, import, revisit loop, heartbeat, dedup.
   - Bot: linking, delivery loop, templates, inline actions, `/events`, `/enroll`, `/invites`.
   - Web: auth flow, onboarding wizard (incl. CV step), settings, profile, directory, catalog, event page, participation panel.
3. **Hackathon lifecycle.**
   - Server: `run_formation` with the `pack()` fallback, placement accept and decline, refills, lead rule, `event_tick`, results (participants, member fallback, auto-close, place verification), reviews, deliveries, admin alerts and endpoints.
   - Algorithm: `form_teams`, points over all facts (§18.3), LLM reasons, caching.
   - Parser: difficulty, all event kinds, people lists. Bot: `/team`, placement accepts, throttling, error texts.
   - Web: formation panel, results and reviews, notifications, admin panel.
4. **Community.**
   - Server: attendance, member events, standalone projects, mentorship, confirmations and trust, feed, to-check queue, penalties and restrictions.
   - Algorithm: `match_mentors`, `scan_community`, abuse flags, project quick-pick.
   - Parser: revisit ended events for results. Bot: `/ask`, `/requests`, `/mentorships`, `/confirm`, new templates.
   - Web: home feed, attendance panel, projects, mentorship, profile history and reputation, admin to-check queue.
5. **Release.** Property, concurrency and timer tests; algorithm performance and evaluation report; parser quality report and a full run against real sources; bot and web end-to-end pass on the dev stack. Ends with the acceptance scenario (§23.4) passing and a `v*` release tag deployed with `deploy/release.sh` (§22.3).

## 23.3 Checkpoints

1. **Foundation:** `docker compose up` brings up every service healthy; the local gate is green; the bot links an account.
2. **Registration and catalog:** frontend signs in via real GitHub; full registration (quiz + CV) → scores on profile; admin completes a member review with an override; parser imports real events; members enroll and create a party.
3. **Hackathon lifecycle:** queue and parties → formation → accepts → refills until `reg_deadline` → results, reviews, XP; placement accepted from Telegram; admin handles every alert type.
4. **Community:** a project ships with confirmed contributions; a mentorship runs from request to reviews; attendance gets confirmed; a planted ring lands in the to-check queue and gets punished.
5. **Release:** the scenario below passes on the dev stack with real GitHub OAuth and real parser sources; the release is tagged and deployed (§22).

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
