# 20. Telegram bot

## 20.1 Scope

Notification delivery with inline actions (incl. Help, Confirm, Check in), account linking, a few read/enroll commands. Everything else stays on the web.

## 20.2 Stack and runtime

aiogram 3, long polling. Lives in `hmm.bot` and runs as its own process from the same image (entry point `hmm-bot`), calling domain modules in-process with its own database sessions and deferring jobs to the shared procrastinate queue. Stateless apart from an in-memory FSM for follow-up questions and the delivery LRU.

Config: `BOT_TOKEN`, `DATABASE_URL`, `PUBLIC_URL` (for web links). Messages use HTML parse mode with link previews off. When `PUBLIC_URL` is not `https://` there are no web links at all (Telegram rejects such URL buttons and doesn't render localhost hrefs); only the linking instructions and error texts show the plain URL.

| Module | Role |
|---|---|
| `main.py` | entry point: bot, dispatcher, command menu, delivery task |
| `handlers.py` | aiogram router, FSM flows, callback handling |
| `commands.py` | command and callback logic, returns `Reply` |
| `runtime.py` | `BotRuntime`, `respond` (actor + transaction + error text) |
| `templates.py` | notification rendering and keyboards |
| `templates_en.py`, `templates_ru.py` | per-type notification templates per language |
| `i18n.py` | `Texts` string table type, Russian/English plural rules, Telegram language detection |
| `texts_en.py`, `texts_ru.py` | command, button, label and error strings per language |
| `delivery.py` | lease/send/ack loop, throttle |
| `callbacks.py` | `CallbackData` factories |

Language: every reply and notification is rendered in the user's `users.locale` (§3.7). Unlinked users get the language of their Telegram app (`language_code` `ru*` → Russian, else English). Dates use localized weekday and month names; Russian counts use the three plural forms. A test keeps both string tables and template sets complete.

## 20.3 Account linking

- `/start <code>` → `identity.service.link_telegram` → "Linked to <display_name>" with the main menu (Events / My teams / Mentorships / Profile). If `users.locale` is still `en` and the Telegram app is Russian, linking sets it to `ru`. Errors:
  - `code_invalid` / `code_expired` → tell the user to generate a new code on the web;
  - `telegram_already_linked` → this Telegram account belongs to another user, who must unlink it first.
- `/start` without a code greets a linked user with the main menu; an unlinked user, or any command or text from one, gets a message linking to `PUBLIC_URL/settings#telegram`.
- `/unlink` → `identity.service.unlink_telegram_account`.
- Private chats only; ignore groups.

## 20.4 Commands

| Command | Same as | Output |
|---|---|---|
| `/events` | `GET /api/v1/events?phase=registration_open&limit=10` | list with one button per event (its title) opening the event card |
| `/event <id\|slug>` | `GET /api/v1/events/{id}` | hidden; event card: dates, format, team size, queue and projected teams, formation date, my status + buttons from `allowed_actions` (I'm going / Find me a team / Start a party / Leave the queue) and ⬅️ All events |
| `/enroll <id\|slug>` | `POST /api/v1/events/{id}/enroll` | hidden; confirmation or error text from problem `code` |
| `/team` | `GET /api/v1/me/projects` | current teams with pending actions (accept or decline a placement before `accept_by`) as buttons |
| `/invites` | `GET /api/v1/me/invites?status=pending` | list with Accept/Decline |
| `/ask [<area> [<topic>]]` | `POST /api/v1/help-requests` | guided: area buttons → topic → goal, each step a follow-up; arguments skip steps |
| `/requests` | `GET /api/v1/help-requests?status=open&area=<my mentor areas>` | open help requests I could take, with Help buttons |
| `/mentorships` | `GET /api/v1/me/mentorships` | active, stalled and closing mentorships with Check in / Close buttons |
| `/confirm` | `GET /api/v1/me/confirmations/pending` | pending confirmation digests as checkbox keyboards |
| `/profile` | `GET /api/v1/me`, `/api/v1/users/{id}/metrics`, `/api/v1/users/{id}/scores` | short profile: top areas, XP, activity, totals, current teams; zero values are left out. `/me` is a hidden alias |
| `/dnd [off\|<days>]` | `PUT /api/v1/me/availability` | DND toggle |
| `/settings` | – | link to web notification settings |
| `/language` | `PATCH /api/v1/me/profile` (`locale`) | 🇬🇧 English / 🇷🇺 Русский buttons; tapping saves `users.locale` and re-renders the message in place; works before onboarding is done |
| `/unlink` | – | §20.3 |
| `/cancel` | – | drops the pending follow-up question |
| `/help` | – | command list |

Hidden commands work but are left out of the Telegram command menu and `/help`. Other text from a linked user outside a follow-up gets a pointer to `/help`.

Each command resolves the linked user with `identity.service.telegram_actor` (§6.5) and runs the domain functions behind the listed endpoint in one transaction, with the same permission checks (§3.2). Errors are rendered from the `ProblemError` code; validation errors keep a follow-up question open so the user can resend.

Follow-ups: Help (offer message), Check in (note) and `/ask` (topic, then goal) wait for one plain-text message. Every prompt carries a Cancel button; when the flow ends, its prompt's keyboard is removed. Close asks for the outcome with Goal reached / Partly / Not reached / Cancel buttons. Confirm buttons flip ☐ to ☑ in place.

Event buttons edit the message in place: the card replaces the list, ⬅️ All events goes back, and an op re-renders the card with the result on top (also shown as a toast). Starting a party adds an Invite teammates web button.

The main entity of a reply (event, team, project, topic, profile) is a bold inline link to its web page; there are no separate "open on web" buttons. Times are shown in the user's timezone (else the event's, else UTC) with a relative hint such as "(in 3 days)". Lists number their buttons ("1 · Accept") only when there is more than one item.

`callback_data` prefixes (aiogram packs UUIDs as 32 hex chars): `a` API action, `e` event op (show/list/going/enroll/withdraw/party), `h` help, `ci` check in, `cl` close, `o` outcome, `c` confirm, `m` menu item, `aa` ask area, `x` cancel, `l` language.

Command menu: the default list is English, a `language_code=ru` list serves Russian Telegram apps, and linking or `/language` sets a per-chat list in the user's language (removed again on unlink). A language change made on the web shows up in replies right away but updates the per-chat menu only on the next link or `/language`.

## 20.5 Notification delivery

- One delivery loop: lease up to 50, send each, ack each in its own transaction; stop sending after 45 s so leases do not expire mid-batch.
- Rendering: per-type templates in the recipient's language, HTML parse mode with escaping. Unknown type → generic text (forward compatible). Times use the recipient's timezone and a relative hint, as in §20.4. Samples: [20-bot-samples.md](20-bot-samples.md), [20-bot-samples-ru.md](20-bot-samples-ru.md).
- Inline buttons from payload `actions`, API buttons in one row first, labelled by action key: one-tap `kind: "api"` calls resolved via shared `ACTION_ROUTES`, or `kind: "link"` web links for forms and navigation. `callback_data`: `a:<key>:<entity_id>` (≤ 64 bytes). On press: `notifications.actions.run_action(key, entity_id)` runs the route's domain function with its fixed body as the user; edit the message with the outcome, and answer the callback.
- Idempotency: keep an in-memory LRU of delivered `notification_id`s. An expired lease is redelivered; duplicates within the LRU are acked as `sent` without resending.
- Link actions with a bot flow become callback buttons instead of web links: Help, Check in, Close. Confirmation digests get one ☐ button per fact (max 20), plus a See everyone web link only beyond that.
- Web link buttons appear only for link actions without a bot flow (Write reviews, Endorse, Ask again, Submit result, Open project, Transfer lead, Open alert), plus Open admin on the admin digest and Continue onboarding on onboarding reopened.
- Low-priority types are sent with `disable_notification`: invite outcomes, member joined/left, event changed, offer closed, review received/removed, score changes, admin digest.
- Throttle: ≤ 25 msgs/s global, ≤ 1 msg/s per chat; `RetryAfter` sleeps and retries up to 3 times, then acks `failed`.
- `Forbidden` / `chat not found` / `user is deactivated` / `bot was blocked` → ack `permanent_failure`. Other API errors → `failed` (backoff, §15.2).
- Blocking the bot (`my_chat_member` → kicked) unlinks the account.

## 20.6 Deliverables

- `hmm-bot` entry point and a `bot` service in the compose files.
- An English and a Russian template for every notification type in §15.1, with text samples in [20-bot-samples.md](20-bot-samples.md) and [20-bot-samples-ru.md](20-bot-samples-ru.md).
- Template tests over `tests/fixtures/notifications/*.json` (`tests/test_bot_templates.py`); delivery, linking and command tests in `tests/test_bot.py`.
