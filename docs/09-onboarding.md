# 9. Core — Registration and onboarding

## 9.1 Registration flow

```text
GitHub sign-in ─► account created (onboarding_status=not_started), github_sync enqueued
      │
      ▼
Quiz: general questions ─► conditional branches revealed by earlier answers
      │
      ▼
Optional CV upload (PDF / Markdown / Typst)
      │
      ▼
Submit ─► full access immediately
      │      score_onboarding(quiz answers + CV text + GitHub summary) ─► initial area scores
      ▼
Admin "New members" queue (non-blocking): review, optionally adjust scores with evidence
```

- Until onboarding is submitted, the user can only reach the endpoints listed in §3.3.
- The CV step is part of the wizard but optional and skippable. It can be added or replaced later in settings (§7). The upload itself uses the same endpoint as §7.
- Submitting never waits for GitHub sync or CV extraction. `score_onboarding` runs as a job once both have settled: GitHub sync finished or failed, and CV extraction finished, failed or skipped. The wait is capped at 10 minutes, after which it scores with what's available. Later GitHub/CV changes flow through `recompute_member`.
- Profile shows "Scores calculating…" until the first result lands.

## 9.2 Quiz model
- Versioned quizzes; exactly one `published`. Only `draft` versions are editable. Publish freezes it; the previous published becomes `archived`. Submissions reference the version they were made against, so old answers never get reinterpreted.
- Questions are grouped into **sections** (`section` field). General sections come first; conditional sections and questions appear via `visible_if`.
- Question types and `config`:

| type | answer value | config |
|---|---|---|
| `single_choice` | option key | `{allow_other: bool}` |
| `multi_choice` | option key[] | `{min, max, allow_other}` |
| `scale` | int | `{min, max, min_label, max_label}` |
| `text` | string | `{max_length, multiline}` |
| `number` | number | `{min, max, unit}` |
| `url` | string | `{}` |

- `allow_other` adds a free-text `other` value alongside option keys.
- `area_hints` on options give algo per-area weight hints. They are editable data, not a formula.

## 9.3 Visibility conditions (`visible_if`)
JSON tree, evaluated against answers to questions **positioned earlier**:

```json
{ "all": [
  { "q": "languages", "op": "includes", "value": "python" },
  { "any": [
    { "q": "directions", "op": "includes", "value": "ml_ai" },
    { "q": "directions", "op": "includes", "value": "backend" }
  ]}
]}
```

Operators: `eq`, `neq`, `includes`, `excludes`, `includes_any` (value is list), `gte`, `lte` (scale/number), `answered`, `not_answered`. Combinators: `all`, `any`, `not`. Max depth 5. A section can carry `visible_if` too; it applies to all its questions.

Validation on save and publish (`/validate` returns all errors):
- referenced question exists and is positioned earlier; option keys exist; operator compatible with type;
- no unreachable required question that blocks submission (warning, not error);
- every published quiz has ≥ 1 question with `area_hints` per non-archived area (warning).

Core implements the evaluator once in Python. Frontend implements the same evaluator in TS for live branching. Shared test vectors live in `contracts/fixtures/visibility_cases.json`, and both implementations must pass them.

## 9.4 Submission
1. `not_started` → user opens `/onboarding`, gets the published definition → `in_progress` on first save.
2. Drafts autosave (`PUT answers`, partial allowed).
3. `submit`: server recomputes visibility, drops answers to hidden questions, checks required visible questions are answered and values are valid → 422 with per-question errors otherwise.
4. On success: submission `submitted`, user `completed`, enqueue `score_onboarding(user)` (§9.1 wait rule). Its output is written to `area_scores.calculated` with events, then `recompute_member` and `compute_lead_fits_for_member` are enqueued. A `member_reviews` item is opened (§9.5).
5. Retake: only via admin `reopen` → status `reopened`, user gets a notification and sees the **current** published version with previous answers pre-filled where question keys match. Until resubmitted, the user keeps full access (reopen is not a lock). Resubmission replaces the onboarding baseline and reopens the member-review item; overrides are untouched.

## 9.5 Admin member review (non-blocking)
- Each completed onboarding (and each resubmission) creates or reopens a `member_reviews` row with status `pending`.
- The review screen shows quiz answers, CV (text + file), GitHub summary, calculated scores with reasons, and **discrepancy hints** produced by `score_onboarding` (e.g. "quiz claims ML 8/10, no ML evidence in GitHub or CV").
- Admin actions:
  - set or remove area overrides, each with `reason` and `evidence` (`interview`, `github_review`, `cv_review`, `other`);
  - add a private admin note;
  - mark `reviewed` (with or without changes).
- Nothing waits on this. Unreviewed members are fully usable everywhere.
- Profile shows a "Scores reviewed by admin" badge with the date once reviewed. Algo receives `scores_reviewed_at` and may weigh reviewed scores as more reliable.
- Dashboard shows the pending count. Items older than `member_review_reminder_days` (7) appear in the weekly admin digest; no per-item alert spam.

## 9.6 Endpoints

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/onboarding` | user → `{status, quiz, draft_answers, cv: {present, text_status}}` |
| PUT | `/api/v1/onboarding/answers` | user (status `not_started`/`in_progress`/`reopened`) |
| POST | `/api/v1/onboarding/submit` | user |
| PUT | `/api/v1/me/cv` | user (also used by the wizard's CV step) |
| GET | `/api/v1/admin/quizzes` | admin |
| POST | `/api/v1/admin/quizzes` | admin → new empty draft |
| POST | `/api/v1/admin/quizzes/{id}/clone` | admin → new draft from any version |
| GET | `/api/v1/admin/quizzes/{id}` | admin, full tree |
| PUT | `/api/v1/admin/quizzes/{id}` | admin, replace whole tree (draft only; 409 `not_draft`) |
| POST | `/api/v1/admin/quizzes/{id}/validate` | admin → `{errors[], warnings[]}` |
| POST | `/api/v1/admin/quizzes/{id}/publish` | admin |
| DELETE | `/api/v1/admin/quizzes/{id}` | admin, draft only |
| GET | `/api/v1/admin/users/{id}/onboarding` | admin, submissions history |
| POST | `/api/v1/admin/users/{id}/onboarding/reopen` | admin |
| GET | `/api/v1/admin/member-reviews?status=&cursor=` | admin |
| GET | `/api/v1/admin/member-reviews/{user_id}` | admin, full review bundle |
| POST | `/api/v1/admin/member-reviews/{user_id}/complete` | admin `{note?}` |
| PATCH | `/api/v1/admin/member-reviews/{user_id}` | admin `{note}` |

Score overrides use the endpoints in §10 with an added `evidence` field.

The whole-tree PUT is deliberate: the builder edits locally and saves atomically, so there are no partial invalid states.

## Done when
- Answers to hidden questions never reach algo.
- Publishing a new version doesn't change existing submissions or scores.
- Frontend and core evaluators agree on all shared test vectors.
- Submitting without a CV and with GitHub sync failing still produces scores within 10 minutes.
- Member review never blocks any member action.
