# 15. Core — Notifications and admin alerts

## Notifications
- Created only by core, in the same transaction as the cause. `dedupe_key` prevents duplicates from retried jobs.
- For each notification, if the user has Telegram linked and the type's Telegram toggle is on → `notification_deliveries(channel=telegram, status=pending)`.
- Web: inbox + unread count; frontend polls `GET /notifications/unread-count` every 30 s and on focus.
- Payload: typed per `type` in `contracts/notifications.py`. Each payload includes entity IDs and denormalized display strings, so clients render without extra calls. Buttons are `actions: [{kind: "api", key, entity_id, label_key} | {kind: "link", label_key, url}]`: one-tap API calls resolved via shared `ACTION_ROUTES`, or web links for forms and navigation. Telegram callbacks use `a:<key>:<entity_id>` (≤ 64 bytes).

| type | recipient | actions |
|---|---|---|
| `invite_received` (`kind` core/fill) | invitee | accept / decline |
| `invite_accepted`, `invite_declined`, `invite_expired`, `invite_closed` | lead / invitee | – |
| `join_request_received` | lead | accept / reject |
| `join_request_accepted`, `join_request_rejected` | requester | – |
| `formation_soon` | enrolled members, leads | – |
| `formation_rescheduled` | enrolled members, leads | – |
| `team_proposed` | placed member (with `confirm_by`) | confirm / decline |
| `team_reconfirm` | existing core member at formation (with `confirm_by`) | confirm / leave |
| `presence_check` | every team member before `starts_at` (with `confirm_by`) | confirm / leave |
| `lead_offer` (`source` formation/succession/transfer) | offered member | accept / decline |
| `lead_offer_declined` | transferring lead | – |
| `formation_ready` | claimed leads (with `confirm_by`) | confirm / open team |
| `reserve_placed` | reserve members (with rank) | – |
| `confirm_reminder` | proposed members, 24/6/1 h before `confirm_by` | confirm / decline |
| `review_ending` | leads of teams without a lead or below `V` | open team |
| `placement_lapsed` | removed silent member | enroll again |
| `swap_request_received` | other lead, both moved members | accept / decline |
| `merge_proposed` | both leads | accept / decline |
| `swap_result`, `merge_result`, `placement_changed` | leads / moved members | – |
| `team_locked` | team, reserve | – |
| `team_member_joined`, `team_member_left` | lead / team | – |
| `seat_opened` | lead (with quick-pick and head-start deadline) | open team |
| `seat_autofilled` | lead | – |
| `reserve_offer` | offered reserve member (with `confirm_by`) | confirm / decline |
| `lead_opportunity` | eligible enrolled/reserve members | claim lead |
| `lead_transferred`, `lead_stepped_down` | team | – |
| `lead_assigned` | forced lead | transfer / step down |
| `team_disbanded`, `team_status_changed` | team | – |
| `event_changed`, `event_cancelled` | team members, enrolled members, `going` attendees | – |
| `attendance_confirm_digest` | attendees after `ends_at`; member organizer/host | confirm attendees |
| `project_invite_received` | invitee | accept / decline |
| `project_join_request_received` | project lead | accept / reject |
| `project_member_joined`, `project_member_left`, `project_member_removed` | lead / member | – |
| `project_idle`, `project_paused` | lead / members | open project |
| `project_released` | members, followers of the feed item | – |
| `contributions_confirm_digest` | project members after a release | confirm lead |
| `project_lead_offer`, `project_lead_claimable` | member / members of a paused project | accept / claim |
| `help_request_match` | suggested mentors | help |
| `help_request_direct` | target mentor | help / decline |
| `help_request_declined` | learner | open to everyone |
| `help_offer_received` | learner | accept |
| `help_offer_accepted`, `help_offer_closed` | offer author | – |
| `help_request_expired` | learner | reopen |
| `mentorship_started` | both sides | – |
| `mentorship_idle`, `mentorship_stalled` | both sides | check in |
| `mentorship_close_proposed` | learner | close with outcome |
| `mentorship_closed` | mentor | acknowledge / endorse |
| `penalty_applied` | punished member | – |
| `restriction_lifted` | member | – |
| `result_due` | lead; every confirmed member after `results_due_days` | submit result |
| `reviews_open`, `reviews_pending` | participants of a review window (team, project, mentorship) | review |
| `review_received` | reviewee (digest at reveal) | – |
| `review_removed` | reviewer | – |
| `score_changed` | member | – |
| `scores_reviewed` | member (admin review completed) | – |
| `onboarding_reopened` | member | – |
| `telegram_link_broken` | member (web only) | – |
| `admin_alert` | all admins | open alert |

Defaults: all types on for web and Telegram, except `score_changed`, `team_member_joined`, `project_member_joined`, `project_released` and `attendance_confirm_digest` (Telegram off).

## Telegram delivery contract (bot pulls)
- `GET /internal/notifications/pending?channel=telegram&limit=50&wait=25` long-poll: returns up to `limit` deliveries with `next_attempt_at ≤ now`, leases them for 60 s. Items: `{delivery_id, notification_id, telegram_chat_id, type, payload, created_at}`.
- `POST /internal/notifications/ack` `{results: [{delivery_id, status: sent|failed|permanent_failure, external_id?, error?}]}`.
  - `sent` → done.
  - `failed` → attempts+1, exponential backoff (30 s … 1 h), max 8 attempts, then `failed` final.
  - `permanent_failure` (user blocked bot / chat not found) → mark link `broken_at`, stop Telegram for user, create `telegram_link_broken` web notification.
- Lease expiry without ack → redelivered (bot must tolerate, see §20).
- Deliveries still pending after 7 days are `skipped`.

## Admin alerts
- Separate table, shown on the admin dashboard. `warning` and `critical` alerts are also fanned out as `admin_alert` notifications to each admin (respecting toggles); `info` alerts stay on the dashboard.
- Deduped by `dedupe_key` while open; auto-resolved when the condition disappears (e.g. `team_below_min` resolves when the team reaches `min`).

| type | severity | trigger |
|---|---|---|
| `formation_undersized` | info | core below `V` after formation (§12.5) |
| `formation_failed` | warning | algo plan invalid, fallback used, or the formation job failed (§12.5) |
| `team_below_min` | warning | team below `min` with an empty reserve, disbanded at `reg_deadline`, or dropped below `min` after it (§12.7) |
| `event_dates_changed` | info | dates changed while teams are in `review`/`locked` (§12.2) |
| `forced_lead`, `lead_below_threshold` | info | lead role forced or transferred below threshold (§12.4, §12.8) |
| `result_overdue` | warning | §12.10 |
| `import_review` | info | §11 |
| `event_cancelled_with_teams` | warning | §11 |
| `team_size_conflict` | warning | §11 |
| `suspicious_activity` | warning | new to-check item (§10) |
| `restriction_violation` | info | restricted member attempted the restricted action repeatedly (§10) |
| `review_reported` | info | §13 |
| `banned_member_in_active_team` | warning | §6 |
| `algo_failure`, `job_failing` | critical | §14, §17 |
| `parser_down` | critical | §11 |

The new-member review queue (§9.5), member-event duplicates (§26.3) and results to verify (§13) are dashboard counters plus a weekly digest, not per-item alerts.

## Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/notifications?unread=&cursor=` | user |
| GET | `/api/v1/notifications/unread-count` | user |
| POST | `/api/v1/notifications/{id}/read` | user |
| POST | `/api/v1/notifications/read-all` | user |
| GET | `/internal/notifications/pending` | bot |
| POST | `/internal/notifications/ack` | bot |
| GET | `/api/v1/admin/alerts?status=&type=&cursor=` | admin |
| POST | `/api/v1/admin/alerts/{id}/resolve` | admin `{note}` |
