# 20. Telegram bot

## 20.1 Scope

Notification delivery with inline actions (incl. Help, Confirm, Check in), account linking, a few read/enroll commands. Everything else stays on the web.

## 20.2 Stack and runtime

aiogram 3, long polling. Lives in the `hmm` package and runs as its own process from the same image (entry point `hmm-bot`), calling domain modules in-process with its own database sessions. Stateless apart from an in-memory FSM.

Config: `BOT_TOKEN`, `DATABASE_URL`, `PUBLIC_URL` (for web links).

## 20.3 Account linking

- `/start <code>` → `identity.service.link_telegram` → "Linked to <display_name>". Errors:
  - `code_invalid` / `code_expired` → tell the user to generate a new code on the web;
  - `telegram_already_linked` → this Telegram account belongs to another user, who must unlink it first.
- `/start` without a code, or any command from an unlinked user → a message linking to `PUBLIC_URL/settings#telegram`.
- `/unlink` → `identity.service.unlink_telegram_account`.
- Private chats only; ignore groups.

## 20.4 Commands

| Command | Same as | Output |
|---|---|---|
| `/events` | `GET /api/v1/events?phase=registration_open&limit=10` | list with inline buttons: Details, Going / Enroll |
| `/event <id>` | `GET /api/v1/events/{id}` | card with participation phase, live projection and formation date + buttons from `allowed_actions` (Enroll / Withdraw / Party) + web link |
| `/enroll <id>` | `POST /api/v1/events/{id}/enroll` | confirmation or error text from problem `code` |
| `/team` | `GET /api/v1/me/projects` | current teams with pending actions (accept or decline a placement before `accept_by`) as buttons |
| `/invites` | `GET /api/v1/me/invites?status=pending` | list with Accept/Decline |
| `/ask` `<area> <topic>` | `POST /api/v1/help-requests` | creates a help request (goal asked as a follow-up message) |
| `/requests` | `GET /api/v1/help-requests?status=open&area=<my mentor areas>` | open help requests I could take, with Help buttons |
| `/mentorships` | `GET /api/v1/me/mentorships?status=active` | active mentorships with Check in / Close buttons |
| `/confirm` | `GET /api/v1/me/confirmations/pending` | pending confirmation digests as checkbox keyboards |
| `/me` | `GET /api/v1/me`, `/api/v1/users/{id}/metrics`, `/api/v1/users/{id}/scores` | short profile: scores, XP, activity, current teams |
| `/dnd [off\|<days>]` | `PUT /api/v1/me/availability` | DND toggle |
| `/settings` | – | link to web notification settings |
| `/help` | – | command list |

Each command resolves the linked user with `identity.service.telegram_actor` (§6.5) and runs the domain functions behind the listed endpoint, with the same permission checks (§3.2). Errors are rendered from the `ProblemError` code.

## 20.5 Notification delivery

- One delivery loop: `lease_deliveries`, render, send, `acknowledge` (§15.2).
- Rendering: per-type English templates, HTML parse mode with escaping. Unknown type → generic text + web link (forward compatible).
- Inline buttons from payload `actions`: one-tap `kind: "api"` calls resolved via shared `ACTION_ROUTES`, or `kind: "link"` web links for forms and navigation. `callback_data`: `a:<key>:<entity_id>` (≤ 64 bytes). On press: `notifications.actions.run_action(key, entity_id)` runs the route's domain function with its fixed body as the user; edit the message with the outcome, and answer the callback.
- Idempotency: keep an in-memory LRU of delivered `notification_id`s. An expired lease is redelivered; duplicates within the LRU are acked as `sent` without resending.
- Throttle: ≤ 25 msgs/s global, ≤ 1 msg/s per chat; handle `RetryAfter`.
- `Forbidden: bot was blocked` / `chat not found` → ack `permanent_failure`.

## 20.6 Deliverables

- `hmm-bot` entry point and a `bot` service in the compose files.
- A template for every notification type in §15.1, with screenshots or text samples.
- Template tests over `tests/fixtures/notifications/*.json`.
