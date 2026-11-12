# 10. Core — Scores, XP, activity, moderation

## Area scores
- `calculated` is written only by algo outputs (`score_onboarding`, `compute_member`).
- `override` is written only by admins. `effective = override ?? calculated`. Recalculation continues to update `calculated` under an override; UI shows both to admins.
- Each change of `effective` writes `area_score_events` with reason text from algo or admin.
- `score_changed` notification to member when effective changes by ≥ 1.0 (configurable).
- Area scores grow from all facts (§25): attendances, projects, mentorships, hackathon results, solo entries, GitHub, CV.

## Solo activity log
- Member-created entries: `type` (`learning|course|talk|writeup|oss|other`), `title` (≤ 200), `description_md` (≤ 5000), `links` (≤ 10 URLs), `area_id?`, `occurred_on` (≤ today, ≥ today − 365 d). Editable/deletable by owner; delete is soft.
- Always trust `self` (§25): activity, almost no XP. Anything involving other people belongs to events, projects or mentorship.
- Any change enqueues `recompute_member(user)`.

## XP and activity numbers
- Produced by algo `compute_member` from the member's full history of facts (recompute-from-scratch, so no double awards).
- Stored in `member_metrics` + per-fact `fact_awards`.
- `xp_adjustments` by admins (penalties, rare corrections) are added on top by core: `xp_total = algo_xp + Σ adjustments`.
- Nightly `recompute_all_members` applies time decay of activity.

## To check (abuse handling)
Algo marks anything suspicious; admins decide. Nothing is approved upfront.

**Marking**
- `compute_member` returns per-member flags, and nightly `scan_community` (§18) returns cross-member flags (rings of mutual confirmations, reciprocal mentoring, bursts of member-created events).
- Core upserts each as a `flags` row with status `to_check` and raises admin alert `suspicious_activity` (deduped per user per day).
- While `to_check`:
  - the flagged subject earns nothing;
  - confirmations given by the flagged member are `ignored`;
  - for a ring, this applies to every member in `related_user_ids`.
- The member is not told.

**Resolving** (`POST /admin/flags/{id}/resolve`):

| Decision | Effect |
|---|---|
| `pardon` | Everything counts normally again. Algo receives `flag_status=pardoned` and must not flag the same subject for the same reason again. Member not notified. |
| `punish` | Admin picks one or more penalties (below). The member gets `penalty_applied` with the reason. |

Penalties (combinable, stored in `flags.penalty`, every one audited):
- `exclude`: the subject never earns again (default, always included);
- `xp_penalty {amount}`: an `xp_adjustment` linked to the flag;
- `revoke_confirmations`: withdraw all confirmations the member gave in the flagged context;
- `restrict {kind, until}`: `confirming`, `mentoring`, `creating_events` or `creating_projects`, until a date or indefinitely (`member_restrictions`). A restricted action → 403 `restricted`;
- `ban`: as §6.

- A punished member can't contest it in-app. Admins can lift a restriction or reverse an adjustment later (audited).
- The dashboard shows the to-check count; items older than 7 days appear in the weekly admin digest.

## Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/users/{id}/scores` | member → effective scores + reasons; admins/self also get calculated/override |
| GET | `/api/v1/users/{id}/scores/history?area=&cursor=` | self/admin |
| GET | `/api/v1/users/{id}/activity?cursor=` | member (solo log) |
| POST | `/api/v1/me/activity` | member |
| PATCH | `/api/v1/me/activity/{id}` | owner |
| DELETE | `/api/v1/me/activity/{id}` | owner |
| GET | `/api/v1/users/{id}/metrics` | member → `{xp, activity, reputation: {member, lead, mentor, host}, hackathons_finished, projects_shipped, mentorships_completed}` |
| PUT | `/api/v1/admin/users/{id}/scores/{area_key}/override` | admin `{value, reason, evidence: interview\|github_review\|cv_review\|other}` |
| DELETE | `/api/v1/admin/users/{id}/scores/{area_key}/override` | admin `{reason?}` |
| POST | `/api/v1/admin/users/{id}/xp-adjustments` | admin `{amount, reason}` |
| GET | `/api/v1/admin/flags?status=to_check&user=&subject_type=&cursor=` | admin (subject content, related members, algo reason) |
| POST | `/api/v1/admin/flags/{id}/resolve` | admin `{decision: pardon\|punish, penalties?, note}` |
| GET | `/api/v1/admin/restrictions?user=&active=` | admin |
| DELETE | `/api/v1/admin/restrictions/{id}` | admin `{reason}` (lift) |
| POST | `/api/v1/admin/users/{id}/recompute` | admin, 202 |
