# 8. Core — Areas

## Behavior
- Admin-managed list. `key` is an immutable slug. Seeded by migration in this order:

| key | name | Covers |
|---|---|---|
| `backend` | Backend | APIs, databases, server-side logic, integrations |
| `frontend` | Frontend | web UI, client-side apps |
| `mobile` | Mobile | iOS, Android, cross-platform apps |
| `ml_ai` | ML / AI | machine learning, deep learning, LLM apps, CV/NLP |
| `data` | Data | analytics, BI, data engineering, metrics and unit economics |
| `design` | Design | UI/UX, graphics, branding, pitch visuals |
| `product` | Product | product management, business analysis, scoping, pitching |
| `devops` | DevOps | infrastructure, CI/CD, cloud, deployment |
| `security` | Security | CTF, application and network security |
| `gamedev` | GameDev | game design and development, engines, graphics |
| `hardware` | Hardware | embedded, IoT, robotics, electronics |
| `qa` | QA | testing, test automation, quality |

- Not areas: full-stack (= backend + frontend scores), domain expertise (event-specific, lives in free text and `themes`).
- Archive instead of delete. Archived areas: hidden from new quizzes/imports, existing scores kept read-only.
- Adding an area enqueues `recompute_all_members` (algo may produce an initial score from existing data).

## Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/areas` | member |
| POST | `/api/v1/admin/areas` | admin |
| PATCH | `/api/v1/admin/areas/{id}` | admin (name, description, sort) |
| POST | `/api/v1/admin/areas/{id}/archive` / `/unarchive` | admin |
| GET | `/internal/areas` | parser |
