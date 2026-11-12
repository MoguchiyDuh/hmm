# 21. Frontend (group: frontend)

## 21.1 Stack rules
- React + Vite + TS strict, TanStack Router (file routes) + TanStack Query, `openapi-typescript` types + `openapi-fetch` client generated from core's `openapi.json` (CI check that the generated client is up to date).
- Tailwind + shadcn/ui. Zod for forms. MSW mocks generated from OpenAPI examples for development before core endpoints exist.
- Build to static assets; served by Caddy. No SSR.
- Auth: cookie only; on 401 redirect to `/login`; on `onboarding_status != completed` redirect to `/onboarding`.
- Buttons and visibility come from `allowed_actions`. Errors are rendered from problem `code` via the i18n map, falling back to `title`.
- i18n: English and Russian bundles; language from `users.locale`, switchable in `/settings`, applied without reload. Missing keys fall back to English; CI fails on keys missing from either bundle. Server-generated text (algo reasons, hackathon data) is shown as-is.

## 21.2 Pages

**Public**
- `/login` — "Sign in with GitHub".

**Registration**
- `/onboarding` — wizard: sections → questions with live branching (TS evaluator, §9.3), autosave, progress, optional CV step (drag-and-drop, PDF/MD/Typst, extraction status), review step, submit. After submit: "Scores calculating…" state on the profile until the first result.

**Member**
- `/` home — **action items** on top (placements to confirm with countdowns, pending invites, join requests to answer, lead offers, reserve offers, help offers on my requests, mentorship check-ins due, confirmation digests, results due, reviews pending, lead opportunities), then the **feed** (§25.4) with kind and area filters, defaulting to my areas.
- `/events` — catalog with filters (§11: kind, formation, phase, area, format, mine), cards with kind, phase, dates, format, areas, members going, and for hackathons difficulty, enrolled count and projected teams. "Add event" opens the member-event form with the duplicate check (§26.3).
- `/events/:id` — details, **attendance panel** (going / attended with role and proof links, members going, confirm-attendees checklist after the event, host/speaker rating for member-hosted events), and for hackathons a **participation panel** that changes by phase:
  - `open`: enroll / edit preferred area / "I can lead if needed" checkbox / withdraw; "Lead a team" with lead-fit reason or why it's unavailable (pointing to the checkbox); live projection ("2 teams (6, 5), 0 on reserve"); formation countdown; list of cores with leads and "Request to join"; enrolled list.
  - `review`: my proposed team with reasons, a prominent confirm button and `confirm_by` countdown ("unconfirmed members are removed"), decline; pending lead offer with accept/decline; or my reserve rank.
  - `locked`: teams and rosters; reserve; reserve offer with countdown; late-lead claim when offered.
- `/projects` — browse (status, area, recruiting, event), "New project".
- `/projects/:id` — header (status, areas, event link, recruiting chips), goal, plan (milestones checklist), updates timeline, releases, roster with areas and trust badges. Standalone lead tools: edit, recruiting toggle with quick-pick, invites and join requests, start/pause/abandon/continue, release form with the contributions checklist, transfer/step down. For **hackathon teams**, the roster also shows states (proposed with countdown / confirmed) and volunteer badges, a "No lead yet, offered to X" banner while the role is empty, and a **lead console** by phase:
  - `forming`: core slots (`size / core_max`), quick-pick tabs **Enrolled** and **Others** (score bar, reasons, tags, "Add to core"), member search with "Invite", join requests (accept/reject), pending invites (withdraw), Ready toggle, unclaim.
  - `review`: proposed roster with per-member reasons and confirmation state, area coverage panel (hackathon areas vs team), remove → reserve, add from reserve, fill invite, swap with another team (pick a member from another team's roster; shows which of the 3 accepts are pending), merge proposals (accept/decline/propose), review countdown.
  - `locked`: vacancy panel with quick-pick from the reserve and autofill countdown.
  - common: edit name/description, set member areas, transfer lead (pick a confirmed member, pending state, cancel), step down, leave.
  - after end: result form (outcome, place, award, links, per-member area, retrospective), review form per teammate (1–10 + comment), 7-day review window countdown, "Continue as project".
- `/mentorship` — tabs: **Open requests** (in my mentor areas, with Help), **My requests** (offers with mentor score, reputation and past mentorships; accept), **My mentorships** (as mentor and as learner), **I can mentor** (offers per area: topics, capacity).
- `/mentorship/new` — help request form (area, topic, goal, format, optional direct mentor picked from suggested mentors), with the mentoring/study-pair preview.
- `/mentorships/:id` — both sides, area, kind, check-ins timeline with "Check in", idle countdown, close (learner: outcome + summary) / propose close (mentor), acknowledgment and endorsement (mentor), review form after close.
- `/users` — directory (search, area filter + min score, mentors in area, available only, sort by XP, activity, score, reputation).
- `/users/:id` — profile: header (avatar, GitHub link, contacts, DND badge, "Scores reviewed" badge), area score bars with reasons, XP, activity sparkline, reputation per context (member, lead, mentor, host) with counts, mentor offers with "Ask for help", GitHub summary (top languages, top repos), about, unified history (hackathons, projects, events, mentorships, solo entries) with trust badges and award reasons, reviews and endorsements, CV link when visible.
- `/me/activity` — CRUD for own solo entries.
- `/settings` — language (EN/RU), profile edit (markdown editor with preview), CV upload with extraction status, DND, Telegram link (code + deep link + QR), notification toggles per type and channel, log out everywhere.
- `/notifications` — inbox with inline actions.

**Admin** (`/admin`, visible if `is_admin`)
- Dashboard (§16 counts, alerts list).
- New members — review queue (§9.5): answers, CV, GitHub summary, calculated scores, discrepancy hints, override editor with evidence type, note, "Mark reviewed".
- Alerts — filter, open subject, resolve.
- Users — list, ban/unban, admin toggle, score overrides (calculated vs override vs effective, history), XP adjustments, reopen onboarding, recompute.
- Quiz builder — versions list, clone, draft editor: sections, questions (types, config), options (labels, area-hint sliders per area), visibility condition builder (visual AND/OR/NOT tree with operator pickers), drag-reorder, validate panel (errors/warnings), live preview with the same TS evaluator, publish.
- Areas — CRUD, archive.
- Events — review queue (reasons incl. member-event duplicates, source payload, duplicate candidates side by side, publish/reject/merge), edit form with lock indicators and unlock, `formation_enabled` toggle, revisions diff, source people lists with match status, cancel, create manual.
- Formation — per hackathon: phase, `T`/`V` override, `formation_at` override, form now / re-run, runs list (input, plan, planner, validation errors), lead candidates, assign lead, reserve reorder.
- Teams and projects — any project: add/remove/move member, set lead, disband, set status.
- To check — queue of flagged items: subject content, related members (ring graph view for cross-member flags), algo reason, the member's history; **Pardon** or **Punish** with penalty picker (exclude, XP penalty, revoke confirmations, restriction kind + until, ban) and note.
- Restrictions — active restrictions, lift.
- Reviews — reported reviews, remove.
- Settings — all tunables with descriptions.
- System — parser heartbeats, job stats/failures with retry, algo version.
- Audit log — filterable.

## 21.3 Deliverables
- `web/` with CI: typecheck, lint, build, generated-client freshness, i18n key completeness.
- Visibility evaluator passes `contracts/fixtures/visibility_cases.json`.
- MSW mock mode (`pnpm dev:mock`) to demo every page without core.
