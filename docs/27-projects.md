# 27. Core — Projects

## 27.1 Model

A project is a group effort: lead, members, goal, plan, updates, results. Hackathon teams are projects too (`event_id` set, `team_status` not null, formed by §12). Everything in this section applies to both unless it says "standalone".

```text
idea ──start──► active ──ship──► shipped ──continue──► active
  │               │  ▲
  │               ▼  │
  │             paused
  └──────────────┴──abandon──► abandoned
```

- `idea`: created, maybe recruiting, nothing done yet. Gives no points.
- `active`: work is happening. Updates and milestones earn activity.
- `shipped`: at least one result (release) exists. Releases earn XP for confirmed contributors.
- `paused`: no lead, or the lead paused it. No points accrue.
- `abandoned`: closed without a result. Past activity stays; no XP.
- Hackathon teams start in `active` when the team locks. When the result is submitted (§13), the project becomes `shipped` if the result has a repo or demo link, otherwise `abandoned`; an auto-closed team becomes `abandoned`. The lead can then **continue** it as a regular project (`active` again); `team_status` stays `finished`.

## 27.2 Create and edit (standalone)

- `POST /api/v1/projects {name, description_md, goal, area_keys[], links[], event_id?, visibility}` → `idea`. The creator is the confirmed lead.
- `visibility`: `public` (all members, default) or `members` (non-members receive `view: "limited"`, only name and areas).
- Limits: `project_size_max` (20) members; a member may lead at most `projects_led_max` (5) non-terminal standalone projects.
- `event_id` on a standalone project means "built for or at this event" without formation (e.g. a game jam the team joined outside the platform).
- Lead edits everything. Members edit milestones and post updates.

## 27.3 Members

- **Invite** (lead → anyone onboarded, non-DND, non-banned): the invitee must accept. Reuses `invites` with `kind=project`, TTL `invite_ttl_days`.
- **Join request** (member → project with `recruiting` set): the lead accepts or rejects. Reuses `join_requests`.
- **Recruiting:** `recruiting {areas: [area_key], note}` or null. While set, the project appears in the feed (`project_recruiting`) and the lead gets quick-pick (`recommend` with the project as context, §14).
- **Leave:** any member, anytime. A leaving lead triggers succession (§27.6).
- **Remove:** the lead removes a member with an optional reason. A removed member's earlier confirmed contributions stay.
- Hackathon teams follow §12 for membership until `starts_at`. From `starts_at` until the result is submitted (or the team auto-closes, §13), adding and removing members is admin-only; members may still leave. This section applies only after that.

## 27.4 Plan and updates

- **Milestones:** `{title, description?, due_on?, position}`; any member can mark one `done` (`done_by`, `done_at`). The first done milestone or update moves an `idea` to `active` automatically; the lead can also `start` it explicitly.
- **Updates:** markdown posts (≤ 5000 chars, ≤ 10 links) by any member. Each update is an activity input for its author.
- **Idle:** no update and no milestone change for `project_idle_days` (30) → lead reminder; after 2 × that → `paused` automatically, and members are notified. Any member action reactivates it.

## 27.5 Shipping and contributions

- `POST /api/v1/projects/{id}/releases {title, notes_md, repo_url?, demo_url?, writeup_url?}` by the lead. At least one link is required. The first release moves the project to `shipped`.
- With the release, the lead submits **contributions**: for each current member (and members who left within the last 30 days), `contributed: bool` and an optional area. `contributed=true` is a `trusted` confirmation of that member's fact (§25.2). Members confirm the lead's fact as `peer` through the contributions digest.
- A lead-only project is a `self` fact for the lead; it still earns activity, and algo may use public repo evidence.
- Shipping opens a review window (§13) among members who were on the project at ship time for at least `project_review_min_days` (3).
- Each later release opens a new window only if ≥ 30 days have passed since the last one, so releases can't be used to farm reviews.

## 27.6 Lead role (standalone)

- **Transfer:** the lead offers the role to a member; the member must accept (`lead_offers`, `source=transfer`, 7 days). Same as §12.8.
- **Lead leaves or steps down:** offers go to members in order of confirmed contributions, then tenure, each lasting `project_lead_offer_days` (3). If nobody accepts, the project becomes `paused` without a lead, and any member can claim the lead role while it's paused (first come).
- Hackathon teams keep §12.8 until the result is submitted or the team auto-closes (§13), then follow this section.

## 27.7 Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/projects?status=&area=&recruiting=&event=&member=&cursor=` | member |
| POST | `/api/v1/projects` | member |
| GET | `/api/v1/me/projects?status=` | member (standalone and hackathon teams) |
| GET | `/api/v1/projects/{id}` | member (respecting `visibility`) |
| PATCH | `/api/v1/projects/{id}` | lead `{name, description_md, goal, area_keys, links, visibility, recruiting}` |
| POST | `/api/v1/projects/{id}/start` / `pause` / `abandon` / `continue` | lead |
| POST | `/api/v1/projects/{id}/invites` | lead (existing, `kind=project` for standalone) |
| POST | `/api/v1/projects/{id}/join-requests` | member |
| DELETE | `/api/v1/projects/{id}/members/{user_id}` | lead |
| POST | `/api/v1/projects/{id}/leave` | member |
| POST | `/api/v1/projects/{id}/claim-lead` | member of a `paused` leaderless project |
| GET / POST | `/api/v1/projects/{id}/milestones` | member / project member |
| PATCH / DELETE | `/api/v1/projects/{id}/milestones/{mid}` | project member |
| GET / POST | `/api/v1/projects/{id}/updates` | member / project member |
| POST | `/api/v1/projects/{id}/releases` | lead `{..., contributions: [{user_id, contributed, area_key?}]}` |
| GET | `/api/v1/projects/{id}/releases` | member |
| GET | `/api/v1/projects/{id}/recommendations` | lead (§14) |

Transfer, lead offers, ready, swaps, merges and the rest of the team-only endpoints are in §12.11.

## Done when
- A project never earns XP for a member without a confirmation or system evidence.
- Hackathon teams pass through `team_status` and the project lifecycle without double-counting the result.
- Idle projects pause themselves; any member action resumes them.
