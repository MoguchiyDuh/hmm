# 3. Cross-cutting conventions

## 3.1 HTTP

- Public API: `/api/v1/...`. Service API: `/api/v1/internal/...` (referred to as `/internal/...` below).
- JSON only, `snake_case` fields.
- IDs: UUIDv7 strings.
- Time: RFC 3339 UTC (`2026-11-12T10:00:00Z`). Events additionally carry an IANA `timezone` for display.
- Pagination: cursor-based. Request `?cursor=&limit=` (default 20, max 100). Response:
  ```json
  { "items": [...], "next_cursor": "opaque-or-null" }
  ```
- Errors: RFC 9457 `application/problem+json`:
  ```json
  { "type": "https://hmm/errors/team-full", "title": "Team is full", "status": 409,
    "detail": "Team has reached max size 5", "code": "team_full", "errors": [] }
  ```
  `code` is a stable machine string; frontend and bot switch on it. Validation errors use 422 with `errors: [{loc, msg}]`.
- Status codes: 200/201/202/204; 400 bad input; 401 unauthenticated; 403 forbidden; 404 not found or not visible; 409 state conflict; 422 validation; 429 rate limited.
- Every mutable resource response includes `allowed_actions: string[]` computed for the current actor (e.g. `["invite","ready","unclaim"]`). Frontend and bot render buttons from this and never re-implement permission rules.
- OpenAPI is the source of truth for the frontend. `core` publishes `openapi.json` in CI; `web` regenerates its client from it.

## 3.2 Authentication modes

| Caller | Mechanism |
|---|---|
| Browser | `hmm_session` cookie: `HttpOnly; Secure; SameSite=Lax; Path=/`. Opaque random token, SHA-256 stored server-side. Unsafe methods require `Origin` == `PUBLIC_URL`. |
| Bot acting as a user | `Authorization: Bearer <BOT_SERVICE_TOKEN>` + `X-Telegram-User-Id: <id>`. Core resolves linked user; unlinked → 401 `telegram_not_linked`. Same permission checks as the user. |
| Bot service calls | `Authorization: Bearer <BOT_SERVICE_TOKEN>` on `/internal/telegram/*`, `/internal/notifications/*`. |
| Parser | `Authorization: Bearer <PARSER_SERVICE_TOKEN>` on `/internal/events/*`, `/internal/areas`, `/internal/parser/*` only. |

Tokens are compared in constant time. Service tokens are scoped: bot token rejected on parser routes and vice versa.

## 3.3 Authorization model

- Global flags on user: `is_admin`, `banned`.
- **No global lead role.** Leadership is per project (`projects.lead_user_id`); a hackathon team is a project. "Lead of P" grants lead actions on P only. Mentors and learners act only on their own mentorships.
- Restrictions (§10) are checked per action; violations → 403 `restricted`.
- Onboarding gate: until `onboarding_status = completed`, a user can only call `/me`, `/auth/*`, `/onboarding/*`, `/me/profile`, `/me/cv`, `/notifications/*`.
- Banned users: all sessions revoked, every request → 403 `banned`.

## 3.4 Consistency and concurrency

- All participation mutations (enroll, withdraw, claim/unclaim, invites, join requests, placement confirm/decline, adds/removals, swaps, merges, leave, lead offers, transfer, step-down, formation apply, lock, admin roster ops) run in one transaction that first takes `SELECT ... FOR UPDATE` on the `events` row. This serializes participation per hackathon and makes the invariants in §4 enforceable.
- Standalone project mutations lock the `projects` row; help request and mentorship mutations lock the help request row, plus the mentor's `users` row when capacity is checked.
- Jobs are enqueued inside the same transaction as the state change (procrastinate uses the same connection), so no job is emitted for a rolled-back change.
- DB-level constraints back every invariant (partial unique indexes, check constraints; see §5).

## 3.5 Audit

Every admin action, every forced/system state transition, and every role-like change writes `audit_log` with actor, action, entity, before/after JSON, optional reason. Admin endpoints accept optional `reason` everywhere.

## 3.6 Configuration

- Static config: env vars (§22).
- Runtime-tunable config: `settings` table, editable by admins (§16). Defaults in §24.

## 3.7 Language

Everything is English: code, API, enums, logs, bot, notification templates, algo reasons. The web UI ships English and Russian; each user picks a language in settings (`users.locale`, default `en`). Server-generated text (notification payload strings, algo reasons, parser data) stays English.
