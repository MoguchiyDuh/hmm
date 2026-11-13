# 3. Cross-cutting conventions

## 3.1 HTTP

- Public API: `/api/v1/...`. Service API: `/api/v1/internal/...`, written `/internal/...` in these docs.
- JSON only, `snake_case` fields.
- IDs: UUIDv7 strings.
- Time: RFC 3339 UTC (`2026-11-12T10:00:00Z`). Clients display times in the viewer's timezone (`users.timezone` if set, else the browser's); the bot uses `users.timezone`, else UTC. Events also carry an IANA `timezone`.
- Pagination: cursor-based. Request `?cursor=&limit=` (default 20, max 100). Response:
  ```json
  { "items": [...], "next_cursor": "opaque-or-null" }
  ```
- Errors: RFC 9457 `application/problem+json`:
  ```json
  { "type": "https://<PUBLIC_URL host>/errors/team_full", "title": "Team full", "status": 409,
    "detail": "Team has reached max size 5", "code": "team_full", "errors": [] }
  ```
  - `type` is always `{PUBLIC_URL}/errors/{code}`.
  - `code` is a stable machine string; frontend and bot switch on it. The full list is `hmm_contracts.common.ErrorCode`; a new code is a contract change.
  - Validation errors use 422 with `errors: [{loc, msg}]`.
- Status codes:

| Status | Meaning |
|---|---|
| 200 / 201 / 202 / 204 | OK / created / accepted (job enqueued) / no content |
| 400 | Bad input |
| 401 | Unauthenticated |
| 403 | Forbidden |
| 404 | Not found or not visible |
| 405 | Method not allowed |
| 409 | State conflict |
| 410 | Expired link code |
| 413 / 415 | Upload too large / unsupported type |
| 422 | Validation |
| 429 | Rate limited |
| 500 | `internal_error` |
| 501 | `not_implemented` (endpoint not built yet) |
| 503 | Dependent data not ready yet (`lead_fit_pending`) |

- Text limits unless a section says otherwise: names ≤ 100, titles ≤ 200, notes and reasons ≤ 1000 (trimmed, non-empty), markdown ≤ 5000, event descriptions ≤ 20000, ≤ 10 links.
- Every mutable resource response includes `allowed_actions: string[]` computed for the current actor (e.g. `["invite","ready","unclaim"]`). Frontend and bot render buttons from it and never re-implement permission rules.
- OpenAPI is the source of truth for the frontend. Core commits `core/openapi.json` and CI checks it is current; `web` regenerates its client from it.

## 3.2 Authentication

| Caller | Mechanism |
|---|---|
| Browser | `hmm_session` cookie: `HttpOnly; Secure; SameSite=Lax; Path=/`. Opaque random token; only its SHA-256 is stored. Unsafe methods (anything but GET, HEAD, OPTIONS) require `Origin` = `PUBLIC_URL`, otherwise 403 `origin_mismatch`. |
| Bot acting as a user | `Authorization: Bearer <BOT_SERVICE_TOKEN>` + `X-Telegram-User-Id: <id>`. Core resolves the linked user; unlinked → 401 `telegram_not_linked`. Same permission checks as the user. |
| Bot service calls | `Authorization: Bearer <BOT_SERVICE_TOKEN>` on `/internal/telegram/*` and `/internal/notifications/*` only. |
| Parser | `Authorization: Bearer <PARSER_SERVICE_TOKEN>` on `/internal/events/*`, `/internal/areas` and `/internal/parser/*` only. |

- A request carrying both a session cookie and bot credentials → 400 `bad_request`.
- Tokens are compared in constant time. Service tokens are scoped: the bot token is rejected on parser routes and vice versa.
- Admin endpoints require a web session; the bot cannot call them.

## 3.3 Authorization

**Who** values used in every endpoint table:

| Who | Meaning |
|---|---|
| anyone | No authentication. |
| user | Signed in (web session or linked Telegram), onboarding not required. |
| member | User whose `onboarding_status` is `completed` or `reopened`. Otherwise 403 `onboarding_required`. |
| admin | Web session of a user with `is_admin`. Onboarding is not required. |
| bot / parser | Service token (§3.2). |
| Resource roles | A member acting on a resource they hold a role in: **lead** (of that project or team), **team member** / **project member** (on its roster), **invitee**, **requester**, **learner**, **mentor**, **creator**, **offer author**, **confirmer**. Otherwise 403 `forbidden` or 404 if not visible. |

Rules:
- Global flags on a user: `is_admin` and banned (`banned_at`).
- **No global lead role.** Leadership is per project (`projects.lead_user_id`); a hackathon team is a project. "Lead of P" grants lead actions on P only. Mentors and learners act only on their own mentorships.
- **Onboarding gate:** a user who isn't a member can only call `user` endpoints: `/auth/*`, `/me`, `/me/profile`, `/me/cv`, `/me/telegram*`, `/me/notification-settings`, `/onboarding/*` and `/notifications/*`.
- **Restrictions** (§10.4) are checked per action. A restricted action → 403 `restricted`, except `confirming`, whose confirmations are stored and ignored.
- **Banned users:** all sessions are deleted, so web requests get 401 `unauthenticated` and sign-in redirects to `/login?error=banned`. Bot requests for a banned user get 403 `banned`.

## 3.4 Consistency and concurrency

- All participation mutations on a hackathon run in one transaction that first takes `SELECT ... FOR UPDATE` on the `events` row: enroll, withdraw, claim/unclaim, invites, join requests, placement confirm/decline, adds and removals, swaps, merges, leave, lead offers, transfer, step-down, formation apply, lock and admin roster operations. This serializes participation per hackathon and makes the invariants in §4.4 enforceable.
- That lock is never held across an algo or network call: `run_formation` locks only to switch phase and to apply the plan (§12.5).
- Standalone project mutations lock the `projects` row. Help request and mentorship mutations lock the help request row, plus the mentor's `users` row when capacity is checked.
- Jobs are enqueued in the same transaction as the state change (procrastinate uses the same connection), so a rolled-back change emits no job.
- DB constraints back every invariant (partial unique indexes, check constraints; see §5).
- A mutation whose target state already holds returns 200 with the current resource and no side effects. Any other wrong-state transition → 409.

## 3.5 Audit

Every admin action, every forced or system state transition, and every role-like change writes `audit_log` with actor, action, entity, before/after JSON and an optional reason. Every admin endpoint accepts an optional `reason`.

## 3.6 Configuration

- Static config: environment variables (§22.2).
- Runtime-tunable config: `settings` table, editable by admins (§16). Keys and defaults: §24.

## 3.7 Language

- Everything is English: code, API, enums, logs, bot, notification templates, algo reasons.
- The web UI ships English and Russian; each user picks one in settings (`users.locale`, default `en`).
- Server-generated text (notification payload strings, algo reasons, parser data) stays English.
