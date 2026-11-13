# 12. Hackathon participation and team formation

This section applies to events with `formation_enabled` ("hackathons"). A team is a `projects` row with `team_status` set; "team status" here means `projects.team_status`. After the result, the team continues under §27.

All mutations in this section follow §3.4 (hackathon row lock) and the invariants in §4.4. `last_lead_action_at` is updated on every lead action.

## 12.1 Overview

```text
publish ─► OPEN   ──────────────────────► FORMING ─► REVIEW ─────────────────► LOCKED ──────────────────► CLOSED
           members enroll                  one algo   everyone must confirm,    reserve fills open seats,  starts_at
           volunteers tick "can lead"      run packs  leads adjust,             late teams from reserve,
           eligible members claim lead     all teams  silence = removed         presence check
           leads build cores
```

- Teams are completed in one pass, after everyone interested is known. Leads choose people through their core and their review edits; the global packing decides how many teams exist.
- Nobody ends up on a team without saying so, and nobody stays on one while silent:
  - every placement made by the system or by a lead is a **proposal** with a `confirm_by` deadline;
  - everyone already on a team re-confirms during review;
  - everyone confirms again shortly before the start (presence check);
  - a member who doesn't confirm in time is removed (§12.6, §12.7).

## 12.2 Sizes and timing

Sizes are recomputed live during `open` and frozen at formation:

| Value | Rule |
|---|---|
| `max` | `hackathon.team_size_max ?? team_size_default_max` |
| `min` | `hackathon.team_size_min ?? team_size_default_min` |
| `T` target | `target_override ?? max`. `target_override` must be within [`min`, `max`], otherwise 422. |
| `V` viable | `max(min, ceil(T × viable_ratio))`, `viable_ratio` (0.75) |
| `core_max` | `min(max, max(2, ceil(T × core_ratio)))`, lead included, `core_ratio` (0.5) |

Deadlines:
- `reg_deadline = registration_deadline ?? starts_at`: the organizer's own deadline.
- `team_deadline = reg_deadline − registration_buffer_hours` (48): teams are locked by then, so they have time to register with the organizer.
- `formation_at = formation_at_override ?? team_deadline − formation_lead_hours` (72).
- `review_ends_at = min(formation_at + review_hours (48), team_deadline)`.

**Short notice.** At publish, if `formation_at` is earlier than `now + formation_min_notice_hours` (24):
1. `formation_at = min(now + formation_min_notice_hours, team_deadline − review_min_hours (12))`.
2. If that is in the past, the buffer shrinks: `team_deadline = reg_deadline − 1 h`, and step 1 is applied again.
3. If it is still in the past, `formation_at = now + 1 h`, and review is shortened by the `review_ends_at` rule.

**Early start.** When the hackathon has at least one claimed lead, every claimed lead has pressed **Ready**, and live enrollments (`active` + `placed`) ≥ `V`: `formation_at = now + 1 h`, and everyone enrolled is notified. The hour lets others react. Once moved earlier, `formation_at` never moves back, even if a lead un-readies or a new lead claims. Admins can "form now" at any time in `open`.

**Date changes** (import or admin edit):
- `open`: `formation_at` is recomputed; enrolled members and leads get `formation_rescheduled` if it moves by more than 6 h.
- `review`: `review_ends_at` is re-clamped to the new `team_deadline` (it only moves earlier); if that has already passed, the teams lock now.
- `locked`: timers follow the new dates; a timer whose new moment has passed fires on the next tick.
- In `review` and `locked`, admins get alert `event_dates_changed` (info).

## 12.3 Placement arithmetic (`hmm_contracts.formation`)

A pure function shared by core (live projection, plan validation, fallback) and algo (which must hit the same counts):

```python
def placement_bounds(free: int, cores: list[int], v: int, t: int, max_size: int) -> Bounds:
    ...  # -> Bounds(teams: int, new_teams: int, placed: int, reserve: int, undersized_cores: int)
```

Definition. Cores are filled toward `V` before any new team is opened; this takes precedence over placing more people.

```text
need  = Σ max(0, V − c)                      over cores c
if free ≤ need:
    new_teams = 0, placed = free, reserve = 0
    free people go to the cores closest to V first
    undersized_cores = cores still below V
else:
    rest  = free − need
    extra = Σ (max_size − max(c, V))         room left in cores once they reach V
    for every n ≥ 0 with n × V ≤ rest:
        placed(n) = need + min(rest, extra + n × max_size)
    new_teams = the n with the largest placed(n); on a tie, the smallest n
    placed = placed(new_teams), reserve = free − placed, undersized_cores = 0
teams = len(cores) + new_teams
```

- `free` is the number of unplaced enrollments; `cores` are the current core sizes (each a team that must survive).
- New teams hold between `V` and `max_size`; cores grow up to `max_size`. Among plans with these counts, sizes closest to `T` are preferred (a soft goal, §18.3).
- An undersized core gets a merge proposal from formation if a merge fits within `max_size` (§12.5).

Examples with `T = max = 6`, `V = 5` and no cores:

| Free | Result |
|---|---|
| 6 | 1 × 6 |
| 8 | 1 × 6, 2 on reserve |
| 10 | 2 × 5 |
| 11 | 2 teams (6, 5) |
| 13 | 2 × 6, 1 on reserve |
| 15 | 3 × 5 |

With two cores of 2 and 10 free: `need` = 6, `rest` = 4 < `V`, so 2 × 6 and 2 on reserve.

The hackathon page shows the live projection: "Projected: 2 teams (6, 5), 0 on reserve, 1 lead volunteer".

## 12.4 Enrollment phase

**Enroll.** `POST /api/v1/events/{id}/enroll {preferred_area_key?, note?, willing_to_lead?}`.
- Allowed while the hackathon is `published` and `now < reg_deadline`.
- The resulting status depends on the phase:

| Phase | Result |
|---|---|
| `open` | `active` |
| `forming` | 409 `formation_in_progress` (retry shortly) |
| `review`, `locked` | `reserve`; the reserve is re-ranked |

- Re-enrolling after `withdrawn` or `lapsed` reactivates the same row under the same phase rule.
- Other 409 codes: `already_enrolled`, `already_in_team`, `enrollment_closed`.
- Preferred area, note and `willing_to_lead` can be edited until `starts_at`.
- Withdraw at any time before `starts_at` while not on a team, including from the reserve. A member on a team leaves first (§12.7). Withdrawing also cancels the event attendance if it is still `going` (§26.1).
- No eligibility or schedule-overlap checks (§4.4).

**"I can lead if needed"** (`willing_to_lead`): a checkbox on enrollment, open to every member whatever their lead fit. It doesn't create a team or a core. It moves the member up the lead order (§12.8), so formation, succession and late rounds pick volunteers before anyone else.

**Claim lead.** `POST /api/v1/events/{id}/projects {name, description_md?}`.
- Phase `open`, or `locked` for a late claim (§12.7).
- The caller must have `lead_fit ≥ lead_fit_threshold` (0.6), not be on a team, and not be on DND.
  - Fit too low → 403 `lead_fit_too_low` with the reason; the UI suggests ticking "I can lead if needed" instead.
  - No fit computed yet → 503 `lead_fit_pending`, and `compute_lead_fits_for_member` is enqueued. The UI polls `GET /api/v1/events/{id}/lead-fit/me` and retries.
- Effects: a team in `forming` with the caller as confirmed lead (`joined_via = claim`). The caller's enrollment is created or switched to `placed`. Their pending invites and join requests for this hackathon close. Recommendations refresh.
- Admin path: `POST /api/v1/admin/events/{id}/projects {lead_user_id, name}` bypasses the threshold (audited, alert `forced_lead`, info).

**Build the core** (team `forming`, size < `core_max`):
- **Core invite** to any onboarded, non-DND, non-banned member who isn't on a team for this hackathon, enrolled or not.
  - Pending core invites ≤ `core_max − size`.
  - Accepting adds the invitee as a confirmed member and creates or switches their enrollment to `placed`.
  - Expires at `min(now + invite_ttl_days (7), formation_at)`.
- **Join request** from a member with an `active` enrollment to one forming team (one pending per hackathon). Without an active enrollment → 409 `not_enrolled`.
  - The lead accepts if there's room (the member joins confirmed, enrollment `placed`) or rejects (enrollment stays `active`).
  - Requests close at `formation_at`.
- The lead sees all active enrollments with quick-pick suggestions. "Add to core" sends a core invite.
- **Ready** toggle per lead (`POST` / `DELETE /api/v1/projects/{id}/ready`), used by the early start (§12.2).
- Core members may leave at any time in `open`; their enrollment goes back to `active`.
- The lead may transfer, step down or leave (§12.8), or **unclaim**. Unclaiming disbands the team, returns everyone's enrollment to `active`, and closes pending invites and requests.

## 12.5 Formation

Job `run_formation(hackathon, kind)`: `kind = main` is enqueued by `event_tick` at `formation_at`; `kind = late` runs a late round (§12.7). The hackathon row is locked only in steps 1 and 5; the algo call runs without it.

1. **Start** (short transaction): lock the hackathon row and set phase `forming`. While `forming`, every participation mutation on this hackathon → 409 `formation_in_progress`.
   - Main round: close pending core invites, join requests and lead offers. A core still without a lead (§12.8) is dissolved: its members become unplaced `active` enrollments.
   - Late round: the same, for late cores only.
2. **Build `FormationRequest`** (§18.2):
   - hackathon snapshot, `T`, `V`, `max` and the §12.3 bounds;
   - cores: teams in `forming`, with members;
   - unplaced enrollments (`active` for the main round, `reserve` for a late round) with member snapshots, preferred areas and `willing_to_lead`;
   - lead fits of unplaced members.

   Banned users are excluded. DND doesn't exclude people who enrolled themselves.
3. **Plan:** call `algo.form_teams` (timeout 120 s). With more than 100 unplaced enrollments, skip algo and use the fallback planner directly.
4. **Validate** the plan against the hard constraints below.
   - Invalid plan or algo failure → retry once.
   - Still failing → core's deterministic **fallback planner** runs:
     1. apply the §12.3 counts;
     2. fill cores with members covering missing `primary` and `required` areas;
     3. seed each new team with one volunteer, tier 2 before tier 3 (§12.8);
     4. fill new teams round-robin by effective area score;
     5. planned lead = top of the lead order.
   - The run records `planner = fallback`, and admins get alert `formation_failed`.
5. **Apply** (one transaction, hackathon row locked again):
   - Existing core members, the claimed lead included, are set back to `state = proposed`, `confirm_kind = reconfirm`, `confirm_by = review_ends_at`. For a lead, any lead action also counts as confirming.
   - Cores get proposed members: `state = proposed`, `confirm_kind = placement`, `joined_via = formation`, `proposal_reason`, `confirm_by = review_ends_at`.
   - New teams are created with `origin = formation`, team status `review` and `lead_user_id = null`. All their members are proposed, and a lead offer (`source = formation`) goes to the planned lead.
   - Enrollments become `placed`, or `reserve` with a rank.
   - Merge proposals from the plan are stored as `pending`.
   - All `forming` teams become `review` with `projects.review_ends_at` set.
   - Main round: phase becomes `review` and the event's `review_ends_at` is set.
   - Late round: phase returns to `locked`; each late team has its own `review_ends_at` (§12.7).
6. **Notify:**

| Recipient | Notification |
|---|---|
| Placed members | `team_proposed` (confirm / decline, with `confirm_by`) |
| Existing core members | `team_reconfirm` (confirm / leave, with `confirm_by`) |
| Planned leads | `lead_offer` (accept / decline) |
| Each claimed lead | `formation_ready` (includes the confirm action) |
| Reserve | `reserve_placed`, with their rank |
| Admins | alert `formation_undersized` for each core below `V` |

If the job fails for good, the phase returns to where it was (`open` with `formation_at = now + 1 h`, or `locked`), and admins get alert `formation_failed`.

Edge cases:
- No enrollments and no cores → the phase goes straight to `locked`.
- Cores but no free enrollments → cores go to `review` as they are (members still re-confirm), and undersized ones are flagged.

**Hard constraints** (validated by core):
1. Every input person appears exactly once: in one team or in the reserve.
2. Cores are intact: every core member stays on their core team, and the core lead stays lead.
3. Every team is ≤ `max`. New teams are ≥ `V`. A core may stay below `V` only when §12.3 says it's unavoidable, and it then carries a merge proposal if a merge into another team fits within `max`.
4. `placed` and the team count equal `placement_bounds()`.
5. Every new team has a planned lead from its own members, taken from the highest lead tier present in that team (§12.8).
6. Merge proposals only reference teams in this hackathon, and the merged size is ≤ `max`.

Soft goals are owned by algo (§18.3): volunteers spread first (one per new team when there are enough, tier 2 before tier 3), then area coverage, the mentoring mix, preferred areas, spreading strong members, and lead fit.

## 12.6 Review

Until the team's `review_ends_at`:

**Proposed member** (placement or re-confirmation):
- `confirm` → `confirmed`. This is the only way to stay on the team.
- `decline {withdraw: bool}` → leaves the team ("leave" in notifications means the same action).
  - `withdraw = false`: the enrollment goes to `reserve`; `true`: it becomes `withdrawn`.
  - The lead is notified with quick-pick suggestions for the free slot.
  - A claimed lead who declines gives up the role (§12.8).
- No answer by `confirm_by` → removed at lock (§12.7).
- Reminders (`confirm_reminder`, web and Telegram) go out `confirm_reminder_hours` (24, 6, 1) before `confirm_by`.

**Lead offer** (new teams): handled per §12.8. Accepting the lead role also confirms the placement. Declining it leaves the member as a proposed member who still has to confirm.

**Lead** of any team in `review`:
- **Remove** a member → their enrollment goes to `reserve`.
- **Add from the reserve** → `state = proposed`, `joined_via = reserve_add`, `confirm_by = min(max(review_ends_at, now + reserve_offer_hours (12)), reg_deadline)`. The member confirms like any placement.
- **Fill invite** to someone who isn't enrolled → they must accept; their enrollment is created as `placed` and they join `confirmed`. Expires at `min(now + invite_ttl_days, reg_deadline)`.
- **Swap** with another team: `POST /api/v1/projects/{id}/swaps {from_user_id, to_user_id}`.
  - The other lead and both moved members must accept. Nobody moves until all three have accepted.
  - A decline, or no answer by `review_ends_at`, cancels the swap.
  - Leads can't be swapped (409 `lead_not_swappable`); they transfer first (§12.8).
  - Moved members keep their state (a confirmed member stays confirmed).
- **Merge:** accept or decline a system merge proposal, or propose one (`POST /api/v1/projects/{id}/merges {target_team_id}`).
  - Both leads must accept.
  - The target team's lead stays lead; the absorbed team's lead becomes a confirmed member.
  - Absorbed members keep their state and `confirm_by`, are notified (`merge_result`), and may leave.
  - The absorbed team becomes `disbanded`.
- Quick-pick for open slots: reserve first, then others.
- Rename, assign member areas, transfer the lead role, step down (§12.8).
- Size is always ≤ `max`.

A member who left a team (declined, lapsed, left or was removed) is never offered or added back to that same team from the reserve; a lead's add → 409 `invalid_state`. Other teams can still take them.

**Admin:** may re-run formation in `review` (`kind = main`). Confirmed members, cores and accepted leads stay fixed. Still-proposed placements are re-planned with a fresh `confirm_by`. Pending swaps, merges and `formation` lead offers close; a new team left without members is disbanded. Admins never have to act on the routine path.

Leads of teams below `V` get `review_ending` 12 h before `review_ends_at`.

## 12.7 Lock and locked phase

**At a team's `review_ends_at`** (`event_tick`; main-round teams share the event's value, late teams have their own):
1. Every member still `proposed` with `confirm_by ≤ now` is removed (`left_reason = lapsed`). Their enrollment becomes `lapsed`, they get `placement_lapsed` ("you didn't confirm; enroll again to join the reserve"), and lead offers to them close.
2. Teams with no confirmed members become `disbanded`. Anyone left proposed gets `team_disbanded` and returns to the reserve.
3. A team still without a lead: its pending lead offer closes and the role is forced per §12.8.
4. Pending swaps, merges and invites close.
5. The team becomes `locked`. If it is below `T`, its open seats start their head start now (below).
6. Main round only: the phase becomes `locked`, and leads, members and the reserve get `team_locked`.

**Open seats** (from lock until `reg_deadline`):
- `open_seats = T − size`, where `size` counts every current member, proposed ones (including pending reserve offers) too.
- A seat opens when a member declines, lapses, leaves or is removed. `seat_open_since` (the oldest unfilled seat) is set, and the lead gets `seat_opened` with quick-pick.
- **Lead head start** of `seat_fill_hours` (12) from `seat_open_since`. During it the lead may:
  - add from the reserve (proposed, `joined_via = reserve_add`);
  - send a fill invite;
  - **close seats** (`POST /api/v1/projects/{id}/seats/close`: "we're complete", no autofill; `DELETE` reopens). Closing needs size ≥ `min` (else 409 `seats_close_below_min`); a team that drops below `min` reopens automatically.
- **Autofill:** after the head start, each open seat of a team whose seats aren't closed gets a reserve offer.
  - Order across teams: below `min` first, then below `V`, then the rest; ties go to the oldest `seat_open_since`.
  - A reserve member holds at most one pending offer.
- **Reserve offer:** the best-ranked reserve candidate for the team (`recommend` over the reserve) is added as `proposed`, `joined_via = reserve_offer`, `confirm_kind = placement`, `confirm_by = min(now + reserve_offer_hours (12), reg_deadline)`.
  - They get `reserve_offer` (confirm / decline). The lead gets `seat_autofilled` once they confirm.
  - Decline → back on the reserve. No answer → `lapsed`. Either way the next candidate gets the offer.
- Reserve empty while a team is below `min` → alert `team_below_min`.

**While `locked`** (until `starts_at`):
- New enrollments go to the reserve (until `reg_deadline`), which is re-ranked (`rank_reserve`).
- **Leaving:** a member leaves (`POST /api/v1/projects/{id}/leave`) → a seat opens.
- **Lead leaving or stepping down:** §12.8.
- **Late round.** Conditions: every open seat is filled, closed or has a pending offer; reserve members without a pending offer ≥ `V`; and `now < reg_deadline − review_min_hours`. Then:
  1. Reserve members with lead fit ≥ `lead_fit_threshold` who aren't on DND get `lead_opportunity` (at most once per day).
  2. Only such a member, after receiving `lead_opportunity`, may late-claim (`origin = late_claim`, §12.4) and build a core as in §12.4 for up to `late_core_hours` (12) or until the core is full.
  3. If nobody claims within `late_claim_grace_hours` (24), the round runs anyway, with leads taken from the lead order (volunteers first).
  4. `run_formation(kind = late)` runs over the reserve once every late core's window is over or the core is full (§12.5). The phase is `forming` during the run and returns to `locked` after it.
  5. Late teams get their own `review_ends_at = min(now + late_review_hours (24), reg_deadline − 1 h)`, follow the same confirm rules, and lock individually as above.
- Cross-team moves after lock: admins only.

**At `reg_deadline`:**
- Seat filling stops: no new reserve offers, reserve adds or fill invites. Pending invites and join requests expire. Pending reserve offers stay open until their `confirm_by`.
- Teams below `min` with no pending reserve offers become `disbanded`, and their members go to the reserve (alert `team_below_min`). A team that drops below `min` later is kept, and admins get `team_below_min`.

**Presence check** (at `starts_at − presence_check_hours` (48)):
- Every confirmed member of a `locked` team, lead included, is set to `proposed` with `confirm_kind = presence` and `confirm_by = starts_at − presence_confirm_hours` (12). They get `presence_check` ("Still in for X?", confirm / leave) plus the usual reminders. For a lead, any lead action also counts as confirming.
- Members who confirmed anything on this team within the last `presence_check_hours` are skipped (late teams, short notice).
- No answer → removed (`lapsed`) at `confirm_by`. Their seat opens and is refilled only while `now < reg_deadline`.
- A lead removed this way triggers succession (§12.8). If nobody on the team is confirmed yet, the forced assignment waits for the presence deadline.

**At `starts_at`:**
- Members still `proposed` are removed (`lapsed`).
- A `locked` team with no members left becomes `disbanded`; the other `locked` teams become `active`.
- The phase becomes `closed`, and reserve enrollments become `expired`.
- Until the result is submitted (§13), adding and removing members is admin-only; members may still leave. Lead transfer, stepping down and succession still work (§12.8).

## 12.8 Lead role

**Lead order.** Wherever the system has to pick a lead for a team, it ranks the team's members like this:

| Tier | Who |
|---|---|
| 1 | Claimed lead (claimed with fit ≥ threshold). Formation never moves or replaces them. |
| 2 | Volunteer (`willing_to_lead`) with lead fit ≥ threshold |
| 3 | Volunteer with lead fit below the threshold |
| 4 | Anyone else, as a last resort |

- Within a tier: higher lead fit first, then earlier enrollment. A member with no computed lead fit counts as fit 0.
- Candidates are the team's current members, confirmed or proposed.
- The threshold only gates self-claiming. Every other route to the lead role uses this order.

**Lead offers** (`lead_offers`): at most one pending per team.
- Sources:
  - `formation`: the planned lead of a new team;
  - `succession`: the role is empty;
  - `transfer`: chosen by the current lead.
- An offer expires after `lead_offer_hours` (12), capped at the current phase deadline:

| Team phase | Cap |
|---|---|
| `open` | `formation_at` |
| `review` | the team's `review_ends_at` |
| `locked` | `starts_at` |
| `active` | `ends_at` |

- Accept → the member becomes lead (`projects.lead_user_id`, `role = lead`), and their placement is confirmed.
- Decline or expiry of a `formation` or `succession` offer → the next candidate in the lead order. Members who declined are skipped for the rest of that succession.

**Forced assignment.** If the order runs out, or a phase deadline passes with the role still empty:

| Phase | When forced |
|---|---|
| `open` | Never; a leaderless core is dissolved at formation (§12.5) |
| `review` | At lock (§12.7) |
| `locked`, `active` | Immediately; during a presence check, at its deadline |

The role goes to the top of the lead order among confirmed members, ignoring earlier declines. They get `lead_assigned`, and admins get alert `forced_lead` (info). A forced lead can transfer or step down like any lead.

**Transfer.** `POST /api/v1/projects/{id}/transfer-lead {user_id}`.
- Target: any confirmed member of the team, of any tier.
- Creates a `transfer` offer. The current lead keeps the role until the target accepts, then becomes a confirmed member.
- The lead can cancel the offer (`DELETE /api/v1/lead-offers/{id}`). A decline (the lead gets `lead_offer_declined`) or expiry changes nothing.
- Allowed in every non-terminal team status, including `active`.
- Target's fit below the threshold → alert `lead_below_threshold` (info).

**Step down / leave.**
- `POST /api/v1/projects/{id}/lead/step-down` (stays a member) or `POST /api/v1/projects/{id}/leave` (leaves the team) frees the role immediately and starts a succession. The person stepping down is excluded from it, and their own pending transfer offer is cancelled.
- In `open`, a lead alone in their core who leaves effectively unclaims.
- Leaving as lead in `locked` also opens a seat (§12.7).

**While a team has no lead:**
- Lead-only actions are unavailable. Invites, swaps and merges can't be started; pending ones stay open.
- System timers keep running: confirmations, reserve offers and autofill.
- Admins may assign a lead (`PUT /api/v1/admin/projects/{id}/lead`), but never have to.

**Edge cases:**

| Case | Behavior |
|---|---|
| Lead banned | Treated as leaving (§6.4) |
| Lead turns on DND | No effect on the role |
| Claimed lead's fit later drops below the threshold | Keeps the role |
| New team has no volunteer | Planned lead comes from tier 4. They can decline, and the succession continues |
| Lead offer recipient lapses at lock | Offer closes; forced assignment among confirmed members |
| Volunteer unticks the checkbox | Pending offers stay; future orderings use the new value |
| Two-person team, lead leaves | The remaining member is the only candidate: offer, then forced |
| Lead leaves during `active` | Succession until `ends_at`, then forced. Any confirmed member can submit the result after `results_due_days` anyway (§13.1) |
| Merge between two claimed leads | Target team's lead stays lead; they can transfer afterwards |
| Lead silent | Removed by an unanswered re-confirmation or presence check (§12.5, §12.7), like any member. Otherwise members may leave; admins may reassign |

## 12.9 Team state machine

| From | Event | To |
|---|---|---|
| `forming` | Formation applies | `review` |
| `forming` | Lead unclaims; leaderless core dissolved at formation | `disbanded` |
| `review` | Team's `review_ends_at` | `locked` |
| `review` | Absorbed by a merge; no confirmed members at lock; admin disband | `disbanded` |
| `locked` | `starts_at` | `active` |
| `locked` | Below `min` at `reg_deadline`; no members at `starts_at`; admin disband | `disbanded` |
| `active` | Result submitted | `finished` or `withdrawn` (the result's `outcome`) |
| `active` | No result by `results_close_days` | `withdrawn` |
| any non-terminal | Hackathon cancelled | `cancelled` |

Terminal: `finished`, `withdrawn`, `disbanded`, `cancelled`. The project `status` of a team follows §27.1.

## 12.10 Timers (`event_tick`, every 5 min)

| Condition | Action |
|---|---|
| Phase `open`, `now ≥ formation_at` | Enqueue `run_formation(main)` |
| Phase `open`, early-start conditions met (§12.2) | `formation_at = now + 1 h`, notify |
| `formation_at − 24 h` | `formation_soon` to enrolled members and leads, once |
| Proposed member, `confirm_by − confirm_reminder_hours` | `confirm_reminder` |
| Proposed member past `confirm_by`, outside a review end | Remove (`lapsed`); the seat opens |
| Team in `review`, below `V`, `review_ends_at − 12 h` | `review_ending` to the lead (§12.6) |
| Team in `review`, `now ≥ review_ends_at` | Lock the team (§12.7); main round: phase `locked` |
| Lead offer past `expires_at` | Next candidate or forced assignment (§12.8) |
| `locked`, open seat past its head start, `now < reg_deadline` | Reserve offer (§12.7 order) |
| `locked`, late-round conditions met (§12.7) | `lead_opportunity` (once per day); after `late_claim_grace_hours` → late round without a claim |
| Every late core's window over or core full | Enqueue `run_formation(late)` |
| `reg_deadline` | Stop seat filling; expire invites and requests; disband teams below `min` |
| `starts_at − presence_check_hours` | Presence check (§12.7) |
| `starts_at` | Remove remaining proposed; disband empty teams; others `active`; phase `closed`; reserve `expired` |
| `ends_at`, team `active`, no result | `result_due` to the lead; after `results_due_days` → `result_due` to every confirmed member, alert `result_overdue`, reminder every 3 days; after `results_close_days` → auto-close (§13.1) |
| Invite past `expires_at` | `expired`; both sides get `invite_expired` |

## 12.11 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/events/{id}/formation` | member | → phase, `T`, `V`, `core_max`, `formation_at`, `review_ends_at`, `team_deadline`, counts (incl. volunteers), live projection, my state, `allowed_actions` |
| POST | `/api/v1/events/{id}/enroll` | member | `{preferred_area_key?, note?, willing_to_lead?}` |
| PATCH | `/api/v1/events/{id}/enroll` | enrolled member | Same fields |
| DELETE | `/api/v1/events/{id}/enroll` | enrolled member not on a team | Withdraw |
| GET | `/api/v1/events/{id}/enrollments?cursor=` | member | Name, avatar, preferred area, volunteer flag, top scores, team if any |
| GET | `/api/v1/events/{id}/reserve` | member | Ranked reserve |
| GET | `/api/v1/me/enrollments?status=` | member | Own enrollments |
| GET | `/api/v1/events/{id}/lead-fit/me` | member | → `{fit, eligible, reason, computed_at}` |
| POST | `/api/v1/events/{id}/projects` | member with fit ≥ threshold | Claim lead: `{name, description_md?}` |
| GET | `/api/v1/projects/{id}` | member | Pending invites, requests and offers are visible to the lead and admins only |
| PATCH | `/api/v1/projects/{id}` | lead | `{name, description_md}` |
| POST | `/api/v1/projects/{id}/ready` | lead | Mark ready |
| DELETE | `/api/v1/projects/{id}/ready` | lead | Unmark ready |
| POST | `/api/v1/projects/{id}/unclaim` | lead, `forming` only | – |
| POST | `/api/v1/projects/{id}/seats/close` | lead, `locked`, size ≥ `min` | Close seats |
| DELETE | `/api/v1/projects/{id}/seats/close` | lead, `locked` | Reopen seats |
| POST | `/api/v1/projects/{id}/join-requests` | member with an `active` enrollment | `{note}` |
| GET | `/api/v1/projects/{id}/join-requests` | lead | Requests |
| DELETE | `/api/v1/join-requests/{id}` | requester | Withdraw |
| POST | `/api/v1/join-requests/{id}/accept` | lead | – |
| POST | `/api/v1/join-requests/{id}/reject` | lead | – |
| POST | `/api/v1/projects/{id}/invites` | lead | `{user_id, message?, area_key?}`; `core` in `forming`, `fill` in `review`/`locked` until `reg_deadline` |
| GET | `/api/v1/projects/{id}/invites` | lead, admin | Invites |
| DELETE | `/api/v1/invites/{id}` | lead | Withdraw |
| GET | `/api/v1/me/invites?status=` | member | Own invites |
| POST | `/api/v1/invites/{id}/accept` | invitee | – |
| POST | `/api/v1/invites/{id}/decline` | invitee | – |
| POST | `/api/v1/projects/{id}/placement/confirm` | proposed team member | Any `confirm_kind` |
| POST | `/api/v1/projects/{id}/placement/decline` | proposed team member | `{withdraw}` |
| GET | `/api/v1/me/lead-offers?status=` | member | Own lead offers |
| POST | `/api/v1/lead-offers/{id}/accept` | offered member | – |
| POST | `/api/v1/lead-offers/{id}/decline` | offered member | – |
| DELETE | `/api/v1/lead-offers/{id}` | lead | Cancel own transfer offer |
| POST | `/api/v1/projects/{id}/transfer-lead` | lead | `{user_id}` |
| POST | `/api/v1/projects/{id}/lead/step-down` | lead | – |
| POST | `/api/v1/projects/{id}/members` | lead | `{user_id, area_key?}` from the reserve; `review`, or `locked` until `reg_deadline` |
| DELETE | `/api/v1/projects/{id}/members/{user_id}` | lead, `review` | Remove → reserve |
| PATCH | `/api/v1/projects/{id}/members/{user_id}` | lead | `{area_key}` |
| POST | `/api/v1/projects/{id}/swaps` | lead | `{from_user_id, to_user_id}` |
| POST | `/api/v1/swaps/{id}/accept` | other lead, either moved member | – |
| POST | `/api/v1/swaps/{id}/decline` | other lead, either moved member | – |
| POST | `/api/v1/projects/{id}/merges` | lead | `{target_team_id}` |
| POST | `/api/v1/merges/{id}/accept` | either lead | – |
| POST | `/api/v1/merges/{id}/decline` | either lead | – |
| POST | `/api/v1/projects/{id}/leave` | team member, lead | – |
| GET | `/api/v1/me/projects?status=` | member | Own teams and projects |
| PATCH | `/api/v1/admin/events/{id}/formation` | admin | `{target_override, formation_at_override}` |
| POST | `/api/v1/admin/events/{id}/formation/run` | admin | Form now in `open`, re-run in `review` |
| GET | `/api/v1/admin/events/{id}/formation/runs` | admin | Input, output, planner, validation errors |
| GET | `/api/v1/admin/events/{id}/lead-candidates` | admin | Lead candidates |
| POST | `/api/v1/admin/events/{id}/projects` | admin | `{lead_user_id, name}` |
| PUT | `/api/v1/admin/events/{id}/reserve/order` | admin | Reserve order |
| POST | `/api/v1/admin/projects/{id}/members` | admin | Add member |
| DELETE | `/api/v1/admin/projects/{id}/members/{user_id}` | admin | Remove member |
| POST | `/api/v1/admin/projects/{id}/move` | admin | `{user_id, to_team_id}` |
| PUT | `/api/v1/admin/projects/{id}/lead` | admin | Assign lead |
| POST | `/api/v1/admin/projects/{id}/disband` | admin | `{reason}` |
| POST | `/api/v1/admin/projects/{id}/status` | admin | `{status, reason}` |

## 12.12 Done when

- Property tests: for random inputs (up to 60 people, 0–5 cores, varied `T`/`V`, 0–N volunteers), every applied plan satisfies the hard constraints, and `placement_bounds()` matches a brute-force implementation of the §12.3 definition for `n ≤ 20`.
- The fallback planner alone produces valid plans for the same inputs.
- Concurrency: parallel accepts, adds and swaps never exceed `max` or double-book a member; mutations during `forming` get 409.
- Nobody is ever `confirmed` on a team without an explicit action of their own (claim, accepted invite or request, confirm, accepted offer), except a forced lead who was already confirmed.
- Nobody silent stays on a team: every member who reaches `active` answered both the review and the presence check (or was skipped by the recency rule).
- Every team that reaches `locked` or `active` has a lead.
- Open seats are filled in the §12.7 order and never after `reg_deadline`.
- Time-travel tests cover every row of §12.10.
