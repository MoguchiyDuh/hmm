# 20. Telegram bot

## 20.1 Scope

Notification delivery with inline actions (incl. Help, Confirm, Check in), account linking, a few read/enroll commands. Everything else stays on the web.

## 20.2 Stack and runtime

aiogram 3, long polling. Lives in `hmm.bot` and runs as its own process from the same image (entry point `hmm-bot`), calling domain modules in-process with its own database sessions and deferring jobs to the shared procrastinate queue. Stateless apart from an in-memory FSM for follow-up questions and the delivery LRU.

Config: `BOT_TOKEN`, `DATABASE_URL`, `PUBLIC_URL` (for web links). Messages use HTML parse mode with link previews off. When `PUBLIC_URL` is not `https://`, web links go into the message text instead of URL buttons, which Telegram rejects.

| Module | Role |
|---|---|
| `main.py` | entry point: bot, dispatcher, command menu, delivery task |
| `handlers.py` | aiogram router, FSM flows, callback handling |
| `commands.py` | command and callback logic, returns `Reply` |
| `runtime.py` | `BotRuntime`, `respond` (actor + transaction + error text) |
| `templates.py` | per-type notification templates and keyboards |
| `delivery.py` | lease/send/ack loop, throttle |
| `callbacks.py` | `CallbackData` factories |

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
| `/event <id\|slug>` | `GET /api/v1/events/{id}` | card with participation phase, live projection and formation date + buttons from `allowed_actions` (Enroll / Withdraw / Party) + web link |
| `/enroll <id\|slug>` | `POST /api/v1/events/{id}/enroll` | confirmation or error text from problem `code` |
| `/team` | `GET /api/v1/me/projects` | current teams with pending actions (accept or decline a placement before `accept_by`) as buttons |
| `/invites` | `GET /api/v1/me/invites?status=pending` | list with Accept/Decline |
| `/ask` `<area> <topic>` | `POST /api/v1/help-requests` | creates a help request (goal asked as a follow-up message); without arguments lists area keys |
| `/requests` | `GET /api/v1/help-requests?status=open&area=<my mentor areas>` | open help requests I could take, with Help buttons |
| `/mentorships` | `GET /api/v1/me/mentorships` | active, stalled and closing mentorships with Check in / Close buttons |
| `/confirm` | `GET /api/v1/me/confirmations/pending` | pending confirmation digests as checkbox keyboards |
| `/me` | `GET /api/v1/me`, `/api/v1/users/{id}/metrics`, `/api/v1/users/{id}/scores` | short profile: scores, XP, activity, current teams |
| `/dnd [off\|<days>]` | `PUT /api/v1/me/availability` | DND toggle |
| `/settings` | – | link to web notification settings |
| `/unlink` | – | §20.3 |
| `/cancel` | – | drops the pending follow-up question |
| `/help` | – | command list |

Each command resolves the linked user with `identity.service.telegram_actor` (§6.5) and runs the domain functions behind the listed endpoint in one transaction, with the same permission checks (§3.2). Errors are rendered from the `ProblemError` code; validation errors keep a follow-up question open so the user can resend.

Follow-ups: Help (offer message), Check in (note) and `/ask` (goal) wait for one plain-text message. Close asks for the outcome with Goal reached / Partly / Not reached buttons. Confirm buttons flip ☐ to ☑ in place.

`callback_data` prefixes (aiogram packs UUIDs as 32 hex chars): `a` API action, `e` event op (show/going/enroll/withdraw/party), `h` help, `ci` check in, `cl` close, `o` outcome, `c` confirm.

## 20.5 Notification delivery

- One delivery loop: lease up to 50, send each, ack each in its own transaction; stop sending after 45 s so leases do not expire mid-batch.
- Rendering: per-type English templates, HTML parse mode with escaping. Unknown type → generic text + web link (forward compatible). Samples: [20-bot-samples.md](20-bot-samples.md).
- Inline buttons from payload `actions`, API buttons in one row first, labelled by action key: one-tap `kind: "api"` calls resolved via shared `ACTION_ROUTES`, or `kind: "link"` web links for forms and navigation. `callback_data`: `a:<key>:<entity_id>` (≤ 64 bytes). On press: `notifications.actions.run_action(key, entity_id)` runs the route's domain function with its fixed body as the user; edit the message with the outcome, and answer the callback.
- Idempotency: keep an in-memory LRU of delivered `notification_id`s. An expired lease is redelivered; duplicates within the LRU are acked as `sent` without resending.
- Link actions with a bot flow become callback buttons instead of web links: Help, Check in, Close. Confirmation digests get one ☐ button per fact (max 20).
- Throttle: ≤ 25 msgs/s global, ≤ 1 msg/s per chat; `RetryAfter` sleeps and retries up to 3 times, then acks `failed`.
- `Forbidden` / `chat not found` / `user is deactivated` / `bot was blocked` → ack `permanent_failure`. Other API errors → `failed` (backoff, §15.2).
- Blocking the bot (`my_chat_member` → kicked) unlinks the account.

## 20.6 Deliverables

- `hmm-bot` entry point and a `bot` service in the compose files.
- A template for every notification type in §15.1, with text samples in [20-bot-samples.md](20-bot-samples.md).
- Template tests over `tests/fixtures/notifications/*.json` (`tests/test_bot_templates.py`); delivery, linking and command tests in `tests/test_bot.py`.
