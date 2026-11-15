# 17. Background jobs

## 17.1 Rules

- Every job is idempotent and retried with exponential backoff, at most 5 times, unless noted.
- A job that exhausts its retries → admin alert `job_failing` (deduped per task name per hour); the next successful run of that task resolves it.
- Algo timeouts: `score_onboarding` 60 s, `compute_member` 60 s, `recommend` 60 s, `form_teams` 120 s, `match_mentors` 60 s, `scan_community` 600 s. A timeout counts as a failure and is retried.

## 17.2 Jobs

| Job | Trigger | Lock | Work |
|---|---|---|---|
| `github_sync(user)` | Login (stale snapshot), manual refresh, daily | user | §7.2 |
| `github_sync_all` | Cron 03:00 | – | Enqueue per user, spread over 2 h |
| `cv_extract(user)` | Upload | user | §7.3 |
| `score_onboarding(user)` | Submit (waits per §9.1) | user | Algo `score_onboarding` |
| `recompute_member(user)` | Profile, solo log, CV, GitHub, result, override, flag resolution, any fact or confirmation change | `member:<id>` | Algo `compute_member` → scores, metrics, awards, flags |
| `recompute_all_members` | Cron 04:00, area added | – | Enqueue per user |
| `refresh_recommendations(project)` | §14.1 | `rec:<project>` | Algo `recommend` |
| `run_formation(hackathon)` | §12.9, form now | `formation:<hid>` | Algo `form_teams` → validate → apply; `pack()` on failure (§12.5) |
| `match_mentors(request)` | Help request opened | `mentor:<rid>` | Algo `match_mentors` → notify |
| `scan_community` | Cron 02:00 | singleton | Algo `scan_community` → to-check flags |
| `community_tick` | Every 15 min | singleton | Help request expiry; mentorship idle, stalled and close timers; project idle and pause; attendance confirmation digests and `going` expiry; parser heartbeat check (§11.2) |
| `event_tick` | Every 5 min | singleton | §12.9 per hackathon (formation due, `formation_soon`, accept reminder and lapse, refill, `reg_deadline`, `starts_at`, invite expiry), result auto-close (§13.1), review windows (§13.2), reminders |
| `admin_digest` | Weekly, Monday 09:00 UTC | singleton | Member reviews pending longer than `member_review_reminder_days`, to-check items older than 7 days, member-event duplicates, results to verify, open alerts summary |
| `archive_old_events` | Nightly | singleton | §11.3 |
| `cleanup` | Nightly | singleton | Expired sessions, link codes, old deliveries, `algo_cache` expiry |
| `parser_run` | Every 30 min | singleton | Run due parser sources (§19.5), import in batches of 50 (§11.2), record source state and a heartbeat. Timeout 1800 s |
