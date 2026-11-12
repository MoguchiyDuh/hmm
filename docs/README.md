# hmm — design documentation

A community platform for hackathon teams, projects, events and mentorship.
- **Registration:** members sign in with GitHub, complete a branching onboarding quiz, optionally attach a CV, and get per-area skill scores. Admins may review those scores later without blocking anyone.
- **Hackathons:** a parser discovers events. Members enroll, eligible members claim the lead role and gather a core, others can volunteer to lead if needed, and one formation round packs everyone into complete teams.
- **Review:** leads adjust the result and everyone on a team must confirm their spot, and again shortly before the start; silence means removal. Every team ends up with a lead, picked by a fixed lead order. A reserve fills open seats.
- **After the event:** results, peer ratings, XP, activity and score progression. The team can continue as a project.
- **Between hackathons:** standalone projects (team, plan, releases), event attendance ("we went to X"), and mentorship ("I want to learn X").
- **Points:** XP needs a second party: co-participants, leads, organizers or learners confirm facts. Algo computes everything; suspicious items go to an admin "to check" queue to punish or pardon.

These documents are the contract between five parallel groups. Anything not specified here is the owning group's choice. Section numbers (`§N`) are stable across files.

## Groups and ownership

| Group | Owns | Talks to | Read first |
|---|---|---|---|
| **core** | `core/`: FastAPI API, worker, PostgreSQL schema and migrations, object storage, `contracts/` stewardship | everyone | §2–§17, §22, §25–§28 |
| **algo** | `algo/`: Python package for onboarding scoring, score progression, XP and activity over all facts, lead fit, quick-pick, team formation, reserve ranking, mentor matching, abuse flags and community scan | core (in-process, via `contracts/algo.py`) | §4, §12, §14, §18, §25, §28 |
| **parser** | `parser/`: standalone service for event discovery (all kinds), extraction, normalization, dedup, people lists and import | core (`/internal/*` HTTP) | §11, §19, §26 |
| **bot** | `bot/`: Telegram bot for notification delivery, quick actions and account linking | core (`/api/v1/*` + `/internal/*` HTTP) | §6, §15, §20, §28 |
| **frontend** | `web/`: SPA including the admin panel | core (`/api/v1/*` HTTP, generated client) | §3, §9, §12, §21, §25–§28 |

Everyone reads §2–§4 and §23.

Rules:
- Nobody except core reads or writes core's database.
- `contracts/` changes require approval from every group that imports the changed module.
- Each group ships fixtures and a way to run its part against fakes (§23).

## Index

| § | File | Owner |
|---|---|---|
| 2 | [Architecture, repo, stack](02-architecture.md) | all |
| 3 | [Cross-cutting conventions](03-conventions.md) | all |
| 4 | [Domain glossary and rules](04-domain.md) | all |
| 5 | [Data model](05-data-model.md) | core |
| 6 | [Identity and access](06-identity.md) | core |
| 7 | [Profiles, GitHub, CV, availability](07-profiles.md) | core |
| 8 | [Areas](08-areas.md) | core |
| 9 | [Registration and onboarding](09-onboarding.md) | core |
| 10 | [Scores, XP, activity, moderation](10-scores.md) | core |
| 11 | [Event catalog and import](11-catalog.md) | core |
| 12 | [Hackathon participation and team formation](12-formation.md) | core |
| 13 | [Results, reviews, retrospectives](13-results-reviews.md) | core |
| 14 | [Algorithm integration](14-algo-integration.md) | core |
| 15 | [Notifications and admin alerts](15-notifications.md) | core |
| 16 | [Admin](16-admin.md) | core |
| 17 | [Background jobs](17-jobs.md) | core |
| 18 | [Algorithm package](18-algo.md) | algo |
| 19 | [Parser service](19-parser.md) | parser |
| 20 | [Telegram bot](20-bot.md) | bot |
| 21 | [Frontend](21-frontend.md) | frontend |
| 22 | [Runtime contract and configuration](22-deployment.md) | core |
| 23 | [Delivery plan](23-delivery-plan.md) | all |
| 24 | [Settings defaults and open points](24-defaults.md) | all |
| 25 | [Facts, confirmations, trust, feed](25-trust-feed.md) | core |
| 26 | [Event attendance](26-attendance.md) | core |
| 27 | [Projects](27-projects.md) | core |
| 28 | [Mentorship](28-mentorship.md) | core |
