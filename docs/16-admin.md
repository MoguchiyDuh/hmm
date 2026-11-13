# 16. Admin

## 16.1 Scope

Admins never approve enrollments, lead claims, placements, events, projects, mentorships, confirmations, activity or new members; those paths don't exist. Admin work is limited to the exception queues:
- new-member review, non-blocking (§9.5);
- import review (§11.3);
- results to verify (§13.1);
- to-check items, punish or pardon (§10.4);
- alerts (§15.3).

## 16.2 Endpoints

Admin capabilities beyond the module-specific ones in other sections:

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/admin/dashboard` | admin | Counts: open alerts by type, pending member reviews, import review queue (incl. member-event duplicates), to-check items, active restrictions, hackathons by participation phase, teams without a lead, teams below `min`, open seats, results to verify, open help requests without offers, parser last heartbeat, job failures in the last 24 h |
| GET | `/api/v1/admin/users?q=&banned=&admin=&cursor=` | admin | Users |
| PATCH | `/api/v1/admin/users/{id}` | admin | `{is_admin?, display_name?, about_md?, reason?}` (§6.3) |
| POST | `/api/v1/admin/users/{id}/ban` | admin | `{reason}` (§6.4) |
| POST | `/api/v1/admin/users/{id}/unban` | admin | `{reason?}` |
| GET | `/api/v1/admin/audit?actor=&entity_type=&entity_id=&cursor=` | admin | Audit log |
| GET | `/api/v1/admin/settings` | admin | Every tunable with its default and description |
| PUT | `/api/v1/admin/settings/{key}` | admin | `{value}`, validated against the key's JSON schema; keys are the §24 table |
| GET | `/api/v1/admin/system` | admin | Parser heartbeats, job queue stats, algo version, failed jobs |
| POST | `/api/v1/admin/jobs/{id}/retry` | admin | Retry a failed job |
