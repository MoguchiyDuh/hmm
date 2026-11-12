# 8. Core — Areas

## Behavior
- Admin-managed list. `key` immutable slug (`backend`, `frontend`, `ml_ai`, `design`, `mobile`, `devops`, `product`, `data`, `security`, `gamedev`). Seeded by migration.
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
