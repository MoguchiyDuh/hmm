# 7. Profiles, GitHub, CV, availability

## 7.1 Profile fields

- Editable by the owner: `display_name`, `about_md`, `contacts`, `timezone`, `locale`.
- Any edit enqueues `recompute_member` (debounced).

## 7.2 GitHub sync

- Job `github_sync(user)`, using REST and GraphQL with an app token (`GITHUB_TOKEN`) for rate limits.
- Fetches: profile; up to 100 most recently pushed public non-fork repos (name, description, topics, language, stars, `pushed_at`); language bytes aggregated; followers; stars total; contributions in the last year (GraphQL `contributionsCollection`).
- Success: store the snapshot, enqueue `recompute_member`.
- Failure: keep the previous snapshot, set `status = error`, retry with backoff.
- Schedule: daily for every user; on login if the snapshot is older than 24 h; manual refresh at most once per `github_manual_refresh_min_interval_minutes` (60), otherwise 429 `refresh_too_soon`.

## 7.3 CV

- One file per user. Accepted: PDF (`application/pdf`, checked by magic bytes), Markdown (`.md`) and Typst (`.typ`), both checked by UTF-8 decoding.
- Max `cv_max_bytes` (5 MB) → otherwise 413 `file_too_large`. Another type → 415 `unsupported_file_type`.
- Stored in object storage at `cv/<user_id>/<uuid>`. Replacing deletes the old object.
- Job `cv_extract`: PDF → text via pypdf; Markdown and Typst are stored as-is; text capped at 100k characters; then `recompute_member`.
- Uploaded as an optional onboarding step (§9) or in settings.
- Visible to every member. The upload UI warns: "Your CV is visible to all members. Remove personal data you don't want to share (phone, address, birth date)."
- Served through a presigned URL valid for 5 minutes.

## 7.4 Availability (DND)

- `PUT /api/v1/me/availability {dnd_until: null | timestamp | "indefinite"}`.
- While active, the member:
  - is excluded from quick-pick, lead-fit candidates and mentor matching;
  - cannot be invited or sent a direct help request (409 `user_dnd`).
- Pending invites stay pending. The member's own enrolling and accepting still work, and an enrolled DND member is still placed by formation.

## 7.5 Visibility

- **Public profile** (every member): display name, avatar, GitHub login and link, about, contacts, GitHub summary, effective area scores, XP, activity, reputation per context (`member`, `lead`, `mentor`, `host`) with counts, mentor offers, the unified fact history (hackathons with role, area, outcome and place; projects with releases; events with role; mentorships as mentor or learner; solo entries) with trust badges, revealed reviews and endorsements. Items that are to check or excluded are hidden.
- **Owner and admins only:** score history, CV text status, notification settings, Telegram link.
- **Admins only:** flags.

## 7.6 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/users?q=&area=&min_score=&available=&mentor=&sort=&cursor=` | member | Directory; `sort` = `xp`, `activity`, `score`, `reputation` or `name` |
| GET | `/api/v1/users/{id}` | member | Public profile |
| PATCH | `/api/v1/me/profile` | user | Profile fields |
| PUT | `/api/v1/me/availability` | member | `{dnd_until}` |
| POST | `/api/v1/me/github/refresh` | member | 202, enqueues sync |
| PUT | `/api/v1/me/cv` | user | Multipart upload → 202 |
| DELETE | `/api/v1/me/cv` | user | 204 |
| GET | `/api/v1/users/{id}/cv` | member | → `{filename, url, text_status}` |
| GET | `/api/v1/users/{id}/facts?type=&cursor=` | member | Unified history (§25.5) |
| GET | `/api/v1/me/notification-settings` | user | Per-type web/Telegram toggles |
| PUT | `/api/v1/me/notification-settings` | user | Update toggles |

## 7.7 Done when

- A GitHub outage leaves profiles intact and retries.
- A CV of the wrong type or size is rejected with 415 or 413; PDF text is extracted asynchronously; a failed extraction is visible to the owner.
- A DND user is absent from quick-pick and cannot be invited.
