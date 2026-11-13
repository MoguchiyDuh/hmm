# 20. Telegram bot

## 20.1 Scope

Notification delivery with inline actions (incl. Help, Confirm, Check in), account linking, a few read/enroll commands. Everything else stays on the web.

## 20.2 Stack and runtime

Python 3.13, aiogram 3, long polling, httpx client to core. Stateless apart from an in-memory FSM. If persistence is ever needed, use aiogram's storage on a local volume; never core's DB.

Config: `BOT_TOKEN`, `CORE_URL`, `BOT_SERVICE_TOKEN`, `PUBLIC_URL` (for web links).

## 20.3 Account linking

- `/start <code>` → `POST /internal/telegram/link` → "Linked to <display_name>". Errors:
  - `code_invalid` / `code_expired` → tell the user to generate a new code on the web;
  - `telegram_already_linked` → this Telegram account belongs to another user, who must unlink it first.
- `/start` without a code, or any command from an unlinked user (`telegram_not_linked`) → a message linking to `PUBLIC_URL/settings#telegram`.
- `/unlink` → `POST /internal/telegram/unlink`.
- Private chats only; ignore groups.

## 20.4 Commands

| Command | Calls | Output |
|---|---|---|
| `/events` | `GET /api/v1/events?phase=registration_open&limit=10` | list with inline buttons: Details, Going / Enroll |
| `/event <id>` | `GET /api/v1/events/{id}` | card with participation phase, projection and formation date + buttons from `allowed_actions` (Enroll / Withdraw / Lead a team) + web link |
| `/enroll <id>` | `POST /api/v1/events/{id}/enroll` | confirmation or error text from problem `code` |
| `/team` | `GET /api/v1/me/projects` | current teams with pending actions (confirm placement, re-confirmation or presence check with deadline, accept/decline lead offer, accept swap) as buttons |
| `/invites` | `GET /api/v1/me/invites?status=pending` | list with Accept/Decline |
| `/ask` `<area> <topic>` | `POST /api/v1/help-requests` | creates a help request (goal asked as a follow-up message) |
| `/requests` | `GET /api/v1/help-requests?status=open&area=<my mentor areas>` | open help requests I could take, with Help buttons |
| `/mentorships` | `GET /api/v1/me/mentorships?status=active` | active mentorships with Check in / Close buttons |
| `/confirm` | `GET /api/v1/me/confirmations/pending` | pending confirmation digests as checkbox keyboards |
| `/me` | `GET /api/v1/me`, `/api/v1/users/{id}/metrics`, `/api/v1/users/{id}/scores` | short profile: scores, XP, activity, current teams |
| `/dnd [off\|<days>]` | `PUT /api/v1/me/availability` | DND toggle |
| `/settings` | – | link to web notification settings |
| `/help` | – | command list |

All user-acting calls use the bot token and `X-Telegram-User-Id` (§3.2). Errors are rendered from the problem `code`.

## 20.5 Notification delivery

- One worker loop: long-poll `/internal/notifications/pending`, render, send, ack (§15.2).
- Rendering: per-type English templates, HTML parse mode with escaping. Unknown type → generic text + web link (forward compatible).
- Inline buttons from payload `actions`: one-tap `kind: "api"` calls resolved via shared `ACTION_ROUTES`, or `kind: "link"` web links for forms and navigation. `callback_data`: `a:<key>:<entity_id>` (≤ 64 bytes). On press: fill `{id}` with `entity_id`, call the route with its fixed body as the user, edit the message with the outcome, and answer the callback.
- Idempotency: keep an in-memory LRU of delivered `notification_id`s. Core may redeliver after lease expiry; duplicates within the LRU are acked as `sent` without resending.
- Throttle: ≤ 25 msgs/s global, ≤ 1 msg/s per chat; handle `RetryAfter`.
- `Forbidden: bot was blocked` / `chat not found` → ack `permanent_failure`.

## 20.6 Deliverables

- `bot` container + README.
- A template for every notification type in §15.1, with screenshots or text samples.
- Runs against a fake core (`contracts/fixtures/notifications/*.json` + a tiny stub server) for development.
