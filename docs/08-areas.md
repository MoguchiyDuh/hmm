# 8. Areas

## 8.1 Seed list

Admin-managed list. `key` is an immutable slug. A migration seeds, in this order:

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

Not areas: full-stack (= backend + frontend scores), and domain expertise (event-specific; lives in free text and `themes`).

## 8.2 Rules

- Archive instead of delete. Archived areas are hidden from new quizzes and imports; existing scores are kept read-only.
- Adding an area enqueues `recompute_all_members` (algo may produce an initial score from existing data).

## 8.3 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/areas` | member | Area list |
| POST | `/api/v1/admin/areas` | admin | `{key, name, description, sort}` |
| PATCH | `/api/v1/admin/areas/{id}` | admin | `{name, description, sort}` |
| POST | `/api/v1/admin/areas/{id}/archive` | admin | – |
| POST | `/api/v1/admin/areas/{id}/unarchive` | admin | – |
| GET | `/internal/areas` | parser | Area list |
