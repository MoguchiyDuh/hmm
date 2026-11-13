# 27. Projects

## 27.1 Model

A project is a group effort: lead, members, goal, plan, updates, results. Hackathon teams are projects too (`event_id` set, `team_status` not null, formed by §12). Everything in this section applies to both unless it says "standalone".

```text
idea ──start──► active ──ship──► shipped ──continue──► active
  │               │  ▲
  │               ▼  │
  │             paused
  └──────────────┴──abandon──► abandoned
```

| Status | Meaning |
|---|---|
| `idea` | Created, maybe recruiting, nothing done yet. Gives no points. |
| `active` | Work is happening. Updates and milestones earn activity. |
| `shipped` | At least one release exists. Releases earn XP for confirmed contributors. Any new release moves an `active` project back to `shipped`. |
| `paused` | No lead, or paused by the lead or by idle detection. No points accrue. |
| `abandoned` | Closed without a result. Past activity stays; no XP. |
| `disbanded` | Hackathon team disbanded, or any project disbanded by an admin. |
| `cancelled` | Hackathon team of a cancelled event. |

Hackathon teams follow their `team_status` (§12.9):

| `team_status` | Project `status` |
|---|---|
| `forming`, `review` | `idea` |
| `locked`, `active` | `active` |
| `finished`, `withdrawn` | Set by the result (§13.1): `shipped` or `abandoned`; an auto-closed team is `abandoned` |
| `disbanded` | `disbanded` |
| `cancelled` | `cancelled` |

After the result, the lead can **continue** the team as a regular project (`active` again); `team_status` stays as it was.

## 27.2 Create and edit (standalone)

- `POST /api/v1/projects {name, description_md, goal, area_keys[], links[], event_id?, visibility}` → `idea`. The creator is the confirmed lead. Under a `creating_projects` restriction → 403 `restricted`.
- `visibility`: `public` (all members, default) or `members` (non-members receive `view: "limited"`, only name and areas).
- Limits: `project_size_max` (20) members (409 `project_full`); a member may lead at most `projects_led_max` (5) non-terminal standalone projects (409 `projects_led_limit`).
- `event_id` on a standalone project means "built for or at this event" without formation (e.g. a game jam the team joined outside the platform).
- Lead edits everything. Members edit milestones and post updates.

## 27.3 Members

- **Invite** (lead → any onboarded, non-DND, non-banned member): the invitee must accept. Reuses `invites` with `kind = project`; expires after `invite_ttl_days` (7).
- **Join request** (member → project with `recruiting` set): the lead accepts or rejects. Reuses `join_requests`.
- **Recruiting:** `recruiting {areas: [area_key], note}` or null. While set, the project appears in the feed (`project_recruiting`) and the lead gets quick-pick (`recommend` with the project as context, §14).
- **Leave:** any member, anytime. A leaving lead triggers succession (§27.6).
- **Remove:** the lead removes a member with an optional reason. A removed member's earlier confirmed contributions stay.
- Hackathon teams follow §12 for membership until `starts_at`. From `starts_at` until the result is submitted (or the team auto-closes, §13.1), adding and removing members is admin-only; members may still leave. This section applies only after that.

## 27.4 Plan and updates

- **Milestones:** `{title, description?, due_on?, position}`; any member can mark one `done` (`done_by`, `done_at`). The first done milestone or update moves an `idea` to `active` automatically; the lead can also `start` it explicitly.
- **Updates:** markdown posts (≤ 5000 chars, ≤ 10 links) by any member. Each update is an activity input for its author.
- **Idle** (`idea` and `active` projects): no update and no milestone change for `project_idle_days` (30) → the lead gets `project_idle`; after 2 × that → `paused` automatically, and members get `project_paused`.
- A project paused by idle detection resumes on any member action. A project the lead paused resumes only through `start`.

## 27.5 Shipping and contributions

- `POST /api/v1/projects/{id}/releases {title, notes_md, repo_url?, demo_url?, writeup_url?}` by the lead. At least one link is required. The first release moves the project to `shipped`.
- With the release, the lead submits **contributions**: for each current member (and members who left within the last 30 days), `contributed: bool` and an optional area. `contributed=true` is a `trusted` confirmation of that member's fact (§25.2). Members confirm the lead's fact as `peer` through the contributions digest.
- A lead-only project is a `self` fact for the lead; it still earns activity, and algo may use public repo evidence.
- Shipping opens a review window (§13) among members who were on the project at ship time for at least `project_review_min_days` (3).
- A later release opens a new window only if ≥ 30 days have passed since the project's last review window opened.

## 27.6 Lead role (standalone)

- **Transfer:** the lead offers the role to a member, who must accept (`lead_offers`, `source = transfer`). Works as in §12.8, but the offer expires after 7 days.
- **Lead leaves or steps down:** their own pending transfer offer is cancelled, and offers go to members in order of confirmed contributions, then tenure, each lasting `project_lead_offer_days` (3) (`project_lead_offer`). If nobody accepts, the project becomes `paused` without a lead, members get `project_lead_claimable`, and any project member can claim the lead role while it's paused (first come).
- Hackathon teams keep §12.8 until the result is submitted or the team auto-closes (§13.1), then follow this section.

## 27.7 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/projects?status=&area=&recruiting=&event=&member=&cursor=` | member | Projects |
| POST | `/api/v1/projects` | member | §27.2 |
| GET | `/api/v1/me/projects?status=` | member | Own standalone projects and hackathon teams |
| GET | `/api/v1/projects/{id}` | member | Respects `visibility` |
| PATCH | `/api/v1/projects/{id}` | lead | `{name, description_md, goal, area_keys, links, visibility, recruiting}` |
| POST | `/api/v1/projects/{id}/start` | lead | – |
| POST | `/api/v1/projects/{id}/pause` | lead | – |
| POST | `/api/v1/projects/{id}/abandon` | lead | – |
| POST | `/api/v1/projects/{id}/continue` | lead | – |
| POST | `/api/v1/projects/{id}/invites` | lead | Same endpoint as §12.11; `kind = project` for standalone projects |
| POST | `/api/v1/projects/{id}/join-requests` | member | `{note}`; project must have `recruiting` set |
| DELETE | `/api/v1/projects/{id}/members/{user_id}` | lead | `{reason?}` |
| POST | `/api/v1/projects/{id}/leave` | project member | – |
| POST | `/api/v1/projects/{id}/claim-lead` | project member of a `paused` leaderless project | – |
| GET | `/api/v1/projects/{id}/milestones` | member | Milestones |
| POST | `/api/v1/projects/{id}/milestones` | project member | `{title, description?, due_on?, position}` |
| PATCH | `/api/v1/projects/{id}/milestones/{mid}` | project member | Milestone fields, `done` |
| DELETE | `/api/v1/projects/{id}/milestones/{mid}` | project member | 204 |
| GET | `/api/v1/projects/{id}/updates` | member | Updates |
| POST | `/api/v1/projects/{id}/updates` | project member | `{body_md, links?}` |
| POST | `/api/v1/projects/{id}/releases` | lead | `{title, notes_md, repo_url?, demo_url?, writeup_url?, contributions: [{user_id, contributed, area_key?}]}` |
| GET | `/api/v1/projects/{id}/releases` | member | Releases |
| GET | `/api/v1/projects/{id}/recommendations` | lead | §14.7 |

Transfer, lead offers, ready, swaps, merges and the other team-only endpoints are in §12.11.

## 27.8 Done when

- A project never earns XP for a member without a confirmation or system evidence.
- Hackathon teams pass through `team_status` and the project lifecycle without double-counting the result.
- Idle projects pause themselves; a project paused by idle detection resumes on any member action.
