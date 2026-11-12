# 17. Core — Background jobs

All jobs are idempotent, with retries (exponential, max 5) unless noted. A job that exhausts its retries → admin alert `job_failing` (deduped per task name per hour).

| Job | Trigger | Lock | Work |
|---|---|---|---|
| `github_sync(user)` | login (stale), manual, daily | user | §7 |
| `github_sync_all` | cron 03:00 | – | enqueue per user, spread over 2 h |
| `cv_extract(user)` | upload | user | §7 |
| `score_onboarding(user)` | submit (waits per §9.1) | user | algo `score_onboarding` |
| `recompute_member(user)` | profile, solo log, CV, GitHub, result, override, flag resolution, any fact or confirmation change | `member:<id>` | algo `compute_member` → scores, metrics, awards, flags; then `compute_lead_fits_for_member` |
| `recompute_all_members` | cron 04:00, area added | – | enqueue per user |
| `compute_lead_fits(hackathon)` | publish, areas changed, nightly | `leadfit:<hid>` | algo `lead_fit` |
| `compute_lead_fits_for_member(user)` | after `recompute_member` | – | algo `lead_fit` for `open`/`locked` hackathons, one candidate |
| `refresh_recommendations(team)` | §14 | `rec:<team>` | algo `recommend` |
| `run_formation(hackathon, kind)` | §12.10, admin | `formation:<hid>` | algo `form_teams` → validate → apply (§12.5) |
| `rank_reserve(hackathon)` | §14 | `reserve:<hid>` | algo `rank_reserve` |
| `match_mentors(request)` | help request opened | `mentor:<rid>` | algo `match_mentors` → notify |
| `scan_community` | cron 02:00 | singleton | algo `scan_community` → to-check flags |
| `community_tick` | every 15 min | singleton | help request expiry, mentorship idle/stalled/close timers, project idle/pause, lead offer expiry for projects, confirmation and review digests, attendance `going` expiry |
| `event_tick` | every 5 min | singleton | §12.10 (incl. presence checks, seat filling, result auto-close), review windows (§13), reminders |
| `admin_digest` | weekly, Monday 09:00 UTC | singleton | pending member reviews, to-check items older than 7 days, member-event duplicates, results to verify, open alerts summary |
| `archive_old_events` | nightly | singleton | §11 |
| `cleanup` | nightly | singleton | expired sessions, link codes, old deliveries, `algo_cache` expiry |

Algo timeouts: `score_onboarding` 60 s, `compute_member` 60 s, `lead_fit` 120 s, `recommend` 60 s, `form_teams` 120 s, `rank_reserve` 60 s, `match_mentors` 60 s, `scan_community` 600 s. A timeout counts as a failure and is retried.
