# 20. Telegram bot: Russian template samples

Rendered from `tests/fixtures/notifications/*.json` with `PUBLIC_URL=https://hmm.example`, the event timezone (or UTC) and "now" set to the notification's `created_at`. Text is Telegram HTML; buttons are listed per keyboard row. Silent types are sent with `disable_notification`.

## `accept_reminder`

```html
⏰ Ваше место в команде <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> (Northern Lights Hackathon 2026) ждёт подтверждения. Подтвердите до Пт 20 ноя 18:00 EET (через 6 часов).
```

- [Принять] `a:placement_accept:019a7b32000070008000000000000003` · [Отклонить] `a:placement_decline:019a7b32000070008000000000000003`

## `admin_alert`

```html
⚠️ <a href="https://hmm.example/admin"><b>Team below minimum size</b></a>
Aurora Builders has 2 confirmed members, below the minimum of 3, and the reserve is empty.
<i>команда меньше минимума · Aurora Builders</i>
```

- [Открыть алерт] → `https://hmm.example/admin`

## `admin_digest` (silent)

```html
🗂 Сводка для админов, Пн 09 ноя — Пн 16 ноя
• Участники на проверке: 7
• Просроченные задачи на проверку: 2
• Дубликаты событий от участников: 1
• Результаты на проверку: 3
• Открытые алерты «команда меньше минимума»: 2
• Открытые алерты «парсер не работает»: 1
```

- [Открыть админку] → `https://hmm.example/admin`

## `attendance_confirm_digest`

```html
Кого вы встретили на <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a>?
Отметьте всех, кого вы действительно там встретили.
```

- [☐ Casey Quinn] `c:attendance:019a7b3200007000800000000000000d`

## `contributions_confirm_digest`

```html
<a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>: вышел релиз <b>Aurora 1.0</b>. Подтверждаете, что <b>Maya Patel</b> руководит проектом?
```

- [☐ Подтвердить: Maya Patel] `c:project_member:019a7b32000070008000000000000006`

## `event_cancelled`

```html
❌ Событие <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a> отменено: The organizer cancelled the event because the venue is unavailable.
```

## `event_changed` (silent)

```html
📅 Изменения в <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a>:
• начало: Чт 26 ноя 11:00 EET → Пт 27 ноя 11:00 EET
Теперь: Пт 27 ноя — Вс 29 ноя (через 7 дней).
```

## `formation_soon`

```html
⏳ Формирование команд на <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a>: Сб 21 ноя 12:00 EET (через 24 часа).
Уже записались: 23 человека, это примерно 4 команды.
```

## `help_offer_accepted`

```html
🎉 Ученик <b>Alex Morgan</b> принял ваше предложение по теме <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a>. Менторство началось.
```

## `help_offer_closed` (silent)

```html
Ваше предложение по теме <a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a> закрыто: The learner accepted another mentor's offer.
```

## `help_offer_received`

```html
🤝 Ментор <b>Noor Kim</b> предлагает помощь по теме <a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a>:
<blockquote>I can review your service design and pair on authentication next week.</blockquote>
<i>менторство · Backend 8.2 (у вас 4.3) · прошлых менторств: 6</i>
```

- [Принять предложение] `a:help_offer_accept:019a7b32000070008000000000000010`

## `help_request_declined`

```html
Ментор, которого вы выбрали, отклонил ваш запрос помощи:
<a href="https://hmm.example/mentorship"><b>Reliable Python services</b></a> · Backend · онлайн
<blockquote>Deploy a service with authentication and observability.</blockquote>
Теперь его можно открыть для всех.
```

- [Открыть для всех] `a:help_request_open:019a7b3200007000800000000000000f`

## `help_request_direct`

```html
🙋 <b>Alex Morgan</b> просит о помощи именно вас:
<a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a> · Backend · онлайн
<blockquote>Deploy a service with authentication and observability.</blockquote>
Ответьте до Пт 27 ноя 10:00 UTC (через 7 дней).
```

- [Отклонить] `a:help_request_decline:019a7b3200007000800000000000000f`
- [Помочь] `h:019a7b3200007000800000000000000f`

## `help_request_expired`

```html
⌛ Срок вашего запроса помощи истёк, подходящего ментора не нашлось:
<a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a> · Backend · онлайн
<blockquote>Deploy a service with authentication and observability.</blockquote>
```

- [Спросить снова] → `https://hmm.example/mentorship/new?from=019a7b32-0000-7000-8000-00000000000f`

## `help_request_match`

```html
🙋 <b>Alex Morgan</b> ищет помощь:
<a href="https://hmm.example/help-requests/019a7b32-0000-7000-8000-00000000000f"><b>Reliable Python services</b></a> · Backend · онлайн
<blockquote>Deploy a service with authentication and observability.</blockquote>
<i>Your backend mentor offer and service design experience match the learner's goal.</i>
Запрос открыт до Сб 21 ноя 12:00 UTC (через 26 часов).
```

- [Помочь] `h:019a7b3200007000800000000000000f`

## `invite_accepted` (silent)

```html
✅ Участник <b>Alex Morgan</b> принял приглашение в <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `invite_closed` (silent)

```html
Приглашение в <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> для <b>Alex Morgan</b> закрыто: Formation started and pending core invites were closed.
```

## `invite_declined` (silent)

```html
Участник <b>Alex Morgan</b> отклонил приглашение в <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `invite_expired` (silent)

```html
⌛ Истекло приглашение в <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> для <b>Alex Morgan</b>.
```

## `join_request_accepted`

```html
✅ Лид <b>Maya Patel</b> принял вашу заявку на вступление в <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `join_request_rejected`

```html
Лид <b>Maya Patel</b> отклонил вашу заявку на вступление в <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `lead_assigned`

```html
👑 Новый лид команды <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>: <b>Maya Patel</b>. Причина: No candidate accepted before lock; the highest-ranked confirmed member was assigned.
```

- [Передать лидерство] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `mentorship_close_proposed`

```html
Ментор <b>Noor Kim</b> предлагает завершить менторство по теме <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a>. Завершите его с итогом до Пт 27 ноя 10:00 UTC (через 7 дней).
```

- [Завершить] `cl:019a7b32000070008000000000000011`

## `mentorship_closed`

```html
🏁 Менторство по теме <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a> завершено. Ученик: <b>Alex Morgan</b>, ментор: <b>Noor Kim</b>.
Итог: <b>Цель достигнута</b>
Отзывы открыты до Пт 27 ноя 10:00 UTC (через 7 дней).
```

- [Подтвердить] `a:mentorship_acknowledge:019a7b32000070008000000000000011`
- [Порекомендовать] → `https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011`

## `mentorship_idle`

```html
В менторстве по теме <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a> нет чек-инов уже 10 дней. Без чек-ина оно застопорится Вт 24 ноя 10:00 UTC (через 4 дня).
```

- [Чек-ин] `ci:019a7b32000070008000000000000011`

## `mentorship_stalled`

```html
Менторство по теме <a href="https://hmm.example/mentorships/019a7b32-0000-7000-8000-000000000011"><b>Reliable Python services</b></a> застопорилось: 10 дней без активности. Оно закроется Вт 29 дек 18:00 UTC (через 39 дней), если никто не сделает чек-ин.
```

- [Чек-ин] `ci:019a7b32000070008000000000000011`

## `onboarding_reopened`

```html
Администратор <b>Avery Chen</b> снова открыл ваш онбординг.
```

- [Продолжить онбординг] → `https://hmm.example/onboarding`

## `party_invite`

```html
👥 <b>Avery Chen</b> приглашает вас в свою группу на <a href="https://hmm.example/parties/019a7b32-0000-7000-8000-000000000003"><b>Northern Lights Hackathon 2026</b></a>.
Действует до Сб 21 ноя 12:00 UTC (через 26 часов).
```

- [Принять] `a:invite_accept:019a7b32000070008000000000000004` · [Отклонить] `a:invite_decline:019a7b32000070008000000000000004`

## `penalty_applied`

```html
⚠️ Штраф: An administrator confirmed coordinated false attendance confirmations.
XP -20.
```

## `placement_lapsed`

```html
Ваше место в команде <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> (Northern Lights Hackathon 2026) аннулировано: The placement was not confirmed before its deadline.
Встать в очередь снова можно до Вт 24 ноя 12:00 UTC (через 4 дня).
```

- [Встать в очередь снова] `a:event_enroll:019a7b32000070008000000000000002`

## `project_idle`

```html
💤 В проекте <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> нет прогресса уже 10 дней. Он встанет на паузу Вс 20 дек 10:00 UTC (через 30 дней), если никто не опубликует обновление.
```

- [Открыть проект] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_invite_received`

```html
👥 <b>Avery Chen</b> приглашает вас в проект <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
Действует до Сб 21 ноя 12:00 UTC (через 26 часов).
```

- [Принять] `a:invite_accept:019a7b32000070008000000000000004` · [Отклонить] `a:invite_decline:019a7b32000070008000000000000004`

## `project_join_request_received`

```html
🙋 <b>Alex Morgan</b> хочет вступить в <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

- [Принять] `a:join_request_accept:019a7b32000070008000000000000005` · [Отклонить] `a:join_request_reject:019a7b32000070008000000000000005`

## `project_member_joined` (silent)

```html
<b>Jordan Lee</b> теперь в проекте <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>. Всего 4 участника.
```

## `project_member_left` (silent)

```html
<b>Jordan Lee</b> больше не в проекте <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>. Осталось 4 участника.
```

## `project_member_removed`

```html
Участник <b>Jordan Lee</b> удалён из проекта <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>. Инициатор: <b>Avery Chen</b>.
```

## `project_paused`

```html
⏸ Проект <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> на паузе: No updates or milestone changes were posted for 60 days.
```

- [Открыть проект] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `project_released`

```html
🚀 Вышел релиз <b>Aurora 1.0</b> проекта <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>. Автор: <b>Avery Chen</b>.
<blockquote>First public release with account linking and event search.</blockquote>
```

## `reserve_placed`

```html
Команды на <a href="https://hmm.example/events/019a7b32-0000-7000-8000-000000000002"><b>Northern Lights Hackathon 2026</b></a> сформированы. Вы в резерве (всего 4 человека) и получите место, если оно освободится.
```

## `restriction_lifted`

```html
Ограничение на <b>подтверждения</b> снято: The seven-day confirmation restriction expired.
```

## `result_due`

```html
📝 Отправьте результат команды <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> (Northern Lights Hackathon 2026).
Лид может отправить его сейчас, любой участник — с Вс 06 дек 18:00 UTC (через 6 дней).
Приём закрывается Вт 29 дек 18:00 UTC (через 29 дней).
```

- [Отправить результат] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `review_received` (silent)

```html
⭐ Вы получили 3 отзыва по <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
```

## `review_removed` (silent)

```html
Отзыв о <b>Casey Quinn</b> по <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> удалён: The review contained personal harassment and was removed by an administrator.
```

## `reviews_open`

```html
⭐ Открыты отзывы по <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
Кому написать отзыв: Casey Quinn
Уже отправлено: 1.
Отзывы закрываются Вт 29 дек 18:00 UTC (через 39 дней).
```

- [Написать отзывы] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `reviews_pending`

```html
⭐ Напоминание: у вас остались ненаписанные отзывы по <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>.
Кому написать отзыв: Casey Quinn
Уже отправлено: 1.
Отзывы закрываются Вт 29 дек 18:00 UTC (через 39 дней).
```

- [Написать отзывы] → `https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003`

## `score_changed` (silent)

```html
📈 Ваши оценки изменились:
• Backend: 5.4 → <b>6.7</b> — Recent confirmed project contributions updated the effective score.
```

## `scores_reviewed` (silent)

```html
✅ Администратор <b>Robin Silva</b> проверил ваши оценки.
• Backend: 5.4 → <b>6.7</b> — Recent confirmed project contributions updated the effective score.
```

## `team_disbanded`

```html
Команда <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> (Northern Lights Hackathon 2026) расформирована: The team was below minimum size at the registration deadline.
```

## `team_invite`

```html
👥 <b>Avery Chen</b> приглашает вас в команду <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> на <b>Northern Lights Hackathon 2026</b>.
Действует до Сб 21 ноя 12:00 UTC (через 26 часов).
```

- [Принять] `a:invite_accept:019a7b32000070008000000000000004` · [Отклонить] `a:invite_decline:019a7b32000070008000000000000004`

## `team_member_joined` (silent)

```html
<b>Jordan Lee</b> теперь в команде <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>. Всего 4 участника.
```

## `team_member_left` (silent)

```html
<b>Jordan Lee</b> больше не в команде <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a>: Jordan withdrew because of a scheduling conflict. Осталось 4 участника.
```

## `team_proposed`

```html
🧩 Вы распределены в команду <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> на <b>Northern Lights Hackathon 2026</b>.
<i>Your backend experience covers the team's missing primary area.</i>
Команда: Casey Quinn
Подтвердите место до Вс 22 ноя 14:00 EET (через 2 дня).
```

- [Принять] `a:placement_accept:019a7b32000070008000000000000003` · [Отклонить] `a:placement_decline:019a7b32000070008000000000000003`

## `team_status_changed`

```html
Команда <a href="https://hmm.example/projects/019a7b32-0000-7000-8000-000000000003"><b>Aurora Builders</b></a> теперь <b>активна</b> (The event started and the locked roster became active). Удачи на Northern Lights Hackathon 2026!
```

## `telegram_link_broken`

```html
Доставка в Telegram остановлена Пт 20 ноя 10:00 UTC (только что): Telegram reported that the bot was blocked by the user.
```
