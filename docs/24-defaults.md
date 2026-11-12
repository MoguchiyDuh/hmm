# 24. Settings defaults and open points

## Settings defaults (admin-tunable)

| key | default | used in |
|---|---|---|
| `team_size_default_min` / `max` | 2 / 5 | §12.2 |
| `viable_ratio` | 0.75 | §12.2 |
| `core_ratio` | 0.5 | §12.2 |
| `registration_buffer_hours` | 48 | §12.2 |
| `formation_lead_hours` | 72 | §12.2 |
| `formation_min_notice_hours` | 24 | §12.2 |
| `review_hours` | 48 | §12.2 |
| `review_min_hours` | 12 | §12.2 |
| `late_core_hours` | 12 | §12.7 |
| `late_review_hours` | 24 | §12.7 |
| `seat_fill_hours` | 12 | §12.7 |
| `late_claim_grace_hours` | 24 | §12.7 |
| `presence_check_hours` | 48 | §12.7 |
| `presence_confirm_hours` | 12 | §12.7 |
| `lead_offer_hours` | 12 | §12.8 |
| `reserve_offer_hours` | 12 | §12.6, §12.7 |
| `confirm_reminder_hours` | 24, 6, 1 | §12.6, §12.7 |
| `lead_fit_threshold` | 0.6 | §12.4, §12.8, §14 |
| `invite_ttl_days` | 7 (capped by phase deadlines) | §12.4 |
| `results_due_days` | 7 | §12.10, §13 |
| `results_close_days` | 30 | §12.10, §13 |
| `review_window_days` | 7 | §13 |
| `activity_window_days` | 14 | §10 |
| `score_change_notify_delta` | 1.0 | §10 |
| `member_review_reminder_days` | 7 | §9.5 |
| `import_auto_publish_confidence` | 0.8 | §11 |
| `parser_heartbeat_max_age_hours` | 26 | §11 |
| `session_ttl_days` | 30 | §6 |
| `github_manual_refresh_min_interval_minutes` | 60 | §7 |
| `cv_max_bytes` | 5 242 880 | §7 |
| `event_archive_after_days` | 180 | §11 |
| `attendance_claim_days` | 30 | §26.1 |
| `project_size_max` | 20 | §27.2 |
| `projects_led_max` | 5 | §27.2 |
| `project_idle_days` | 30 | §27.4 |
| `project_review_min_days` | 3 | §27.5 |
| `project_lead_offer_days` | 3 | §27.6 |
| `mentor_capacity_default` | 3 (active mentorships as mentor, all areas; used while `users.mentor_capacity` is null) | §28.2 |
| `mentee_active_max` | 2 | §28.3 |
| `help_request_ttl_days` | 14 | §28.3 |
| `mentor_notify_top_n` | 5 | §28.3 |
| `mentor_min_score` | 6.0 | §28.4 |
| `mentor_min_gap` | 2.0 | §28.4 |
| `mentorship_idle_days` | 14 | §28.5 |
| `confirmation_withdraw_days` | 7 | §25.2 |

A changed setting does not move already stored deadlines. Related keys must satisfy: `team_size_default_min ≤ team_size_default_max`, `formation_min_notice_hours ≤ formation_lead_hours`, `review_min_hours ≤ review_hours`, `presence_confirm_hours < presence_check_hours`, `results_due_days < results_close_days`, `confirm_reminder_hours` strictly descending.

## Open points
- Parser source coverage (CIS, global or both) is decided by the parser group.
