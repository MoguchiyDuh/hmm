# hmm — design documentation

A community platform for hackathon teams, projects, events and mentorship.

- **Registration:** members sign in with GitHub, complete a branching onboarding quiz, optionally attach a CV, and get per-area skill scores. Admins may review those scores later without blocking anyone.
- **Hackathons:** a parser discovers events. Members join a matchmaking queue alone or with a party of friends, and one formation run packs the queue into complete teams.
- **After formation:** every placed member accepts their seat within 24 h or loses it, and open seats keep filling from the queue until the registration deadline. Every team has a lead by a fixed rule, and the role passes at once when it empties.
- **After the event:** results, peer ratings, XP, activity and score progression. The team can continue as a project.
- **Between hackathons:** standalone projects (team, plan, releases), event attendance ("we went to X") and mentorship ("I want to learn X").
- **Points:** XP needs a second party: co-participants, leads, organizers or learners confirm facts. Algo computes everything; suspicious items go to an admin "to check" queue to punish or pardon.

Section numbers (`§N`, `§N.M`) are stable across files.

## How to read a section

- Every section has numbered subsections, an **Endpoints** table and, where testable, a **Done when** list.
- Endpoint tables have the columns `Method | Path | Who | Body → response`. The **Who** values are defined in §3.3.
- A setting is written as its key with the default in parentheses, e.g. `accept_hours` (24). All settings are in §24.
- Notification types are in §15, admin alerts in §15.3, error codes in `hmm.common.ErrorCode`.

## Index

| § | File |
|---|---|
| 2 | [Architecture, repo, stack](02-architecture.md) |
| 3 | [Cross-cutting conventions](03-conventions.md) |
| 4 | [Domain glossary and rules](04-domain.md) |
| 5 | [Data model](05-data-model.md) |
| 6 | [Identity and access](06-identity.md) |
| 7 | [Profiles, GitHub, CV, availability](07-profiles.md) |
| 8 | [Areas](08-areas.md) |
| 9 | [Registration and onboarding](09-onboarding.md) |
| 10 | [Scores, XP, activity, moderation](10-scores.md) |
| 11 | [Event catalog and import](11-catalog.md) |
| 12 | [Hackathon participation and team formation](12-formation.md) |
| 13 | [Results, reviews, retrospectives](13-results-reviews.md) |
| 14 | [Algorithm integration](14-algo-integration.md) |
| 15 | [Notifications and admin alerts](15-notifications.md) |
| 16 | [Admin](16-admin.md) |
| 17 | [Background jobs](17-jobs.md) |
| 18 | [Algorithm package](18-algo.md) |
| 19 | [Parser service](19-parser.md) |
| 20 | [Telegram bot](20-bot.md) |
| 21 | [Frontend](21-frontend.md) |
| 22 | [Deployment and configuration](22-deployment.md) |
| 23 | [Delivery plan](23-delivery-plan.md) |
| 24 | [Settings defaults](24-defaults.md) |
| 25 | [Facts, confirmations, trust, feed](25-trust-feed.md) |
| 26 | [Event attendance](26-attendance.md) |
| 27 | [Projects](27-projects.md) |
| 28 | [Mentorship](28-mentorship.md) |
