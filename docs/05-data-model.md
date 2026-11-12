# 5. Data model

Core owns all tables. Types simplified; every table has `created_at`, and mutable tables `updated_at`.

## 5.1 Identity

```text
users
  id uuid pk
  github_id bigint unique not null
  github_login text not null
  display_name text not null
  avatar_url text
  about_md text not null default ''        -- max 5000 chars
  contacts jsonb not null default '{}'     -- {telegram, email, site, ...} free-form, max 10 keys
  timezone text                            -- IANA
  locale text not null default 'en'        -- en|ru, web UI only
  is_admin bool not null default false
  banned_at timestamptz, ban_reason text
  onboarding_status text not null          -- not_started|in_progress|completed|reopened
  dnd_until timestamptz                    -- null = off; 'infinity' = indefinitely
  mentor_capacity int null                 -- max active mentorships as mentor, all areas; null = mentor_capacity_default
  last_seen_at timestamptz

sessions
  token_hash bytea pk, user_id fk, expires_at, last_used_at, user_agent, ip

telegram_links
  user_id pk fk, telegram_user_id bigint unique, chat_id bigint, username text, linked_at, broken_at
telegram_link_codes
  code text pk, user_id fk, expires_at, used_at

notification_settings
  user_id fk, type text, web bool, telegram bool       -- pk(user_id, type); absence = defaults
```

## 5.2 Profile data

```text
github_snapshots
  user_id pk fk, fetched_at, status ok|error, error text,
  profile jsonb, repos jsonb, languages jsonb,  -- languages: {lang: bytes}
  stats jsonb                                  -- followers, public_repos, contributions_last_year, stars_total
  manual_refresh_at timestamptz

cv_files
  user_id pk fk, object_key text, filename text, mime text, size_bytes int,
  uploaded_at, text text, text_status pending|ok|failed, text_error text
```

## 5.3 Areas and quiz

```text
areas
  id uuid pk, key text unique (slug, immutable), name text, description text,
  sort int, archived_at

quizzes
  id uuid pk, version int unique, title text,
  status draft|published|archived, published_at, created_by fk
  -- exactly one published: partial unique index on (status) where status='published'

questions
  id uuid pk, quiz_id fk, section text, key text, type text, text text, help text,
  required bool, position int, visible_if jsonb null, config jsonb
  unique(quiz_id, key)

question_options
  id uuid pk, question_id fk, key text, label text, position int,
  area_hints jsonb  -- {area_key: weight 0..1}; hints for algo, not a formula
  unique(question_id, key)

onboarding_submissions
  id uuid pk, user_id fk, quiz_id fk, answers jsonb,
  status draft|submitted, submitted_at
  -- partial unique (user_id) where status='draft'
```

## 5.4 Scores, XP, activity

```text
area_scores
  user_id fk, area_id fk, calculated numeric(4,2), calc_reason text,
  override numeric(4,2) null, override_by fk null, override_reason text,
  override_evidence interview|github_review|cv_review|other null, override_at
  pk(user_id, area_id)  -- check 0<=calculated<=10, 0<=override<=10

member_reviews
  user_id pk fk, status pending|reviewed, opened_at, reviewed_by fk null, reviewed_at,
  admin_note text, discrepancies jsonb      -- from score_onboarding

area_score_events
  id, user_id, area_id, kind calculated|override_set|override_removed,
  old_effective, new_effective, reason text, algo_version text, actor_id null

member_metrics
  user_id pk, xp numeric, activity numeric, algo_version, input_hash text, computed_at

activity_entries                            -- solo log: always trust 'self' (§25)
  id uuid pk, user_id fk, type learning|course|talk|writeup|oss|other,
  title text, description_md text, links jsonb, area_id fk null, occurred_on date,
  deleted_at

fact_awards
  fact_type attendance|project_member|mentorship_side|activity_entry, fact_id uuid, user_id fk,
  xp numeric, activity numeric, reason text, algo_version, computed_at
  pk(fact_type, fact_id, user_id)

xp_adjustments
  id, user_id, amount numeric, reason text not null, admin_id fk, flag_id fk null

flags
  id, user_id, subject_type attendance|project_member|mentorship|confirmation|activity_entry|review|profile|ring,
  subject_id uuid, related_user_ids uuid[],          -- other members involved (rings)
  reason text, severity low|medium|high, status to_check|pardoned|punished,
  algo_version, resolved_by, resolved_at, resolution_note,
  penalty jsonb null                                 -- {exclude, xp_penalty, revoke_confirmations, restrictions[], ban}
  -- partial unique (subject_type, subject_id) where status='to_check'

member_restrictions
  id, user_id fk, kind confirming|mentoring|creating_events|creating_projects,
  until timestamptz null, reason text, flag_id fk null, created_by fk, lifted_at
```

## 5.5 Events

```text
events
  id uuid pk, slug text unique, kind hackathon|contest|conference|meetup|workshop|course|other,
  formation_enabled bool,                       -- default true for hackathon|contest
  title, summary, description_md,
  organizer text, website_url, registration_url,
  starts_at, ends_at, registration_opens_at null, registration_deadline null, timezone,
  format online|offline|hybrid, location jsonb {country, city, address},
  language text[], eligibility text,            -- informational only, never checked (§4.4)
  themes text[],
  team_size_min int null, team_size_max int null,   -- check 1<=min<=max<=20; used when formation_enabled
  prize jsonb {amount, currency, description},
  difficulty numeric(3,1) null, difficulty_rationale text,
  status draft|published|cancelled|archived,
  review_reasons text[],                        -- low_confidence|possible_duplicate|unmapped_areas|manual
  origin import|manual|member, created_by fk null, confidence numeric(3,2), unmapped_areas text[]

event_areas
  event_id fk, area_id fk, importance primary|required|nice, headcount int null
  pk(event_id, area_id)

event_sources
  id, event_id fk, source_id text unique, urls text[], payload jsonb,
  payload_hash text, extractor_version text, first_seen_at, last_seen_at

event_source_people                            -- attendee/winner lists from the parser (§19)
  id, event_id fk, source_id text, github_login text null, telegram_username text null,
  display_name text, role attendee|speaker|organizer|winner, place int null,
  matched_user_id fk null
  -- unique (source_id, coalesce(github_login, telegram_username, display_name))
  -- winner maps to attendance role attendee; its place verifies team results (§13)

event_field_locks
  event_id fk, field text, locked_by fk, locked_at   -- pk(event_id, field)

event_revisions
  id, event_id, origin import|admin|member|system, actor_id null, diff jsonb, ignored_fields text[]

event_attendances
  id, event_id fk, user_id fk, status going|attended|cancelled, role attendee|speaker|organizer|host|volunteer,
  proof_links jsonb, note text, trust self|peer|trusted, status_at
  -- unique (event_id, user_id)
```

Phase is **derived**, not stored:

```text
upcoming            now < registration_opens_at
registration_open   registration_opens_at <= now < registration_deadline (deadline defaults to starts_at)
registration_closed registration_deadline <= now < starts_at
ongoing             starts_at <= now < ends_at
ended               now >= ends_at
```

For events with `formation_enabled`, enrollment is allowed in any phase except `forming` before the registration deadline of a `published` event. The participation phase (`open → forming → review → locked → closed`) is stored separately in `event_formations` (§5.6, §12).

## 5.6 Formation, teams and projects

```text
event_formations
  event_id pk fk,
  phase open|forming|review|locked|closed,
  target_override int null, formation_at_override timestamptz null,   -- admin
  formation_at timestamptz, review_ends_at timestamptz null,
  rounds int not null default 0, last_run_id fk null

enrollments
  id, event_id fk, user_id fk, preferred_area_id fk null, note text (≤ 1000),
  willing_to_lead bool not null default false,
  status active|placed|reserve|withdrawn|lapsed|expired,
  source self|invite, reserve_rank int null, status_at
  -- partial unique (event_id, user_id) where status in ('active','placed','reserve')

projects                                    -- standalone projects and hackathon teams (§27)
  id, event_id fk null, name text, description_md text, goal text, links jsonb,
  visibility public|members, recruiting jsonb null {areas: [area_key], note},
  lead_user_id fk null,                    -- null while a lead offer or succession is open (§12.8, §27.6)
  status idea|active|shipped|paused|abandoned|disbanded|cancelled,
  team_status forming|review|locked|active|finished|withdrawn|disbanded|cancelled null,   -- hackathon teams only
  origin standalone|claim|formation|late_claim|admin,
  ready_at timestamptz null,               -- lead pressed "ready for formation"
  review_ends_at timestamptz null,         -- team-level: copied from the event for the main round, own value for late teams
  formed_in_run_id fk null, locked_at,
  seat_open_since timestamptz null,        -- oldest unfilled open seat (§12.7)
  seats_closed_at timestamptz null,        -- lead marked the team complete: no autofill
  last_lead_action_at, last_progress_at    -- idle detection (§27.4)

project_areas
  project_id fk, area_id fk               -- pk(project_id, area_id)

project_members
  id, project_id fk, event_id fk (denormalized), user_id fk,
  role lead|member, area_id fk null, state proposed|confirmed, trust self|peer|trusted,
  joined_via create|invite|claim|core_invite|join_request|formation|fill_invite|reserve_add|reserve_offer|swap|merge|admin,
  proposal_reason text null, confirm_by timestamptz null,      -- set while state='proposed'
  confirm_kind placement|reconfirm|presence null,              -- set while state='proposed' (§12.5, §12.7)
  is_team_row bool not null,                                   -- denormalized: project is a hackathon team
  joined_at, confirmed_at, left_at,
  left_reason declined|lapsed|removed|left|swapped|merged|disbanded|banned|admin null
  -- partial unique (event_id, user_id) where left_at is null and is_team_row
  -- partial unique (project_id) where role='lead' and left_at is null

invites
  id, project_id fk, event_id fk, user_id fk, invited_by fk null,   -- null = system
  kind core|fill|project, message text, area_id fk null,
  status pending|accepted|declined|withdrawn|expired|closed, closed_reason, expires_at, responded_at
  -- partial unique (project_id, user_id) where status='pending'

join_requests
  id, project_id fk, event_id fk, user_id fk, note text,
  status pending|accepted|rejected|withdrawn|closed, responded_at
  -- partial unique (event_id, user_id) where status='pending'

lead_offers
  id, project_id fk, event_id fk, user_id fk, source formation|succession|transfer,
  offered_by fk null,                      -- the lead for transfers, null = system
  tier smallint, reason text,
  status pending|accepted|declined|expired|cancelled|closed, expires_at, responded_at
  -- partial unique (project_id) where status='pending'

swap_requests
  id, event_id fk, from_team_id fk, from_user_id fk, to_team_id fk, to_user_id fk, requested_by fk,
  to_lead_accepted_at, from_user_accepted_at, to_user_accepted_at,
  status pending|accepted|declined|expired|closed

merge_proposals
  id, event_id fk, run_id fk null, absorbed_team_id fk, target_team_id fk, proposed_by system|lead,
  absorbed_accepted_at, target_accepted_at,
  status pending|accepted|declined|expired|closed

formation_runs
  id, event_id fk, round int, kind main|late, status pending|running|applied|failed,
  planner algo|fallback, input jsonb, output jsonb, validation_errors jsonb,
  algo_version, created_at, applied_at
```

## 5.7 Results, releases, reviews

```text
project_results                             -- hackathon team results (§13)
  project_id pk, outcome finished|withdrawn, place int null, award text,
  place_status unverified|verified|rejected null,   -- null when no place/award claimed
  place_verified_by fk null, place_verified_at,     -- verified_by null + verified = parser winner match
  repo_url, demo_url, presentation_url, retrospective_md text,
  member_areas jsonb {user_id: area_key},
  participants jsonb {user_id: bool},               -- took part; false = no system confirmation
  auto_closed bool not null default false,          -- no result by results_close_days
  submitted_by fk null, submitted_at, edited_at

project_releases                            -- standalone releases (§27.5); hackathon results also create one
  id, project_id fk, title, notes_md, repo_url, demo_url, writeup_url, created_by fk, created_at

release_contributions
  release_id fk, user_id fk, contributed bool, area_id fk null    -- pk(release_id, user_id)

project_milestones
  id, project_id fk, title, description, due_on date null, position int, done_at, done_by fk null

project_updates
  id, project_id fk, author_id fk, body_md text, links jsonb, created_at, deleted_at

review_windows
  id, subject_type project|mentorship, subject_id uuid, opens_at, closes_at, revealed_at null

reviews
  id, window_id fk null,                    -- null for instant host/speaker ratings
  subject_type project|mentorship|event, subject_id uuid,
  reviewer_id fk, reviewee_id fk,
  context member|lead|mentor|host|speaker,
  rating smallint null (1..10),             -- null = endorsement (mentor → learner)
  comment text (max 2000), revealed_at null, removed_at, removed_by, removed_reason
  unique nulls not distinct (subject_type, subject_id, window_id, reviewer_id, reviewee_id),
  check reviewer_id <> reviewee_id

review_reports
  id, review_id fk, reporter_id fk, reason text, status open|resolved
```

## 5.8 Algo outputs

```text
lead_fits
  event_id fk, user_id fk, fit numeric(3,2), reason text, algo_version, computed_at
  pk(event_id, user_id)

recommendation_sets
  id, subject_type project|help_request, subject_id uuid, input_hash text, status pending|ready|failed, error text,
  enrolled jsonb, others jsonb, algo_version, created_at, completed_at

algo_cache
  namespace text, key text, value bytea, expires_at   -- pk(namespace, key); algo's persistent cache
```

## 5.9 Notifications, admin, ops

```text
notifications
  id, user_id fk, type text, payload jsonb, dedupe_key text unique null, read_at

notification_deliveries
  notification_id fk, channel telegram, status pending|sent|failed|skipped,
  attempts int, external_id text, last_error text, next_attempt_at
  pk(notification_id, channel)

admin_alerts
  id, type text, severity info|warning|critical, subject_type, subject_id,
  payload jsonb, dedupe_key text, status open|resolved, resolved_by, resolved_at, resolution_note
  -- partial unique (dedupe_key) where status='open'

audit_log
  id, actor_id null, actor_kind user|admin|system|service, action text,
  entity_type, entity_id, before jsonb, after jsonb, reason text

settings
  key text pk, value jsonb, updated_by, updated_at

parser_heartbeats
  id, run_id text, started_at, finished_at, stats jsonb, errors jsonb
```

## 5.10 Mentorship

```text
mentor_offers
  user_id fk, area_id fk, topics text, active bool   -- pk(user_id, area_id); capacity lives on users

help_requests
  id, learner_id fk, area_id fk, topic text, goal text, format online|offline|any,
  target_mentor_id fk null, status open|direct|matched|cancelled|expired, expires_at
  -- partial unique (learner_id, area_id) where status in ('open','direct')

help_offers
  id, request_id fk, mentor_id fk, message text,
  status pending|accepted|declined|withdrawn|closed
  -- unique (request_id, mentor_id)

mentorships
  id, request_id fk unique, mentor_id fk, learner_id fk, area_id fk,
  kind mentoring|study_pair,                    -- fixed at acceptance (§28.4)
  mentor_score_at_start numeric, learner_score_at_start numeric,
  status active|stalled|closing|closed|cancelled, started_at, last_checkin_at,
  close_proposed_at, closed_at, outcome reached|partial|not_reached null, summary text,
  mentor_acknowledged_at, endorsement text,
  mentor_trust self|peer|trusted, learner_trust self|peer|trusted

mentorship_checkins
  id, mentorship_id fk, author_id fk, note text, occurred_on date, minutes int null
  -- unique (mentorship_id, author_id, occurred_on)
```

## 5.11 Confirmations and feed

```text
confirmations
  id, fact_type attendance|project_member|mentorship_side, fact_id uuid, subject_user_id fk,
  confirmer_id fk null,                         -- null = system
  source peer|lead|organizer|host|learner|mentor|system|admin, ignored bool,   -- ignored: restricted or to-check confirmer
  created_at, withdrawn_at
  -- partial unique nulls not distinct (fact_type, fact_id, confirmer_id) where withdrawn_at is null

feed_items
  id, kind text, subject_type text, subject_id uuid, actor_id fk null, area_ids uuid[],
  payload jsonb, created_at, hidden_at
```
