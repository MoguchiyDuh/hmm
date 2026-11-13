# 10. Scores, XP, activity, moderation

## 10.1 Area scores

- `calculated` is written only from algo outputs (`score_onboarding`, `compute_member`).
- `override` is written only by admins. `effective = override ?? calculated`. Recalculation keeps updating `calculated` under an override; admins see both.
- Every change of `effective` writes `area_score_events` with the reason text from algo or the admin.
- The member gets `score_changed` when `effective` changes by at least `score_change_notify_delta` (1.0).
- Area scores grow from all facts (§25): attendances, projects, mentorships, hackathon results, solo entries, GitHub and CV.

## 10.2 Solo activity log

- Member-created entries:

| Field | Rule |
|---|---|
| `type` | `learning`, `course`, `talk`, `writeup`, `oss`, `other` |
| `title` | ≤ 200 |
| `description_md` | ≤ 5000 |
| `links` | ≤ 10 URLs |
| `area_id` | optional |
| `occurred_on` | between today − 365 days and today |

- The owner edits and deletes entries; deletion is soft.
- Always trust `self` (§25): activity, almost no XP. Anything involving other people belongs to events, projects or mentorship.
- Any change enqueues `recompute_member(user)`.

## 10.3 XP and activity

- Produced by algo `compute_member` from the member's full fact history. It recomputes from scratch, so nothing is awarded twice.
- Stored in `member_metrics` plus per-fact `fact_awards`.
- Admin `xp_adjustments` (penalties, rare corrections) are added on top by core: `xp_total = algo_xp + Σ adjustments`.
- The nightly `recompute_all_members` applies activity decay.

## 10.4 To check (abuse handling)

Algo marks anything suspicious; admins decide. Nothing is approved upfront.

**Marking**
- `compute_member` returns per-member flags. The nightly `scan_community` (§18) returns cross-member flags: rings of mutual confirmations, reciprocal mentoring, bursts of member-created events.
- Core upserts each as a `flags` row with status `to_check` and raises admin alert `suspicious_activity` (deduped per user per day).
- A flag stays `to_check` until an admin resolves it, even if later algo runs stop returning it.
- While `to_check`:
  - the flagged subject earns nothing;
  - confirmations given by the flagged member are `ignored`;
  - for a ring, this applies to every member in `related_user_ids`.
- The member is not told.

**Resolving** (`POST /api/v1/admin/flags/{id}/resolve`):

| Decision | Effect |
|---|---|
| `pardon` | Everything counts normally again. Algo receives `flag_status = pardoned` and must not flag the same subject for the same reason again. The member is not notified. |
| `punish` | The admin picks one or more penalties (below). The member gets `penalty_applied` with the reason. |

Penalties (combinable, stored in `flags.penalty`, each one audited):

| Penalty | Effect |
|---|---|
| `exclude` | The subject never earns again. Always included. |
| `xp_penalty {amount}` | An `xp_adjustment` linked to the flag. |
| `revoke_confirmations` | Withdraws every confirmation the member gave in the flagged context. |
| `restrict {kind, until}` | A `member_restrictions` row until a date or indefinitely (below). |
| `ban` | As §6.4. |

**Restrictions**

| Kind | Effect |
|---|---|
| `confirming` | The member's confirmations are stored but `ignored`. No error. |
| `mentoring` | Help offers and mentor matching are blocked: 403 `restricted`. |
| `creating_events` | Creating member events: 403 `restricted`. |
| `creating_projects` | Creating standalone projects: 403 `restricted`. |

- A punished member can't contest it in-app. Admins can lift a restriction (the member gets `restriction_lifted`) or reverse an adjustment later; both are audited.
- The dashboard shows the to-check count; items older than 7 days appear in the weekly admin digest.

## 10.5 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/users/{id}/scores` | member | Effective scores with reasons; the owner and admins also get `calculated` and `override` |
| GET | `/api/v1/users/{id}/scores/history?area=&cursor=` | owner, admin | Score events |
| GET | `/api/v1/users/{id}/activity?cursor=` | member | Solo log |
| POST | `/api/v1/me/activity` | member | Entry |
| PATCH | `/api/v1/me/activity/{id}` | owner | Entry fields |
| DELETE | `/api/v1/me/activity/{id}` | owner | 204 |
| GET | `/api/v1/users/{id}/metrics` | member | → `{xp, activity, reputation: {member, lead, mentor, host}, hackathons_finished, projects_shipped, mentorships_completed}` |
| PUT | `/api/v1/admin/users/{id}/scores/{area_key}/override` | admin | `{value, reason, evidence}`; `evidence` = `interview`, `github_review`, `cv_review` or `other` |
| DELETE | `/api/v1/admin/users/{id}/scores/{area_key}/override` | admin | `{reason?}` |
| POST | `/api/v1/admin/users/{id}/xp-adjustments` | admin | `{amount, reason}` |
| GET | `/api/v1/admin/flags?status=&user=&subject_type=&cursor=` | admin | Subject content, related members, algo reason |
| POST | `/api/v1/admin/flags/{id}/resolve` | admin | `{decision: pardon \| punish, penalties?, note}` |
| GET | `/api/v1/admin/restrictions?user=&active=` | admin | Restrictions |
| DELETE | `/api/v1/admin/restrictions/{id}` | admin | `{reason}`; lifts the restriction |
| POST | `/api/v1/admin/users/{id}/recompute` | admin | 202 |
