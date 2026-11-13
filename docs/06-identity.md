# 6. Identity and access

Covers GitHub sign-in, sessions, admin bootstrap, bans, Telegram linking and service-token auth. Authentication modes are in §3.2, authorization in §3.3.

## 6.1 Sign-in

- OAuth app scope: `read:user` only. PKCE (`S256`) is on. `state`, the PKCE verifier and `next` live in a signed cookie that expires after 10 minutes.
- `next` is kept only if it is a same-site path (starts with `/`, not `//`); otherwise the callback goes to `/onboarding`.
- First login creates the `users` row (`onboarding_status = not_started`) and enqueues `github_sync(user)`.
- A repeat login matches by `github_id` and updates `github_login` (logins can change). It enqueues `github_sync` if the last snapshot is older than 24 h.
- A failed callback redirects to `/login?error=<code>`:

| `code` | When |
|---|---|
| `bad_request` | User cancelled on GitHub; `state` missing, forged, expired or replayed |
| `internal_error` | GitHub token exchange or profile fetch failed |
| `banned` | The account is banned |

## 6.2 Sessions

- `session_ttl_days` (30), sliding: `last_used_at`, `expires_at` and the cookie are refreshed at most once every 5 minutes.
- `logout` deletes the current session, `logout-all` every session of the user. Revocation is immediate.

## 6.3 Admins

- `ADMIN_GITHUB_LOGINS` (env, comma-separated, case-insensitive) is bootstrap only. A listed login gets `is_admin = true` when its account is created, or on any login while no admin exists. After that, admin status is managed in-app only, so a revoke sticks.
- Admins grant and revoke admin for others (`PATCH /api/v1/admin/users/{id}`, §16). Revoking the last admin → 409 `last_admin`.
- Granting admin to a banned user → 409 `invalid_state`.

## 6.4 Bans

`POST /api/v1/admin/users/{id}/ban {reason}`. Banning an admin → 409 `invalid_state`; revoke admin first. A ban:

- sets `banned_at` and `ban_reason` and deletes all sessions (§3.3); the Telegram link stays;
- withdraws live enrollments, pending invites, join requests, help requests, help offers and mentor offers;
- cancels active mentorships with no outcome; the other side keeps their activity;
- makes the user's confirmations `ignored`;
- removes the user from standalone projects (a banned lead leaves with succession per §27.6);
- removes the user from teams in `forming`, `review` or `locked` (a banned lead leaves with succession per §12.8, and alerts `team_below_min` or `forced_lead` fire as applicable);
- keeps the user on `active` teams for history and raises admin alert `banned_member_in_active_team`.

`POST /api/v1/admin/users/{id}/unban` clears `banned_at` and `ban_reason`.

## 6.5 Telegram linking

- The web requests a code: 8 characters, base32, valid 10 minutes, one active per user (a new code invalidates the previous one). Deep link: `https://t.me/<bot>?start=<code>`.
- The bot posts the code and the Telegram identity to core (`/internal/telegram/link`).
- One Telegram account ↔ one user:
  - linking a Telegram account that is linked to another user → 409 `telegram_already_linked` (that user unlinks first);
  - a user who links a new Telegram account replaces their own previous link.
- A link marked broken (`broken_at`, §15.2) still authenticates bot requests. Any successful link or bot request as that user clears `broken_at`.
- Unlink from the web (`DELETE /api/v1/me/telegram`) or from the bot (`/unlink`, or the bot learns the user blocked it). Unlinking an unknown Telegram account returns 200 with no effect.

## 6.6 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/auth/github/login?next=` | anyone | 302 to GitHub |
| GET | `/api/v1/auth/github/callback` | anyone (GitHub redirect) | Create or resume the user, set the cookie, 302 to `next` or `/onboarding`; failures per §6.1 |
| POST | `/api/v1/auth/logout` | user | 204 |
| POST | `/api/v1/auth/logout-all` | user | 204 |
| GET | `/api/v1/me` | user | User, `is_admin`, `onboarding_status`, `dnd_until`, Telegram link status, unread count, `allowed_actions` |
| POST | `/api/v1/me/telegram/link-code` | user | → `{code, deep_link, expires_at}` |
| DELETE | `/api/v1/me/telegram` | user | 204 |
| POST | `/internal/telegram/link` | bot | `{code, telegram_user_id, chat_id, username}` → `{user_id, display_name}`; 404 `code_invalid`, 410 `code_expired`, 409 `telegram_already_linked` |
| POST | `/internal/telegram/unlink` | bot | `{telegram_user_id}` |

`allowed_actions` on `/me`: `link_telegram` always; `unlink_telegram` when linked; `refresh_github` and `request_help` for members; `create_project` and `create_event` for members without an active `creating_projects` / `creating_events` restriction.

## 6.7 Done when

- Login, logout and repeat login work; a renamed GitHub login still maps to the same user.
- Session revocation is immediate.
- An unsafe request from a foreign `Origin` is rejected.
- The bot token cannot call parser routes and vice versa.
