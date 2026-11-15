# 20. Telegram bot: template samples

Rendered from `tests/fixtures/notifications/*.json` with `PUBLIC_URL=https://hmm.example`. Text is Telegram HTML; buttons are listed per keyboard row.

## `accept_reminder`

```html
⏰ 6 h left to accept your place on <b>Aurora Builders</b> (Northern Lights Hackathon 2026).
Accept by Fri 20 Nov 18:00 EET.
```

- [Accept] `a:placement_accept:019a7b32000070008000000000000003` · [Decline] `a:placement_decline:019a7b32000070008000000000000003`
- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `admin_alert`

```html
⚠️ <b>Team below minimum size</b>
Aurora Builders has 2 confirmed members, below the minimum of 3, and the reserve is empty.
<i>team below min · warning · Aurora Builders</i>
```

- [Open alert] → `https://hmm.example/admin`

## `admin_digest`

```html
🗂 Admin digest, Mon 09 Nov 09:00 UTC to Mon 16 Nov 09:00 UTC
Member reviews waiting: 7
Overdue to-check items: 2
Member event duplicates: 1
Results to verify: 3
Open alerts:
• team below min: 2
• parser down: 1
```

- [Open in hmm] → `https://hmm.example/admin`

## `attendance_confirm_digest`

```html
Who did you see at <b>Northern Lights Hackathon 2026</b>?
Tap the people you met there (1 attendee).
```

- [☐ Casey Quinn] `c:attendance:019a7b3200007000800000000000000d`
- [Confirm attendees] → `https://hmm.example/events/019a7b32-0000-7000-8000-000000000002`

## `contributions_confirm_digest`

```html
<b>Aurora Builders</b> released <b>Aurora 1.0</b>. Confirm that <b>Maya Patel</b> leads the project.
```

- [☐ Confirm Maya Patel] `c:project_member:019a7b32000070008000000000000006`
- [Confirm lead] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `event_cancelled`

```html
❌ <b>Northern Lights Hackathon 2026</b> was cancelled: The organizer cancelled the event because the venue is unavailable.
```

- [Open in hmm] → `https://hmm.example/events/019a7b32-0000-7000-8000-000000000002`

## `event_changed`

```html
📅 <b>Northern Lights Hackathon 2026</b> changed:
• starts at: Thu 26 Nov 11:00 EET → Fri 27 Nov 11:00 EET
Now Fri 27 Nov 11:00 EET to Sun 29 Nov 20:00 EET.
```

- [Open in hmm] → `https://hmm.example/events/019a7b32-0000-7000-8000-000000000002`

## `formation_soon`

```html
⏳ Teams for <b>Northern Lights Hackathon 2026</b> form Sat 21 Nov 12:00 EET.
23 enrolled, about 4 teams so far.
```

- [Open in hmm] → `https://hmm.example/events/019a7b32-0000-7000-8000-000000000002`

## `help_offer_accepted`

```html
🎉 <b>Alex Morgan</b> accepted your offer for <b>Reliable Python services</b>. Your mentoring has started.
```

- [Open in hmm] → `https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011`

## `help_offer_closed`

```html
Your offer for <b>Reliable Python services</b> was closed: The learner accepted another mentor's offer.
```

- [Open in hmm] → `https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f`

## `help_offer_received`

```html
🤝 <b>Noor Kim</b> offered to help with <b>Reliable Python services</b>:
<blockquote>I can review your service design and pair on authentication next week.</blockquote>
Backend score 8.2 (you: 4.3) · no reviews yet · 6 past mentorships · mentoring
```

- [Accept offer] `a:help_offer_accept:019a7b32000070008000000000000010`
- [Open in hmm] → `https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f`

## `help_request_declined`

```html
The mentor you asked declined your help request:
<b>Reliable Python services</b> · Backend · online
<blockquote>Deploy a service with authentication and observability.</blockquote>
You can open it to everyone now.
```

- [Open to everyone] `a:help_request_open:019a7b3200007000800000000000000f`
- [Open in hmm] → `https://hmm.example/mentorship`

## `help_request_direct`

```html
🙋 <b>Alex Morgan</b> asked you directly for help:
<b>Reliable Python services</b> · Backend · online
<blockquote>Deploy a service with authentication and observability.</blockquote>
Respond by Fri 27 Nov 10:00 UTC.
```

- [Decline] `a:help_request_decline:019a7b3200007000800000000000000f`
- [Help] `h:019a7b3200007000800000000000000f`
- [Open in hmm] → `https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f`

## `help_request_expired`

```html
⌛ Your help request expired without a match:
<b>Reliable Python services</b> · Backend · online
<blockquote>Deploy a service with authentication and observability.</blockquote>
```

- [Ask again] → `https://hmm.example/mentorship/new?from=019a7b32-0000-7000-8000-00000000000f`
- [Open in hmm] → `https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f`

## `help_request_match`

```html
🙋 <b>Alex Morgan</b> is looking for help:
<b>Reliable Python services</b> · Backend · online
<blockquote>Deploy a service with authentication and observability.</blockquote>
<i>Your backend mentor offer and service design experience match the learner's goal.</i>
Open until Sat 21 Nov 12:00 UTC.
```

- [Help] `h:019a7b3200007000800000000000000f`
- [Open in hmm] → `https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f`

## `invite_accepted`

```html
✅ <b>Alex Morgan</b> accepted the invite to <b>Aurora Builders</b>.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `invite_closed`

```html
The invite for <b>Alex Morgan</b> to <b>Aurora Builders</b> was closed: Formation started and pending core invites were closed.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `invite_declined`

```html
<b>Alex Morgan</b> declined the invite to <b>Aurora Builders</b>.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `invite_expired`

```html
⌛ The invite for <b>Alex Morgan</b> to <b>Aurora Builders</b> expired.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `join_request_accepted`

```html
✅ <b>Maya Patel</b> accepted your request to join <b>Aurora Builders</b>.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `join_request_rejected`

```html
<b>Maya Patel</b> turned down your request to join <b>Aurora Builders</b>.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `lead_assigned`

```html
👑 <b>Maya Patel</b> now leads <b>Aurora Builders</b>: No candidate accepted before lock; the highest-ranked confirmed member was assigned.
```

- [Transfer] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `mentorship_close_proposed`

```html
<b>Noor Kim</b> proposed closing <b>Reliable Python services</b>. Close it with an outcome by Fri 27 Nov 10:00 UTC.
```

- [Close] `cl:019a7b32000070008000000000000011`
- [Open in hmm] → `https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011`

## `mentorship_closed`

```html
🏁 <b>Reliable Python services</b> with <b>Alex Morgan</b> and <b>Noor Kim</b> is closed.
Outcome: <b>reached</b>
Reviews are open until Fri 27 Nov 10:00 UTC.
```

- [Acknowledge] `a:mentorship_acknowledge:019a7b32000070008000000000000011`
- [Endorse] → `https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011`

## `mentorship_idle`

```html
No check-ins on <b>Reliable Python services</b> for 10 days. It stalls Tue 24 Nov 10:00 UTC without one.
```

- [Check in] `ci:019a7b32000070008000000000000011`
- [Open in hmm] → `https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011`

## `mentorship_stalled`

```html
<b>Reliable Python services</b> stalled after 10 quiet days. It closes Tue 29 Dec 18:00 UTC unless someone checks in.
```

- [Check in] `ci:019a7b32000070008000000000000011`
- [Open in hmm] → `https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011`

## `onboarding_reopened`

```html
<b>Avery Chen</b> reopened your onboarding.
```

- [Open in hmm] → `https://hmm.example/onboarding`

## `party_invite`

```html
👥 <b>Avery Chen</b> invited you to their party for <b>Northern Lights Hackathon 2026</b>.
Expires Sat 21 Nov 12:00 UTC.
```

- [Accept] `a:invite_accept:019a7b32000070008000000000000004` · [Decline] `a:invite_decline:019a7b32000070008000000000000004`
- [Open in hmm] → `https://hmm.example/parties/019a7b32-0000-7000-8000-000000000003`

## `penalty_applied`

```html
⚠️ Penalty: An administrator confirmed coordinated false attendance confirmations.
XP -20.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `placement_lapsed`

```html
Your place on <b>Aurora Builders</b> (Northern Lights Hackathon 2026) lapsed: The placement was not confirmed before its deadline.
You can enroll again until Tue 24 Nov 12:00 UTC.
```

- [Enroll again] `a:event_enroll:019a7b32000070008000000000000002`
- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_idle`

```html
💤 <b>Aurora Builders</b> has had no progress for 10 days. It pauses Sun 20 Dec 10:00 UTC unless someone posts an update.
```

- [Open project] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_invite_received`

```html
👥 <b>Avery Chen</b> invited you to project <b>Aurora Builders</b>.
Expires Sat 21 Nov 12:00 UTC.
```

- [Accept] `a:invite_accept:019a7b32000070008000000000000004` · [Decline] `a:invite_decline:019a7b32000070008000000000000004`
- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_join_request_received`

```html
🙋 <b>Alex Morgan</b> wants to join <b>Aurora Builders</b>.
```

- [Accept] `a:join_request_accept:019a7b32000070008000000000000005` · [Reject] `a:join_request_reject:019a7b32000070008000000000000005`
- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_member_joined`

```html
<b>Jordan Lee</b> joined <b>Aurora Builders</b> (4 members).
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_member_left`

```html
<b>Jordan Lee</b> left <b>Aurora Builders</b>. 4 members remain.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_member_removed`

```html
<b>Avery Chen</b> removed <b>Jordan Lee</b> from <b>Aurora Builders</b>.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_paused`

```html
⏸ <b>Aurora Builders</b> was paused: No updates or milestone changes were posted for 60 days.
```

- [Open project] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_released`

```html
🚀 <b>Avery Chen</b> released <b>Aurora 1.0</b> of <b>Aurora Builders</b>.
<blockquote>First public release with account linking and event search.</blockquote>
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `reserve_placed`

```html
Teams for <b>Northern Lights Hackathon 2026</b> are formed. You're in the reserve queue (4 people) and get a place if a seat opens.
```

- [Open in hmm] → `https://hmm.example/events/019a7b32-0000-7000-8000-000000000002`

## `restriction_lifted`

```html
Your <b>confirming</b> restriction was lifted: The seven-day confirmation restriction expired.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `result_due`

```html
📝 Submit the result for <b>Aurora Builders</b> (Northern Lights Hackathon 2026).
The lead can submit now; any member can from Sun 06 Dec 18:00 UTC.
Submissions close Tue 29 Dec 18:00 UTC.
```

- [Submit result] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `review_received`

```html
⭐ You received 3 reviews for <b>Aurora Builders</b>.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `review_removed`

```html
A review of <b>Casey Quinn</b> for <b>Aurora Builders</b> was removed: The review contained personal harassment and was removed by an administrator.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `reviews_open`

```html
⭐ Reviews are open for <b>Aurora Builders</b>.
To review: Casey Quinn
1 submitted so far. Closes Tue 29 Dec 18:00 UTC.
```

- [Write reviews] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `reviews_pending`

```html
⭐ Reminder: you still have reviews to write for <b>Aurora Builders</b>.
To review: Casey Quinn
1 submitted so far. Closes Tue 29 Dec 18:00 UTC.
```

- [Write reviews] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `score_changed`

```html
📈 Your scores changed:
• Backend: 5.4 → <b>6.7</b> — Recent confirmed project contributions updated the effective score.
```

- [Open in hmm] → `https://hmm.example/me/scores`

## `scores_reviewed`

```html
✅ <b>Robin Silva</b> reviewed your scores.
• Backend: 5.4 → <b>6.7</b> — Recent confirmed project contributions updated the effective score.
```

- [Open in hmm] → `https://hmm.example/me/scores`

## `team_disbanded`

```html
Team <b>Aurora Builders</b> (Northern Lights Hackathon 2026) was disbanded: The team was below minimum size at the registration deadline.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `team_invite`

```html
👥 <b>Avery Chen</b> invited you to team <b>Aurora Builders</b> for <b>Northern Lights Hackathon 2026</b>.
Expires Sat 21 Nov 12:00 UTC.
```

- [Accept] `a:invite_accept:019a7b32000070008000000000000004` · [Decline] `a:invite_decline:019a7b32000070008000000000000004`
- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `team_member_joined`

```html
<b>Jordan Lee</b> joined <b>Aurora Builders</b> (4 members).
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `team_member_left`

```html
<b>Jordan Lee</b> left <b>Aurora Builders</b>: Jordan withdrew because of a scheduling conflict. 4 members remain.
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `team_proposed`

```html
🧩 You've been placed on team <b>Aurora Builders</b> for <b>Northern Lights Hackathon 2026</b>.
<i>Your backend experience covers the team's missing primary area.</i>
Team: Casey Quinn
Accept by Sun 22 Nov 14:00 EET.
```

- [Accept] `a:placement_accept:019a7b32000070008000000000000003` · [Decline] `a:placement_decline:019a7b32000070008000000000000003`
- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `team_status_changed`

```html
Team <b>Aurora Builders</b> is now <b>active</b> (The event started and the locked roster became active). Good luck at Northern Lights Hackathon 2026!
```

- [Open in hmm] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `telegram_link_broken`

```html
Telegram delivery stopped Fri 20 Nov 10:00 UTC: Telegram reported that the bot was blocked by the user.
```

- [Open in hmm] → `https://hmm.example/settings#telegram`

