# 24. Settings defaults

## 24.1 Settings defaults (admin-tunable)

| key | default | used in |
|---|---|---|
| `team_size_default_min` / `max` | 2 / 5 | §12.2 |
| `formation_lead_hours` | 72 | §12.2 |
| `accept_hours` | 24 | §12.2, §12.6 |
| `invite_ttl_days` | 7 (capped by `reg_deadline`) | §12.4, §27.3 |
| `results_due_days` | 7 | §13.1 |
| `results_close_days` | 30 | §12.8, §13.1 |
| `review_window_days` | 7 | §13.2 |
| `activity_window_days` | 14 | §4.1, §10.3 |
| `score_change_notify_delta` | 1.0 | §10.1 |
| `member_review_reminder_days` | 7 | §9.5 |
| `import_auto_publish_confidence` | 0.8 | §11.2 |
| `parser_heartbeat_max_age_hours` | 26 | §11.2 |
| `session_ttl_days` | 30 | §6.2 |
| `github_manual_refresh_min_interval_minutes` | 60 | §7.2 |
| `cv_max_bytes` | 5 242 880 | §7.3 |
| `event_archive_after_days` | 180 | §11.3 |
| `attendance_claim_days` | 30 | §26.1 |
| `project_size_max` | 20 | §27.2 |
| `projects_led_max` | 5 | §27.2 |
| `project_idle_days` | 30 | §27.4 |
| `project_review_min_days` | 3 | §27.5 |
| `mentor_capacity_default` | 3 (active mentorships as mentor, all areas; used while `users.mentor_capacity` is null) | §28.2 |
| `mentee_active_max` | 2 | §28.3 |
| `help_request_ttl_days` | 14 | §28.3 |
| `mentor_notify_top_n` | 5 | §28.3 |
| `mentor_min_score` | 6.0 | §28.4 |
| `mentor_min_gap` | 2.0 | §28.4 |
| `mentorship_idle_days` | 14 | §28.5 |
| `confirmation_withdraw_days` | 7 | §25.2 |

- A changed setting does not move deadlines that are already stored.
- Related keys must satisfy: `team_size_default_min ≤ team_size_default_max`, `results_due_days < results_close_days`.
