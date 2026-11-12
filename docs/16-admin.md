# 16. Core — Admin

Admin capabilities beyond module-specific ones above:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/admin/dashboard` | counts: open alerts by type, pending member reviews, import review queue (incl. member-event duplicates), to-check items, active restrictions, hackathons by participation phase, teams without a lead, teams below `min`, open seats, results to verify, open help requests without offers, parser last heartbeat, job failures 24 h |
| GET | `/api/v1/admin/users?q=&banned=&admin=&cursor=` | list |
| PATCH | `/api/v1/admin/users/{id}` | `{is_admin, display_name, about_md}` (+reason) |
| POST | `/api/v1/admin/users/{id}/ban` | `{reason}` |
| POST | `/api/v1/admin/users/{id}/unban` | |
| GET | `/api/v1/admin/audit?actor=&entity_type=&entity_id=&cursor=` | audit log |
| GET | `/api/v1/admin/settings` | all tunables with defaults and descriptions |
| PUT | `/api/v1/admin/settings/{key}` | `{value}` validated against the key's JSON schema and together with related keys (min ≤ max etc.); keys are the §24 table |
| GET | `/api/v1/admin/system` | parser heartbeats, job queue stats, algo version, failed jobs |
| POST | `/api/v1/admin/jobs/{id}/retry` | retry a failed job |

Admins never approve enrollments, lead claims, placements, events, projects, mentorships, confirmations, activity or new members. Those paths don't exist by design. Admin work is limited to the exception queues: new-member review (non-blocking), import review, results to verify (§13), to-check items (punish or pardon), alerts.
