# 26. Core — Event attendance

## 26.1 Going and attended

`event_attendances(event_id, user_id, status, role, proof_links, note, trust)`, one per (event, member).

| status | Allowed when | Meaning |
|---|---|---|
| `going` | event `published`, `now < ends_at` | plans to attend; visible on the event page and in the feed |
| `attended` | `now ≥ starts_at` and `now ≤ ends_at + attendance_claim_days (30)` | was there; this is the fact that earns points |
| `cancelled` | anytime | changed mind; no points, no penalty |

- `role`: `attendee` (default), `speaker`, `organizer`, `host`, `volunteer`. A speaker, organizer or host role without proof or confirmation counts as `attendee` for trust purposes; algo decides how much it's worth.
- `proof_links` (≤ 5 URLs: ticket, photo, talk page, slides). They don't raise trust on their own but are inputs for algo and admins.
- `going` that is never turned into `attended` simply expires at `ends_at + attendance_claim_days`, with no penalty.
- **Hackathons:** enrolling sets `going`. A submitted team result sets `attended` with a system confirmation for every participant (`participants = true`, §13). Members can attend a hackathon without enrolling, e.g. with an outside team.

## 26.2 Confirmations

- After `ends_at`, each member with `attended` gets one `attendance_confirm_digest`: "Who did you see at X?" with the other attendees as checkboxes. A tick is a `peer` confirmation.
- The creator of a member-created event (`events.created_by`) gets the same list; their ticks are `peer`, not `trusted`. Abuse through self-made events is left to `scan_community` (§18).
- Parser attendee or winner lists (§19) matched to members by GitHub login or Telegram username create `trusted` system confirmations and set the role from the list.

## 26.3 Member-created events

- `POST /api/v1/events {kind, title, starts_at, ends_at, timezone?, format, location?, website_url?, description_md?, area_keys?}`: published immediately (`origin=member`, `created_by`); timezone defaults to `UTC` when omitted.
- Duplicate check (same URL, or normalized title + overlapping dates ± 1 day): returns 409 `possible_duplicate` with candidates. `confirm_new=true` creates it anyway with `review_reasons += possible_duplicate`, and admins see it in the review queue (§11). Nothing waits on that.
- The creator's attendance gets role `organizer` or `host`.
- `formation_enabled` stays off for member-created events. Admins can switch it on.
- The creator can edit until `starts_at` and cancel anytime (attendees notified).
- **Workshops and talks hosted by a member** are member-created events with role `host`. Attendees who marked `attended` can rate the host (§13); those ratings show immediately as an aggregate.

## 26.4 Endpoints

| Method | Path | Who |
|---|---|---|
| PUT | `/api/v1/events/{id}/attendance` | member `{status, role?, proof_links?, note?}` |
| DELETE | `/api/v1/events/{id}/attendance` | member |
| GET | `/api/v1/events/{id}/attendees?status=&cursor=` | member (going/attended with role and trust) |
| POST | `/api/v1/events` | member (member-created event) |
| PATCH | `/api/v1/events/{id}` | creator before `starts_at`, admin |
| POST | `/api/v1/events/{id}/cancel` | creator, admin `{reason}` |
| GET | `/api/v1/me/attendances?status=` | member |

## Done when
- Attendance claims outside the time window are rejected with 409 `attendance_window_closed`.
- Hackathon results create `attended` + system confirmations exactly once per participant, and none for members marked `participants = false` or for auto-closed teams.
- Duplicate member events are caught before creation in the common cases (same URL, same title and date).
