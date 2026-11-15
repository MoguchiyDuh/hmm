# 20. Telegram bot: template samples

Rendered from `tests/fixtures/notifications/*.json` with `PUBLIC_URL=https://hmm.example`, the event timezone (or UTC) and "now" set to the notification's `created_at`. Text is Telegram HTML; buttons are listed per keyboard row. Silent types are sent with `disable_notification`.

## `accept_reminder`

```html
⏰ Your place on <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> (Northern Lights Hackathon 2026) is waiting. Accept by Fri 20 Nov 18:00 EET (in 6 hours).
```

- [Accept] `a:placement_accept:019a7b32000070008000000000000003` · [Decline] `a:placement_decline:019a7b32000070008000000000000003`

## `admin_alert`

```html
⚠️ <a href="https://hmm.example/admin"><b>Team below minimum size</b></a>
Aurora Builders has 2 confirmed members, below the minimum of 3, and the reserve is empty.
<i>team below min · warning · Aurora Builders</i>
```

- [Open alert] → `https://hmm.example/admin`

## `admin_digest` (silent)

```html
🗂 Admin digest, Mon 09 Nov to Mon 16 Nov
• 7 member reviews waiting
• 2 overdue to-check items
• 1 member event duplicate
• 3 results to verify
• 2 open team below min alerts
• 1 open parser down alert
```

- [Open admin] → `https://hmm.example/admin`

## `attendance_confirm_digest`

```html
Who did you meet at <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a>?
Tap everyone you actually met there.
```

- [☐ Casey Quinn] `c:attendance:019a7b3200007000800000000000000d`

## `contributions_confirm_digest`

```html
<a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> released <b>Aurora 1.0</b>. Can you confirm that <b>Maya Patel</b> leads the project?
```

- [☐ Confirm Maya Patel] `c:project_member:019a7b32000070008000000000000006`

## `event_cancelled`

```html
❌ <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a> was cancelled: The organizer cancelled the event because the venue is unavailable.
```

## `event_changed` (silent)

```html
📅 <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a> changed:
• starts: Thu 26 Nov 11:00 EET → Fri 27 Nov 11:00 EET
Now Fri 27 Nov to Sun 29 Nov (in 7 days).
```

## `formation_soon`

```html
⏳ Teams for <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a> form Sat 21 Nov 12:00 EET (in 24 hours).
23 people enrolled so far, about 4 teams.
```

## `help_offer_accepted`

```html
🎉 <b>Alex Morgan</b> accepted your offer for <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a>. Your mentorship has started.
```

## `help_offer_closed` (silent)

```html
Your offer for <a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a> was closed: The learner accepted another mentor's offer.
```

## `help_offer_received`

```html
🤝 <b>Noor Kim</b> offered to help with <a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a>:
<blockquote>I can review your service design and pair on authentication next week.</blockquote>
<i>mentoring · Backend 8.2 (you: 4.3) · 6 past mentorships</i>
```

- [Accept offer] `a:help_offer_accept:019a7b32000070008000000000000010`

## `help_request_declined`

```html
The mentor you asked declined your help request:
<a href="https://hmm.example/mentorship"><b>Reliable Python services</b></a> · Backend · online
<blockquote>Deploy a service with authentication and observability.</blockquote>
You can open it to everyone now.
```

- [Open to everyone] `a:help_request_open:019a7b3200007000800000000000000f`

## `help_request_direct`

```html
🙋 <b>Alex Morgan</b> asked you directly for help:
<a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a> · Backend · online
<blockquote>Deploy a service with authentication and observability.</blockquote>
Respond by Fri 27 Nov 10:00 UTC (in 7 days).
```

- [Decline] `a:help_request_decline:019a7b3200007000800000000000000f`
- [Help] `h:019a7b3200007000800000000000000f`

## `help_request_expired`

```html
⌛ Your help request expired without a match:
<a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a> · Backend · online
<blockquote>Deploy a service with authentication and observability.</blockquote>
```

- [Ask again] → `https://hmm.example/mentorship/new?from=019a7b32-0000-7000-8000-00000000000f`

## `help_request_match`

```html
🙋 <b>Alex Morgan</b> is looking for help:
<a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a> · Backend · online
<blockquote>Deploy a service with authentication and observability.</blockquote>
<i>Your backend mentor offer and service design experience match the learner's goal.</i>
Open until Sat 21 Nov 12:00 UTC (in 26 hours).
```

- [Help] `h:019a7b3200007000800000000000000f`

## `invite_accepted` (silent)

```html
✅ <b>Alex Morgan</b> accepted the invite to <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `invite_closed` (silent)

```html
The invite for <b>Alex Morgan</b> to <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> was closed: Formation started and pending core invites were closed.
```

## `invite_declined` (silent)

```html
<b>Alex Morgan</b> declined the invite to <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `invite_expired` (silent)

```html
⌛ The invite for <b>Alex Morgan</b> to <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> expired.
```

## `join_request_accepted`

```html
✅ <b>Maya Patel</b> accepted your request to join <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `join_request_rejected`

```html
<b>Maya Patel</b> turned down your request to join <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `lead_assigned`

```html
👑 <b>Maya Patel</b> now leads <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>: No candidate accepted before lock; the highest-ranked confirmed member was assigned.
```

- [Transfer lead] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `mentorship_close_proposed`

```html
<b>Noor Kim</b> proposed closing <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a>. Close it with an outcome by Fri 27 Nov 10:00 UTC (in 7 days).
```

- [Close] `cl:019a7b32000070008000000000000011`

## `mentorship_closed`

```html
🏁 <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a> with <b>Alex Morgan</b> and <b>Noor Kim</b> is closed.
Outcome: <b>Goal reached</b>
Reviews are open until Fri 27 Nov 10:00 UTC (in 7 days).
```

- [Acknowledge] `a:mentorship_acknowledge:019a7b32000070008000000000000011`
- [Endorse] → `https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011`

## `mentorship_idle`

```html
No check-ins on <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a> for 10 days. It stalls Tue 24 Nov 10:00 UTC (in 4 days) without one.
```

- [Check in] `ci:019a7b32000070008000000000000011`

## `mentorship_stalled`

```html
<a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a> stalled after 10 quiet days. It closes Tue 29 Dec 18:00 UTC (in 39 days) unless someone checks in.
```

- [Check in] `ci:019a7b32000070008000000000000011`

## `onboarding_reopened`

```html
<b>Avery Chen</b> reopened your onboarding.
```

- [Continue onboarding] → `https://hmm.example/onboarding`

## `party_invite`

```html
👥 <b>Avery Chen</b> invited you to their party for <a href="https://hmm.example/parties/019a7b32-0000-7000-8000-000000000003"><b>Northern Lights Hackathon 2026</b></a>.
Expires Sat 21 Nov 12:00 UTC (in 26 hours).
```

- [Accept] `a:invite_accept:019a7b32000070008000000000000004` · [Decline] `a:invite_decline:019a7b32000070008000000000000004`

## `penalty_applied`

```html
⚠️ Penalty: An administrator confirmed coordinated false attendance confirmations.
XP -20.
```

## `placement_lapsed`

```html
Your place on <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> (Northern Lights Hackathon 2026) lapsed: The placement was not confirmed before its deadline.
You can enroll again until Tue 24 Nov 12:00 UTC (in 4 days).
```

- [Enroll again] `a:event_enroll:019a7b32000070008000000000000002`

## `project_idle`

```html
💤 <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> has had no progress for 10 days. It pauses Sun 20 Dec 10:00 UTC (in 30 days) unless someone posts an update.
```

- [Open project] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_invite_received`

```html
👥 <b>Avery Chen</b> invited you to project <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
Expires Sat 21 Nov 12:00 UTC (in 26 hours).
```

- [Accept] `a:invite_accept:019a7b32000070008000000000000004` · [Decline] `a:invite_decline:019a7b32000070008000000000000004`

## `project_join_request_received`

```html
🙋 <b>Alex Morgan</b> wants to join <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

- [Accept] `a:join_request_accept:019a7b32000070008000000000000005` · [Reject] `a:join_request_reject:019a7b32000070008000000000000005`

## `project_member_joined` (silent)

```html
<b>Jordan Lee</b> joined <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>. Now 4 members.
```

## `project_member_left` (silent)

```html
<b>Jordan Lee</b> left <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>. Now 4 members.
```

## `project_member_removed`

```html
<b>Avery Chen</b> removed <b>Jordan Lee</b> from <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `project_paused`

```html
⏸ <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> was paused: No updates or milestone changes were posted for 60 days.
```

- [Open project] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_released`

```html
🚀 <b>Avery Chen</b> released <b>Aurora 1.0</b> of <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
<blockquote>First public release with account linking and event search.</blockquote>
```

## `reserve_placed`

```html
Teams for <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a> are formed. You're in the reserve (4 people) and get a place if a seat opens.
```

## `restriction_lifted`

```html
Your <b>confirming</b> restriction was lifted: The seven-day confirmation restriction expired.
```

## `result_due`

```html
📝 Submit the result for <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> (Northern Lights Hackathon 2026).
The lead can submit now; any member can from Sun 06 Dec 18:00 UTC (in 6 days).
Submissions close Tue 29 Dec 18:00 UTC (in 29 days).
```

- [Submit result] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `review_received` (silent)

```html
⭐ You received 3 reviews for <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `review_removed` (silent)

```html
A review of <b>Casey Quinn</b> for <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> was removed: The review contained personal harassment and was removed by an administrator.
```

## `reviews_open`

```html
⭐ Reviews are open for <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
To review: Casey Quinn
1 submitted so far.
Closes Tue 29 Dec 18:00 UTC (in 39 days).
```

- [Write reviews] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `reviews_pending`

```html
⭐ Reminder: you still have reviews to write for <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
To review: Casey Quinn
1 submitted so far.
Closes Tue 29 Dec 18:00 UTC (in 39 days).
```

- [Write reviews] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `score_changed` (silent)

```html
📈 Your scores changed:
• Backend: 5.4 → <b>6.7</b> — Recent confirmed project contributions updated the effective score.
```

## `scores_reviewed` (silent)

```html
✅ <b>Robin Silva</b> reviewed your scores.
• Backend: 5.4 → <b>6.7</b> — Recent confirmed project contributions updated the effective score.
```

## `team_disbanded`

```html
Team <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> (Northern Lights Hackathon 2026) was disbanded: The team was below minimum size at the registration deadline.
```

## `team_invite`

```html
👥 <b>Avery Chen</b> invited you to team <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> for <b>Northern Lights Hackathon 2026</b>.
Expires Sat 21 Nov 12:00 UTC (in 26 hours).
```

- [Accept] `a:invite_accept:019a7b32000070008000000000000004` · [Decline] `a:invite_decline:019a7b32000070008000000000000004`

## `team_member_joined` (silent)

```html
<b>Jordan Lee</b> joined <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>. Now 4 members.
```

## `team_member_left` (silent)

```html
<b>Jordan Lee</b> left <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>: Jordan withdrew because of a scheduling conflict. Now 4 members.
```

## `team_proposed`

```html
🧩 You've been placed on team <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> for <b>Northern Lights Hackathon 2026</b>.
<i>Your backend experience covers the team's missing primary area.</i>
Team: Casey Quinn
Accept by Sun 22 Nov 14:00 EET (in 2 days).
```

- [Accept] `a:placement_accept:019a7b32000070008000000000000003` · [Decline] `a:placement_decline:019a7b32000070008000000000000003`

## `team_status_changed`

```html
Team <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> is now <b>active</b> (The event started and the locked roster became active). Good luck at Northern Lights Hackathon 2026!
```

## `telegram_link_broken`

```html
Telegram delivery stopped Fri 20 Nov 10:00 UTC (just now): Telegram reported that the bot was blocked by the user.
```
