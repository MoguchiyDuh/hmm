# Telegram web preview (`t.me/s/<channel>`)

Probed 2026-11-15. Adapter: `telegram`. Kinds: meetup, workshop, conference, contest (whatever a channel posts).

Server-rendered HTML preview of public channels — the only way to read Telegram without an account, a bot token or an MTProto client. No API, no RSS.

## Access

| URL | method | status | size |
|---|---|---|---|
| `t.me/s/nstechtalks` | GET | 200 | 110 KB, 12 messages |
| `t.me/s/xecut_bg` | GET | 200 | 115 KB, 19 messages |
| `t.me/s/SERBIAGAMEDEVEVENTS` | GET | 200 | 133 KB, 20 messages |
| `t.me/s/<ch>?before=<id>` | GET | 200 | 83–130 KB per page |
| `t.me/s/<ch>?before=1` | GET | 200 | 19.6 KB, `No posts found` |
| `t.me/s/<ch>?q=<term>` | GET | 200 | 186 KB, 20 hits (in-channel search works anonymously) |
| `t.me/<ch>/<id>?embed=1&mode=tme` | GET | 200 | 12–19 KB, single post, has `<time datetime>` + text |
| `t.me/s/<missing-channel>` | GET | **302** | 0 B, redirect |
| `t.me/robots.txt` | GET | **404** | nginx 404 page |

Headers: `content-type: text/html; charset=utf-8`, `cache-control: no-store`, `pragma: no-cache`, **no ETag, no Last-Modified** — conditional requests are useless, every poll is a full page. A `stel_ssid` session cookie is set on the first hit; no auth is required or sent. Latency 0.3–1.0 s per page. No rate limiting observed at ~20 requests / 1.3 s spacing.

Pages are byte-capped rather than count-capped: the preview returned 12, 19 and 20 messages for ~110–133 KB, so batches of 8–20 messages per request are normal and the parser must not assume a fixed page size.

## Discovery

- `GET t.me/s/<channel>` → newest ~20 messages, ascending in document order (oldest first inside the page). Paging backwards: the page ends with
  `<a href="/s/<ch>?before=<oldest_id>" class="tme_messages_more js-messages_more" data-before="<oldest_id>">`.
  `before` is **exclusive** (`?before=293` returns ≤ 292). When there is nothing older the anchor disappears and the page says `No posts found`.
- Incremental crawl: keep the highest `data-post` id per channel, page back until the ids are known. There is no "since" parameter; `?q=` exists but returns matches in context, not a date filter.
- Volumes seen (pages fetched: 4 / 3 / 3):

| channel | messages read | date span | ids | subscribers | notes |
|---|---|---|---|---|---|
| `@nstechtalks` | 40 | 2025-10-31 … 2026-11-06 | 212…316 | 1.18K | Novi Sad meetup, Russian; 175 photos, 113 links |
| `@xecut_bg` | 59 | 2026-04-07 … 2026-11-14 | 229…294 | 696 | hackerspace meetups in **Belgrade**, Russian; 105 photos, 35 files, 113 links |
| `@SERBIAGAMEDEVEVENTS` | 60 | 2024-11-24 … 2026-09-24 | 95…155 | 474 | Belgrade/Novi Sad gamedev events, Russian + English; 133 photos |

- Channel header carries `div.tgme_channel_info` with `counter_type`/`counter_value` pairs (`subscribers`, `photos`, `file(s)`, `links`) — a cheap liveness signal, plus `og:title`/`og:description` for the bio.
- Ids are **not contiguous**: `@nstechtalks` 212…316 holds only 40 live posts (65 ids missing inside the range), `@xecut_bg` 7, `@SERBIAGAMEDEVEVENTS` 1. Verified on one case: `t.me/nstechtalks/270` (inside a gap) returns a widget page with no message block, while `…/292` returns the post text and `datetime` → missing ids are **deleted** posts (or posts removed from the preview). So gaps are the only cancellation signal after the fact.

## Fields

HTML structure per message:

```html
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message text_not_supported_wrap user-color-1 js-widget_message"
       data-post="nstechtalks/316" data-view="eyJj…">
    <div class="tgme_widget_message_author accent_color">
      <a class="tgme_widget_message_owner_name"><span dir="auto">NS Tech Talks</span></a></div>
    <div class="media_supported_cont">
      <div class="tgme_widget_message_text js-message_text" dir="auto">…<br/><br/><b>…</b>
        <a href="https://forms.gle/…" target="_blank">…</a></div>
    </div>
    <div class="tgme_widget_message_reactions js-message_reactions">…</div>
    <div class="tgme_widget_message_footer compact js-message_footer">
      <span class="tgme_widget_message_views">581</span>
      <span class="tgme_widget_message_meta">
        edited &nbsp;<a class="tgme_widget_message_date" href="https://t.me/nstechtalks/316">
          <time datetime="2026-11-06T10:31:51+00:00" class="time">10:31</time></a></span>
    </div>
  </div>
</div>
```

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `data-post` | `nstechtalks/316` | `source.source_id` → `tg:@nstechtalks/316` | channel + message id; unambiguous and stable |
| `time[datetime]` | `2026-11-06T10:31:51+00:00` | **post publication time**, not `starts_at` | always `+00:00` (UTC); the human `<time>` text is rendered in the same UTC offset for anonymous visitors (`10:31` for both). Event start times exist only inside the text |
| `tgme_widget_message_text` | prose with `<br/>`, `<b>`, `<a>` | `description_md` (after HTML → Markdown) | line breaks are `<br/>`, HTML entities unescaped (`&#33;` = `!`). Poll posts and photo-only posts have **no** text div |
| `<a href>` inside text | `https://forms.gle/sxdisG2eDhpKCs84A`, `https://t.me/NSTechTalksBot`, `https://xecut.me/en/location/` | `website_url` / `registration_url` | frequently a Google Form, a **Telegram bot** (`@NSTechTalksBot` is the RSVP mechanism), or the venue page |
| `a.tgme_widget_message_link_preview[href]` + `.link_preview_site_name` / `.link_preview_title` / `.link_preview_description` | `Google Docs` / `Заявка спикера на митапы NS Tech Talks 2026` / … | `website_url`, `summary` | duplicates the first URL in the text; usually the richest single hint about what the post is |
| `tgme_widget_message_owner_name` | `NS Tech Talks` | `organizer` | the channel name |
| `tgme_widget_message_views` | `581`, `1.37K` | — | display strings, not numbers |
| `tgme_widget_message_reactions` | `👍12 ❤4 🔥2` | — | emoji counts |
| `a.tgme_widget_message_photo_wrap` (`style="background-image:url(...)"`) | `https://cdn4.telesco.pe/file/L8bc…jpg` | — | posters/event covers. Grouped albums use `tgme_widget_message_grouped_wrap`; some are `blured` and token-signed, so treat URLs as non-durable |
| `tgme_widget_message_video_wrap` / `_video_player`, `_document_wrap` (`…_document_title`, `…_document_extra` = `109.8 KB`) | `Тоник 4 рецепта.pdf` | — | rare (1 video and 4 documents on the first `@xecut_bg` page, 0 elsewhere) |
| `service_message` class | `NS Tech Talks pinned «Всем привет! 🍂 …»` | — | pinning notices repeat the pinned post's first lines → duplicate-extraction trap |
| `edited` before the date anchor | `edited &nbsp;<a …>` | — | 4 of 12 messages on the first `@nstechtalks` page were edited; edit **time** is not exposed (unverified whether `datetime` stays the original) |
| `data-view` | base64 `{"c":…,"p":…,"t":…,"h":…}` | — | analytics token, ignore |
| — | — | `starts_at`, `ends_at`, `timezone`, `format`, `location.*`, `team_size_*`, `prize`, `eligibility`, `language`, `people` | **none structured**. Everything is prose |

Event facts are always free text with no schema. Observed date forms in event announcements: `31 октября` (no year — the anchor is the post `datetime`), `28 сентября, 12:00 - 20:00`, `В эту субботу, 11 апреля, в 14:00`, `Thursday, September 24, at 5 PM`, `12.07 с 14:00`. Venues: `📍 Бар Arena, Нови-Сад`, `📍 Белград, Hilton Belgrade`, `НТП Нови Сад (Fruškogorska 1)`. Price/registration: `Вход … бесплатный`, `нужна регистрация: @NSTechTalksBot`. Format: `Смотреть трансляцию можно бесплатно онлайн` (online mention inside prose only).

## Revisits and identity

- `source_id` = `tg:@<channel>/<message_id>`. One event typically spans **4–5 messages**: announcement, "регистрация открыта", "митап уже сегодня" reminder, "спасибо всем / фотографии", "выложили записи докладов" (e.g. NS Tech Talks #17 → ids 284, 285, 287, 291; #20 → 309, 310, 311, 312). Dedupe/group by extracted event title+date, keep the earliest announcement as canonical, and only the announcement should become an `EventImport`.
- Edits are flagged (`edited` marker) but no edit time; deletion shows up as an id gap, verified by fetching the single post. A post that disappears is **not** proof of cancellation — only re-check the linked registration page before pushing `status = cancelled`.
- No ETag/Last-Modified/`no-store` → change detection = re-download the newest page(s) and diff by `(id, text hash)`. Two pages are enough for weekly-dormant channels; three to four for weekly-active ones.
- Recaps link recordings (`YouTube`, `youtu.be`) and photo albums — the closest thing to "results" this source has. Never any attendee/speaker list with handles beyond `@mentions` inside the text (those are Telegram usernames → `people.telegram_username`, legitimately published by the source).
- The pinned service message re-quotes the pinned post; skip `service_message` rows.

## Rate and failure behavior

- ~20 requests at 1.3 s spacing: all 200, no 429, no CAPTCHA, no interstitial, no `set-cookie` beyond `stel_ssid`.
- Missing channel → **302** with empty body (follow to `t.me/`); treat as a disabled instance.
- Paging past the oldest post → 200 with `No posts found` (19.6 KB) and no `tme_messages_more`.
- `?before=0` / malformed ids behave like the empty page (no error).
- A post hidden from the preview (unsupported media) shows `<div class="media_not_supported_cont">` with `Please open Telegram to view this post` and a `VIEW IN TELEGRAM` link — the text may still be present (as in id 316) or absent (poll 307).
- Private/closed channels return the generic landing page, not the timeline.
- The preview is server-rendered but heavy (110 KB for 12 messages), so a full backfill of a chatty channel is dozens of pages; keep a per-channel high-water mark.

## Volume and relevance

| channel | messages | events (est.) | cadence | relevance |
|---|---|---|---|---|
| `@nstechtalks` | 40 (2025-10 → 2026-11) | ~10 (issues #13–#20 + workshop + Kleos collab) | 1–7 posts/month, silent Aug 2026 | Novi Sad meetups, Serbian/Russian tech community |
| `@xecut_bg` | 59 (2026-04 → 2026-11) | ~15 (weekly "2600" meetups, Nix, keyboards, AI meetup, birthday) | 2–13 posts/month | Belgrade hackerspace; more posts than events because stream links and recordings are separate messages |
| `@SERBIAGAMEDEVEVENTS` | 60 (2024-11 → 2026-09) | ~55 (one post per event, title-first) | 1–8 posts/month, dead since 2026-09-24 | Belgrade/Novi Sad gamedev events, best signal-to-noise of the three |

Noise per channel: roughly 3 of 4 posts in `@nstechtalks`/`@xecut_bg` are reminders, thank-yous, stream links or recordings; `@SERBIAGAMEDEVEVENTS` is nearly 1:1 event posts. Text volume per message: median 593 chars (`@nstechtalks`), 261 (`@xecut_bg`), 702 (`@SERBIAGAMEDEVEVENTS`); p90 1458 / 761 / 994; max 1912. Event announcements run 600–1400 chars — too much boilerplate and too little structure for regex, so LLM extraction is the right tool, with the post `datetime` injected into the prompt as the year anchor.

## Quirks

- The page is a *preview*: media-only posts lose their caption if it was a poll (`307` had no text at all), and some albums/documents carry no text (`xecut_bg/270`).
- Emoji are rendered as `<i class="emoji" style="background-image:url('//telegram.org/img/emoji/40/…')"><b>🎤</b></i>` and premium emoji as `<tg-emoji emoji-id="…"><i class="emoji">…</i></tg-emoji>`; strip the wrappers but keep the `<b>` inner text or emoji vanish from the extracted text.
- `<time>` text and `datetime` attribute are both UTC for anonymous visitors — a Belgrade channel's 11:31 local post shows `10:31`. Never trust the clock text; use the attribute and the channel's known zone.
- Id gaps are deletions, not pagination artefacts (`nstechtalks` lost 65 of 105 ids in the probed range).
- The pinning notice (`service_message`) repeats the pinned post's opening lines — a silent duplicate source.
- `?q=` searches the whole channel and returns 20 hits with surrounding context; there is no `?after=`/date filter, so it cannot replace paging for incremental crawls.
- Language is mixed per channel (`@SERBIAGAMEDEVEVENTS` alternates English and Russian), and `language` must come from the LLM, not from a channel default.
- Registration is often a Telegram bot handle (`@NSTechTalksBot`), which is not an `HttpUrl` — keep it in `registration_url`/`eligibility` as text or drop it, but do not fabricate a URL.
- `t.me/robots.txt` is a 404 page, so there is no robots policy to obey; the ToS question is Telegram's rather than the channel's, and the preview is public by design.
- Channel descriptions frequently point to the venue (`https://xecut.me/en/location/`) rather than a per-event page.

## Config sketch

```toml
[[source]]
id = "tg-rs"
adapter = "telegram"
interval = "6h"
rate = "10/min"
channels = ["nstechtalks", "xecut_bg", "SERBIAGAMEDEVEVENTS"]
pages_per_run = 3
backfill_pages = 20
default_timezone = "Europe/Belgrade"
skip_classes = ["service_message"]
dedupe = "event-title+date"
llm = true
```
