# 6. Core — Identity and access

## Responsibilities
GitHub OAuth sign-in, sessions, admin bootstrap, bans, Telegram linking, service token auth.

## Behavior
- OAuth app scope: `read:user` only. `state` parameter stored in a short-lived signed cookie; PKCE enabled.
- First login creates `users` row (`onboarding_status = not_started`), enqueues `github_sync(user)`.
- Repeat login matches by `github_id` (login may change; update `github_login`).
- `ADMIN_GITHUB_LOGINS` env (bootstrap): a listed login gets `is_admin = true` when its account is created, or on any login while no admin exists. After that, admin status is managed in-app only, so a revoke sticks. Admins can grant/revoke admin to others; the last admin cannot be demoted.
- Session TTL `session_ttl_days` (30), sliding: `last_used_at` updated at most once per 5 min.
- Ban: sets `banned_at`, deletes sessions, unlinks nothing; withdraws live enrollments, pending invites, join requests, help requests, help offers and mentor offers; cancels active mentorships (no outcome; the other side keeps their activity); their confirmations become `ignored`; leaves standalone projects (lead succession per §27.6); removes from teams in `forming|review|locked` (a banned lead is treated as leaving: succession per §12.8, alerts `team_below_min`/`forced_lead` as applicable); `active` teams keep them (history) but admin alert `banned_member_in_active_team`.
- Telegram link: web requests a code (8 chars, base32, TTL 10 min, one active per user). Deep link `https://t.me/<bot>?start=<code>`. Bot posts code + Telegram identity to core. Re-linking replaces previous link. One Telegram account ↔ one user.

## Endpoints

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/api/v1/auth/github/login?next=` | anyone | 302 to GitHub |
| GET | `/api/v1/auth/github/callback` | GitHub | create/resume user, set cookie, 302 to `next` or `/onboarding` |
| POST | `/api/v1/auth/logout` | user | delete current session |
| POST | `/api/v1/auth/logout-all` | user | delete all own sessions |
| GET | `/api/v1/me` | user | user, `is_admin`, `onboarding_status`, `dnd_until`, telegram link status, unread count, `allowed_actions` |
| POST | `/api/v1/me/telegram/link-code` | user | `{code, deep_link, expires_at}` |
| DELETE | `/api/v1/me/telegram` | user | unlink |
| POST | `/internal/telegram/link` | bot | `{code, telegram_user_id, chat_id, username}` → `{user_id, display_name}`; 404 `code_invalid`, 410 `code_expired` |
| POST | `/internal/telegram/unlink` | bot | `{telegram_user_id}` (user blocked bot / `/unlink`) |

## Done when
- Login/logout/repeat login work; renamed GitHub login still maps to the same user.
- Session revocation is immediate.
- Unsafe request from foreign Origin is rejected.
- Bot token cannot hit parser routes and vice versa.
