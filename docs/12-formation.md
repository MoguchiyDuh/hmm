# 12. Core — Hackathon participation and team formation

This section applies to events with `formation_enabled` ("hackathons"). A team is a `projects` row with `event_id` set; "team status" here means `projects.team_status`. After the result, the team continues under §27.

All mutations in this section follow §3.4 (hackathon row lock) and the invariants in §4. `last_lead_action_at` is updated on every lead action.

## 12.1 Overview

```text
publish ─► OPEN   ──────────────────────► FORMING ─► REVIEW ─────────────────► LOCKED ──────────────────► CLOSED
           members enroll                  one algo   members must confirm,     reserve fills vacancies,   starts_at
           volunteers tick "can lead"      run packs  leads adjust,             late teams from reserve
           eligible members claim lead     all teams  silence = removed
           leads build cores
```

Why it works this way: teams are completed in one pass, after everyone interested is known. Leads still choose people through their core and their review edits, but the global packing decides how many teams exist. That's what prevents 3 teams of 2.

Nobody ends up on a team without saying so. Every placement made by the system or by a lead is a **proposal** with a `confirm_by` deadline. A member who doesn't confirm in time is removed (§12.6, §12.7).

## 12.2 Sizes and timing

Sizes (recomputed live during `open`, frozen at formation):

| Value | Rule |
|---|---|
| `T` target | `target_override ?? hackathon.team_size_max ?? team_size_default_max` |
| `V` viable | `max(team_size_min, ceil(T × viable_ratio))`, `viable_ratio` = 0.75 |
| `core_max` | `max(2, ceil(T × core_ratio))` incl. lead, `core_ratio` = 0.5 |

Timing (`deadline = registration_deadline ?? starts_at`):

| Moment | Rule |
|---|---|
| `formation_at` | `formation_at_override ?? deadline − formation_lead_hours (72 h)` |
| short notice | if that is earlier than `now + formation_min_notice_hours (24 h)` at publish → `now + 24 h`, capped at `deadline − review_min_hours (12 h)`; if even that is in the past → `now + 1 h` and review is shortened |
| early start | every claiming lead pressed **Ready** and enrollments + core members ≥ `V` → `formation_at = now + 1 h` (grace so others can react); admins can "form now" anytime in `open` |
| `review_ends_at` | `min(formation_at + review_hours (48 h), deadline − 1 h)` |
| imported date changes | recompute `formation_at` only while in `open`; notify enrollments if it moves by > 6 h |

## 12.3 Placement arithmetic (`hmm_contracts.formation`)

A pure function shared by core (live projection, plan validation, fallback) and algo (must hit the same counts):

```python
def placement_bounds(free: int, cores: list[int], v: int, t: int, max_size: int) -> Bounds:
    ...  # -> Bounds(teams: int, new_teams: int, placed: int, reserve: int, undersized_cores: int)
```

Semantics:
- `free` is the number of unplaced enrollments; `cores` are the current core sizes (each a team that must survive).
- Choose the number of new teams `n ≥ 0`. Each new team holds between `V` and `max_size`; each core can grow up to `max_size`.
- Pick `n` maximizing `placed`. On a tie, pick the smallest `n` (fewer, bigger teams). On a further tie, pick the distribution closest to `T`.
- Cores are filled toward `V` first, before any new team is opened.
- A core that still can't reach `V` counts as undersized, and formation proposes merging it (§12.5).

Examples (`T = max = 6`, `V = 5`, no cores): 6 → 1×6; 8 → 1×6 + 2 reserve; 10 → 2×5; 11 → 2×(6,5); 13 → 2×6 + 1 reserve; 15 → 3×5.

The hackathon page shows the live projection: "Projected: 2 teams (6, 5), 0 on reserve, 1 lead volunteer".

## 12.4 Enrollment phase

**Enroll.** `POST /events/{id}/enroll {preferred_area_key?, note?, willing_to_lead?}`.
- Allowed while the hackathon is `published` and `now < deadline`.
- In `open` phase the status becomes `active`. In `review`/`locked` it becomes `reserve` and the reserve is re-ranked.
- Re-enrolling after `withdrawn` or `lapsed` reactivates the same row under the same phase rule.
- 409 codes: `already_enrolled`, `already_in_team`, `enrollment_closed`.
- Edit preferred area, note and `willing_to_lead` anytime before `starts_at`. Withdraw anytime before lock while not on a team. On a team, leave first (§12.7).

**"I can lead if needed"** (`willing_to_lead`): a checkbox on enrollment, open to every member regardless of lead fit. It doesn't create a team or a core. It places the member higher in the lead order (§12.8), so formation, succession and late rounds pick volunteers before anyone else.

**Claim lead.** `POST /events/{id}/projects {name, description?}`.
- Phase must be `open`, or `locked` for a late claim (§12.7).
- The caller must have `lead_fit ≥ lead_fit_threshold`, not be on a team, and not be DND.
  - Fit too low → 403 `lead_fit_too_low` with the reason, and the UI suggests ticking "I can lead if needed" instead.
  - No fit computed yet → computed inline for the caller only (15 s timeout); otherwise 503 `lead_fit_pending` and the computation is enqueued.
- Effects: team `forming` with the caller as confirmed lead (`joined_via=claim`). The caller's enrollment is created or switched to `placed`. Their pending invites and join requests for this hackathon close. Recommendations refresh.
- Admin path: `POST /admin/events/{id}/projects {lead_user_id, name}` bypasses the threshold (audit + alert `forced_lead`, info).

**Build the core** (team `forming`, size < `core_max`):
- **Core invite** to any onboarded, non-DND, non-banned member who isn't on a team for this hackathon (enrolled or not). Pending core invites ≤ `core_max − size`. Accepting adds them as a confirmed member and creates or switches their enrollment to `placed`. Invites expire at `formation_at`.
- **Join request** from a member to a specific forming team (one pending per hackathon). The lead accepts if there's room (member joins confirmed, enrollment `placed`) or rejects (enrollment stays `active`). Requests close at `formation_at`.
- The lead sees all active enrollments with quick-pick suggestions. "Add to core" sends a core invite.
- **Ready** toggle per lead (`POST/DELETE /projects/{id}/ready`). It feeds the early start in §12.2.
- Core members may leave anytime in `open`: their enrollment goes back to `active`.
- The lead may transfer, step down or leave (§12.8), or **unclaim**. Unclaiming disbands the team, returns everyone's enrollment to `active` and closes pending invites and requests.

## 12.5 Formation

Job `run_formation(hackathon, kind)`, enqueued by `event_tick` at `formation_at` (`kind=main`) or for a late round (`kind=late`).

1. Lock the hackathon row and set phase `forming`. Close pending core invites, join requests and lead offers. A core still without a lead (§12.8) is dissolved: its members become unplaced `active` enrollments.
2. Build `FormationRequest` (§18.2):
   - hackathon snapshot, `T`/`V`/`max`, the bounds from §12.3;
   - cores (teams in `forming`, with members);
   - unplaced enrollments (`active`, or `reserve` for a late round) with member snapshots, preferred areas and `willing_to_lead`;
   - lead fits of unplaced members.
   Banned users are excluded. DND doesn't exclude people who enrolled themselves.
3. Call `algo.form_teams` (timeout 120 s).
4. Validate the plan against the hard constraints below.
   - Invalid plan or algo failure → retry once.
   - Still failing → the **fallback planner** in core runs. It's deterministic:
     - apply the §12.3 counts;
     - fill cores with members covering missing primary/required areas;
     - seed each new team with one volunteer, tier 2 before tier 3 (§12.8);
     - fill new teams round-robin by effective area score;
     - planned lead = top of the lead order.
   - The run records `planner=fallback`, and admins get alert `formation_failed`.
5. Apply in one transaction:
   - Cores get proposed members (`state=proposed`, `joined_via=formation`, `proposal_reason`, `confirm_by = review_ends_at`).
   - New teams are created with `origin=formation`, status `review` and `lead_user_id = null`. All of their members are proposed, and a lead offer (`source=formation`) goes to the planned lead.
   - Enrollments become `placed`, or `reserve` with a rank.
   - Merge proposals from the plan are stored as `pending`.
   - All `forming` teams become `review`. Phase becomes `review` and `review_ends_at` is set.
6. Notify:
   - placed members: `team_proposed` (confirm / decline, with the `confirm_by` deadline);
   - planned leads: `lead_offer` (accept / decline);
   - each claimed lead: `formation_ready`;
   - reserve: `reserve_placed`, with their rank.
   - Admin alert: `formation_undersized` for a core below `V`.

Edge cases:
- No enrollments and no cores → phase goes straight to `locked`.
- Cores but no free enrollments → cores go to `review` as they are, with undersized ones flagged.

**Hard constraints (validated by core):**
1. Every input person appears exactly once: in one team or in the reserve.
2. Cores are intact: every core member stays on their core team, and the core lead stays lead.
3. Every team is ≤ `max`. New teams are ≥ `V`. A core may stay < `V` only when §12.3 says it's unavoidable, and it then carries a merge proposal if a merge into another team fits within `max`.
4. `placed` and team count equal `placement_bounds()`.
5. Every new team has a planned lead from its own members, taken from the highest lead tier present in that team (§12.8).
6. Merge proposals only reference teams in this hackathon, and the merged size is ≤ `max`.

Soft goals are owned by algo (§18.3). Spreading volunteers comes first: one per new team when there are enough, tier 2 before tier 3. Then area coverage, the mentoring mix, preferred areas, spreading strong members, and lead fit.

## 12.6 Review

Until `review_ends_at`:

**Placed member** (`state=proposed`):
- `confirm` → `confirmed`. This is the only way to stay on the team.
- `decline {withdraw: bool}` → leaves the team. With `withdraw=false` their enrollment goes to `reserve`; with `true` it is `withdrawn`. The lead is notified with quick-pick suggestions for the free slot.
- No answer by `confirm_by` → removed at lock (§12.7).
- Reminders (`confirm_reminder`, web + Telegram) are sent `confirm_reminder_hours` (24, 6, 1) before `confirm_by`.

**Lead offer** (new teams): handled per §12.8. Accepting the lead role also confirms the placement. Declining it leaves the member as a proposed member who still has to confirm.

**Lead** of any team in `review`:
- Remove a member → their enrollment goes to `reserve`.
- Add from the reserve → `state=proposed`, `joined_via=reserve_add`, `confirm_by = max(review_ends_at, now + reserve_offer_hours (12 h))`, capped at `starts_at`. The member must confirm like any placement.
- Fill invite to someone who isn't enrolled → they must accept; their enrollment is created as `placed` and they join `confirmed`.
- Swap with another team: `POST /projects/{id}/swaps {from_user_id, to_user_id}`.
  - The other lead and both moved members must accept. Nobody moves until all three have accepted.
  - A decline or no answer by `review_ends_at` cancels the swap.
  - Leads can't be swapped; they transfer first (§12.8).
  - Moved members keep their state (a confirmed member stays confirmed).
- Merge: accept or decline a system merge proposal, or propose one (`POST /projects/{id}/merges {target_team_id}`).
  - Both leads must accept.
  - The target team's lead stays lead, and the absorbed team's lead becomes a confirmed member.
  - Absorbed members keep their state and `confirm_by`, are notified (`merge_result`), and may leave.
  - The absorbed team is `disbanded`.
- Quick-pick for open slots: reserve first, then others.
- Rename, assign member areas, transfer the lead role, step down (§12.8).
- Size is always ≤ `max`.

**Admin:** may re-run formation in `review` (`kind=main`). Confirmed members and cores stay fixed; proposed placements are re-planned with a fresh `confirm_by`. Admins never have to act on the routine path.

Leads of teams that are below `V` or have no lead get a reminder 12 h before `review_ends_at`.

## 12.7 Lock and locked phase

**At `review_ends_at`** (`event_tick`):
1. Every member still `proposed` with `confirm_by ≤ now` is removed (`left_reason=lapsed`). Their enrollment becomes `lapsed`, and they get `placement_lapsed`: "you didn't confirm; enroll again to join the reserve". Lead offers to them close.
2. Teams with no confirmed members are `disbanded` (`team_disbanded` to anyone left proposed, who return to the reserve).
3. A team still without a lead: its pending lead offer closes and the role is forced per §12.8.
4. Pending swaps, merges and invites close.
5. Teams below `team_size_min` get reserve offers (below), one open offer per missing seat.
6. Teams become `locked`, phase becomes `locked`, and leads, members and the reserve are notified (`team_locked`).

**Reserve offer** (used for top-ups, vacancies and lead-made reserve adds in `locked`):
- The best-ranked reserve candidate for the team (`recommend` over the reserve) is added as `proposed`, `joined_via=reserve_offer`, with `confirm_by = now + reserve_offer_hours (12 h)`, capped at `starts_at`.
- They get `reserve_offer` (confirm / decline), and the lead is told.
- Decline → back on the reserve. No answer → `lapsed`. Either way the next candidate gets an offer.
- Reserve empty → alert `vacancy_unfilled`.

**While `locked`** (until `starts_at`):
- New enrollments go to the reserve, which is re-ranked (`rank_reserve`).
- **Vacancy:** a member leaves (`POST /projects/{id}/leave`) → the lead is notified with quick-pick and `vacancy_since` is set. The lead fills it with a reserve add or a fill invite. If it's still unfilled after `dropout_autofill_hours` (24 h), a reserve offer goes out.
- **Lead leaving or stepping down:** §12.8.
- **Late round:** when the reserve ≥ `V`, before `deadline − review_min_hours`:
  - eligible reserve members get `lead_opportunity`;
  - a reserve member may claim (`origin=late_claim`) and build a core for up to `late_core_hours` (12 h) or until the core is full;
  - if nobody claims within `late_claim_grace_hours` (24 h), the round runs anyway, with leads taken from the lead order (volunteers first);
  - then `run_formation(kind=late)` runs over the reserve only, with a review of `late_review_hours` (24 h) and the same confirm rule.
- Cross-team moves after lock: admin only.

**At the registration deadline:**
- Pending invites and join requests expire. Reserve offers stay open until their `confirm_by`.
- Teams below `team_size_min` with no open reserve offers are disbanded and their members go to the reserve (alert `team_below_min`).

**At `starts_at`:**
- Members still `proposed` are removed (`lapsed`).
- `locked` teams become `active`, the phase becomes `closed`, and reserve enrollments become `expired`.
- Roster changes are admin-only from here. Lead transfer, stepping down and succession still work (§12.8).

## 12.8 Lead role

**Lead order.** Wherever the system has to pick a lead for a team, it ranks the team's members like this:

| Tier | Who |
|---|---|
| 1 | Claimed lead (claimed with fit ≥ threshold). Formation never moves or replaces them. |
| 2 | Volunteer (`willing_to_lead`) with lead fit ≥ threshold |
| 3 | Volunteer with lead fit below the threshold |
| 4 | Anyone else, as a last resort |

- Within a tier: higher lead fit first, then earlier enrollment.
- Candidates are the team's current members, confirmed or proposed.
- The threshold only gates self-claiming. Every other route to the lead role uses this order.

**Lead offers** (`lead_offers`): at most one pending per team.
- Sources:
  - `formation`: the planned lead of a new team;
  - `succession`: the role is empty;
  - `transfer`: chosen by the current lead.
- An offer expires after `lead_offer_hours` (12 h), capped at the current phase deadline:
  - `open` → `formation_at`;
  - `review` → `review_ends_at`;
  - `locked` → `starts_at`;
  - `active` → `ends_at`.
- Accept → the member becomes lead (`projects.lead_user_id`, `role=lead`) and their placement is confirmed.
- Decline or expiry of a `formation` or `succession` offer → the next candidate in the lead order. Members who declined are skipped for the rest of that succession.

**Forced assignment.** If the order runs out, or a phase deadline passes with the role still empty:
- in `open`: nothing is forced; the leaderless core is dissolved at formation (§12.5);
- in `review`: at lock (§12.7);
- in `locked`/`active`: immediately.

The role goes to the top of the lead order among confirmed members, ignoring earlier declines. They get `lead_assigned`, and admins get alert `forced_lead` (info). A forced lead can transfer or step down like any lead.

**Transfer.** `POST /projects/{id}/transfer-lead {user_id}`.
- Target: any confirmed member of the team, of any tier.
- Creates a `transfer` offer. The current lead keeps the role until the target accepts, then becomes a confirmed member.
- The lead can cancel the offer (`DELETE /lead-offers/{id}`). A decline or expiry changes nothing.
- Allowed in every non-terminal team status, including `active`.
- Target's fit below the threshold → alert `lead_below_threshold` (info).

**Step down / leave.**
- `POST /projects/{id}/lead/step-down` (stays a member) or `POST /projects/{id}/leave` (leaves the team) frees the role immediately and starts a succession. The person stepping down is excluded from it.
- In `open`, a lead alone in their core who leaves effectively unclaims.
- Leaving as lead in `locked` also opens a vacancy (§12.7).

**While a team has no lead:**
- Lead-only actions are unavailable. Invites, swaps and merges can't be started, and pending ones stay open.
- System timers keep running: confirmations, reserve offers and autofill.
- Admins may assign a lead (`PUT /admin/projects/{id}/lead`), but they never have to.

**Edge cases:**

| Case | Behavior |
|---|---|
| Lead banned | Treated as leaving (§6) |
| Lead turns on DND | No effect on the role |
| Claimed lead's fit later drops below the threshold | Keeps the role |
| New team has no volunteer | Planned lead comes from tier 4. They can decline, and the succession continues |
| Lead offer recipient lapses at lock | Offer closes; forced assignment among confirmed members |
| Volunteer unticks the checkbox | Pending offers stay; future orderings use the new value |
| Two-person team, lead leaves | The remaining member is the only candidate: offer, then forced |
| Lead leaves during `active` | Succession until `ends_at`, then forced, so someone can submit the result (§13) |
| Merge between two claimed leads | Target team's lead stays lead; transfer afterwards if both agree |
| Lead inactive but present | No automatic removal. Members may leave; admins may reassign |

## 12.9 Team state machine

```text
forming ──formation──► review ──lock──► locked ──starts_at──► active ──result──► finished | withdrawn
   │                     │                │
   └──unclaim/dissolve───┴─merge/empty/admin─┴──deadline below min──► disbanded
any non-terminal ──hackathon cancelled──► cancelled
```

Terminal: `finished`, `withdrawn`, `disbanded`, `cancelled`.

## 12.10 Timers (`event_tick`, every 5 min)

| Condition | Action |
|---|---|
| phase `open`, `now ≥ formation_at` | enqueue `run_formation(main)` |
| phase `open`, all leads ready, enrollments + cores ≥ `V` | `formation_at = now + 1 h`, notify |
| `formation_at − 24 h` | `formation_soon` to enrollments and leads |
| proposed member, `confirm_by − confirm_reminder_hours` | `confirm_reminder` |
| proposed member past `confirm_by` outside a review end | remove (`lapsed`), next reserve offer if the seat came from one |
| phase `review`, `review_ends_at − 12 h` | lead reminders (§12.6) |
| phase `review`, `now ≥ review_ends_at` | lock (§12.7) |
| lead offer past `expires_at` | next candidate or forced assignment (§12.8) |
| `locked`, vacancy older than `dropout_autofill_hours` | reserve offer |
| `locked`, reserve ≥ `V`, late window open | `lead_opportunity` (once per day); after `late_claim_grace_hours` → late round without a claim |
| late core window over or core full | enqueue `run_formation(late)` |
| registration deadline | expire invites/requests; disband teams below min |
| `starts_at` | remove remaining proposed, `active`, phase `closed`, reserve expired |
| `ends_at`, team `active`, no result | `result_due` to lead; after `results_due_days` → alert `result_overdue`, reminder every 3 days |
| invite `expires_at` | `expired`, notify both sides |

## 12.11 Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/events/{id}/formation` | member → phase, `T`/`V`/`core_max`, `formation_at`, `review_ends_at`, counts (incl. volunteers), live projection, my state, `allowed_actions` |
| POST | `/api/v1/events/{id}/enroll` | member `{preferred_area_key?, note?, willing_to_lead?}` |
| PATCH | `/api/v1/events/{id}/enroll` | enrolled member |
| DELETE | `/api/v1/events/{id}/enroll` | enrolled member not on a team |
| GET | `/api/v1/events/{id}/enrollments?cursor=` | member (name, avatar, preferred area, volunteer flag, top scores, team if any) |
| GET | `/api/v1/events/{id}/reserve` | member (ranked) |
| GET | `/api/v1/me/enrollments?status=` | member |
| GET | `/api/v1/events/{id}/lead-fit/me` | member `{fit, eligible, reason, computed_at}` |
| POST | `/api/v1/events/{id}/projects` | eligible member (claim) |
| GET | `/api/v1/projects/{id}` | member (pending invites/requests/offers visible to lead and admins only) |
| PATCH | `/api/v1/projects/{id}` | lead `{name, description}` |
| POST / DELETE | `/api/v1/projects/{id}/ready` | lead |
| POST | `/api/v1/projects/{id}/unclaim` | lead, `forming` only |
| POST | `/api/v1/projects/{id}/join-requests` | member `{note}` |
| GET | `/api/v1/projects/{id}/join-requests` | lead |
| DELETE | `/api/v1/join-requests/{id}` | requester |
| POST | `/api/v1/join-requests/{id}/accept` / `reject` | lead |
| POST | `/api/v1/projects/{id}/invites` | lead `{user_id, message?, area_key?}` (`core` in forming, `fill` in review/locked) |
| GET | `/api/v1/projects/{id}/invites` | lead, admin |
| DELETE | `/api/v1/invites/{id}` | lead (withdraw) |
| GET | `/api/v1/me/invites?status=` | member |
| POST | `/api/v1/invites/{id}/accept` / `decline` | invitee |
| POST | `/api/v1/projects/{id}/placement/confirm` | proposed member |
| POST | `/api/v1/projects/{id}/placement/decline` | proposed member `{withdraw}` |
| GET | `/api/v1/me/lead-offers?status=` | member |
| POST | `/api/v1/lead-offers/{id}/accept` / `decline` | offered member |
| DELETE | `/api/v1/lead-offers/{id}` | lead (cancel own transfer) |
| POST | `/api/v1/projects/{id}/transfer-lead` | lead `{user_id}` |
| POST | `/api/v1/projects/{id}/lead/step-down` | lead |
| POST | `/api/v1/projects/{id}/members` | lead `{user_id, area_key?}` from reserve (review/locked) |
| DELETE | `/api/v1/projects/{id}/members/{user_id}` | lead (review) |
| PATCH | `/api/v1/projects/{id}/members/{user_id}` | lead `{area_key}` |
| POST | `/api/v1/projects/{id}/swaps` | lead `{from_user_id, to_user_id}` |
| POST | `/api/v1/swaps/{id}/accept` / `decline` | other lead, either moved member |
| POST | `/api/v1/projects/{id}/merges` | lead `{target_team_id}` |
| POST | `/api/v1/merges/{id}/accept` / `decline` | either lead |
| POST | `/api/v1/projects/{id}/leave` | member or lead |
| GET | `/api/v1/me/projects?status=` | member |
| PATCH | `/api/v1/admin/events/{id}/formation` | admin `{target_override, formation_at_override}` |
| POST | `/api/v1/admin/events/{id}/formation/run` | admin (form now in `open`, re-run in `review`) |
| GET | `/api/v1/admin/events/{id}/formation/runs` | admin (input, output, planner, validation errors) |
| GET | `/api/v1/admin/events/{id}/lead-candidates` | admin |
| POST | `/api/v1/admin/events/{id}/projects` | admin (assign lead) |
| PUT | `/api/v1/admin/events/{id}/reserve/order` | admin |
| POST | `/api/v1/admin/projects/{id}/members` | admin |
| DELETE | `/api/v1/admin/projects/{id}/members/{user_id}` | admin |
| POST | `/api/v1/admin/projects/{id}/move` | admin `{user_id, to_team_id}` |
| PUT | `/api/v1/admin/projects/{id}/lead` | admin |
| POST | `/api/v1/admin/projects/{id}/disband` | admin `{reason}` |
| POST | `/api/v1/admin/projects/{id}/status` | admin `{status, reason}` |

## Done when
- Property tests: for random inputs (up to 60 people, 0–5 cores, varied `T`/`V`, 0–N volunteers), every applied plan satisfies the hard constraints, and `placement_bounds()` matches brute force for `n ≤ 20`.
- The fallback planner alone produces valid plans for the same inputs.
- Concurrency: parallel accepts, adds and swaps never exceed `max` or double-book a member.
- Nobody is ever `confirmed` on a team without an explicit action of their own (claim, accepted invite or request, confirm, accepted offer), except a forced lead who was already confirmed.
- Every team that reaches `locked` or `active` has a lead.
- Time-travel tests cover every row of §12.10.
