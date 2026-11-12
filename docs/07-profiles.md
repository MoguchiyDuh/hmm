# 7. Core — Profiles, GitHub, CV, availability

## Behavior
- **Profile fields** editable by owner: `display_name`, `about_md`, `contacts`, `timezone`, `locale`. Any edit enqueues `recompute_member` (debounced).
- **GitHub sync** (`github_sync` job): REST/GraphQL with an app token (`GITHUB_TOKEN`) for rate limits. Fetch profile, up to 100 most recently pushed public non-fork repos (name, description, topics, language, stars, pushed_at), language bytes aggregated, followers, stars total, contributions last year (GraphQL `contributionsCollection`). On success store snapshot, enqueue `recompute_member`. On failure keep previous snapshot, set `status=error`, retry with backoff. Schedule: daily for all users, on login if older than 24 h, manual refresh max 1/hour (429 `refresh_too_soon`).
- **CV**: one file per user. Accept `application/pdf`, `.md` (`text/markdown`), `.typ`; max 5 MB; validated by magic bytes for PDF, UTF-8 decoding for md/typ. Stored in MinIO `cv/<user_id>/<uuid>`. `cv_extract` job: PDF → pypdf text; md/typ → stored as-is; text capped at 100k chars; then `recompute_member`. Replacing deletes old object. Upload is offered as an optional onboarding step (§9) and in settings. Visible to every member; the upload UI warns: "Your CV is visible to all members. Remove personal data you don't want to share (phone, address, birth date)." Served via short-lived presigned URL (5 min).
- **DND**: `PUT /me/availability {dnd_until: null | timestamp | "indefinite"}`. While active: excluded from quick-pick, lead-fit candidates and mentor matching, cannot be invited or sent a direct help request (409 `user_dnd`). Pending invites stay pending. Own enrolling/accepting still allowed, and an enrolled DND member is still placed by formation.
- **Public profile** (visible to all members): display name, avatar, GitHub login + link, about, contacts, GitHub summary, effective area scores, XP, activity, reputation per context (member, lead, mentor, host) with counts, mentor offers, unified history of facts (hackathons with role/area/outcome/place, projects with releases, events with role, mentorships as mentor or learner, solo entries) with trust badges, revealed reviews and endorsements. Items that are to check or excluded are hidden.
- **Private to owner + admins**: score history, CV text status, notification settings, Telegram link, flags (admins only).

## Endpoints

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/api/v1/users?q=&area=&min_score=&available=&mentor=&sort=xp\|activity\|score\|reputation\|name&cursor=` | member | directory |
| GET | `/api/v1/users/{id}` | member | public profile |
| PATCH | `/api/v1/me/profile` | user | edit profile fields |
| PUT | `/api/v1/me/availability` | member | DND |
| POST | `/api/v1/me/github/refresh` | member | 202, enqueue sync |
| PUT | `/api/v1/me/cv` | member | multipart upload → 202 |
| DELETE | `/api/v1/me/cv` | member | remove |
| GET | `/api/v1/users/{id}/cv` | member | `{filename, url, text_status}` |
| GET | `/api/v1/users/{id}/facts?type=&cursor=` | member | unified history (§25.5) |
| GET | `/api/v1/me/notification-settings` | user | per-type web/telegram toggles |
| PUT | `/api/v1/me/notification-settings` | user | update |

## Done when
- GitHub outage leaves profiles intact and retries.
- Invalid CV type/size rejected with 422; PDF text extracted asynchronously; failure visible to owner.
- DND user is absent from quick-pick and un-invitable.
