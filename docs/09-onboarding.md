# 9. Registration and onboarding

## 9.1 Registration flow

```text
GitHub sign-in ─► account created (onboarding_status=not_started), github_sync enqueued
      │
      ▼
Quiz: general questions ─► conditional branches revealed by earlier answers
      │
      ▼
Optional CV upload (PDF / Markdown / Typst), visible to all members
      │
      ▼
Submit ─► full access immediately
      │      score_onboarding(quiz answers + CV text + GitHub summary) ─► initial area scores
      ▼
Admin "New members" queue (non-blocking): review, optionally adjust scores with evidence
```

- Until onboarding is submitted, the user can only call `user` endpoints (§3.3).
- The CV step is part of the wizard but optional and skippable. It uses the same endpoint as §7.3 and can be added or replaced later in settings.
- Submitting never waits for GitHub sync or CV extraction. `score_onboarding` runs as a job once both have settled (GitHub sync finished or failed; CV extraction finished, failed or skipped), or after 10 minutes at most, using whatever is available. Later GitHub and CV changes flow through `recompute_member`.
- The profile shows "Scores calculating…" until the first result lands.

## 9.2 Quiz model

- Quizzes are versioned. Exactly one version is `published`; only `draft` versions are editable. Publishing freezes a draft and archives the previously published version.
- Submissions reference the version they were made against, so old answers are never reinterpreted.
- With no published version (fresh install), `GET /api/v1/onboarding` returns `quiz: null` and the web shows "Onboarding opens soon". Admin routes don't require onboarding, so an admin can publish the first quiz.
- Questions are grouped into **sections** (`section` field). General sections come first; conditional sections and questions appear through `visible_if` (§9.3).
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
- The quiz is edited as a whole tree: `PUT /api/v1/admin/quizzes/{id}` replaces the draft atomically.

## 9.3 Visibility conditions (`visible_if`)

A JSON tree evaluated against answers to questions **positioned earlier**:

```json
{ "all": [
  { "q": "languages", "op": "includes", "value": "python" },
  { "any": [
    { "q": "directions", "op": "includes", "value": "ml_ai" },
    { "q": "directions", "op": "includes", "value": "backend" }
  ]}
]}
```

- Operators: `eq`, `neq`, `includes`, `excludes`, `includes_any` (value is a list), `gte`, `lte` (scale and number), `answered`, `not_answered`.
- Combinators: `all`, `any`, `not`. Max depth 5.
- A section can carry `visible_if` too; it applies to all its questions.

Validation on save and publish (`/validate` returns every issue):

| Check | Level |
|---|---|
| Referenced question exists and is positioned earlier; option keys exist; operator fits the question type | error |
| A required question can never become visible | warning |
| Some non-archived area has no option with `area_hints` for it | warning |

Core implements the evaluator once in Python, and the frontend implements the same evaluator in TypeScript for live branching. Both must pass the shared test vectors in `contracts/fixtures/visibility_cases.json`.

## 9.4 Submission

1. `not_started` → the user opens `/onboarding` and gets the published definition; the first save sets `in_progress`.
2. Drafts autosave (`PUT answers`, partial allowed). A draft made against an older version moves to the current published version when it is loaded or saved, keeping answers whose question keys still exist.
3. `submit` validates against the current published version: core recomputes visibility, drops answers to hidden questions, and checks that every visible required question is answered with a valid value. Otherwise 422 with per-question errors.
4. On success: the submission becomes `submitted`, the user `completed`, and `score_onboarding(user)` is enqueued (§9.1 wait rule). Its output is written to `area_scores.calculated` with score events; then `recompute_member` and `compute_lead_fits_for_member` are enqueued. A member review is opened (§9.5).
5. Retake only through an admin `reopen`: status becomes `reopened`, the user gets `onboarding_reopened`, and sees the **current** published version with previous answers pre-filled where question keys match.
   - The user keeps full member access until they resubmit; reopen is not a lock.
   - Resubmitting replaces the onboarding baseline and reopens the member review. Admin overrides are untouched.

## 9.5 Admin member review (non-blocking)

- Each completed onboarding (and each resubmission) creates or reopens a `member_reviews` row with status `pending`.
- The review screen shows quiz answers, CV (text and file), GitHub summary, calculated scores with reasons, and **discrepancy hints** from `score_onboarding` (e.g. "quiz claims ML 8/10, no ML evidence in GitHub or CV").
- Admin actions:
  - set or remove area overrides, each with `reason` and `evidence` (`interview`, `github_review`, `cv_review`, `other`);
  - add a private admin note;
  - mark `reviewed`, with or without changes.
- Nothing waits on this. Unreviewed members can use everything.
- Once reviewed, the profile shows a "Scores reviewed by admin" badge with the date, and the member gets `scores_reviewed`. Algo receives `scores_reviewed_at` and may weigh reviewed scores as more reliable.
- The dashboard shows the pending count. Items older than `member_review_reminder_days` (7) appear in the weekly admin digest; there are no per-item alerts.

## 9.6 Endpoints

| Method | Path | Who | Body → response |
|---|---|---|---|
| GET | `/api/v1/onboarding` | user | → `{status, quiz, draft_answers, cv: {present, text_status}}`; `quiz` is null with no published version |
| PUT | `/api/v1/onboarding/answers` | user with status `not_started`, `in_progress` or `reopened` | Partial answers |
| POST | `/api/v1/onboarding/submit` | user | – |
| PUT | `/api/v1/me/cv` | user | Same endpoint as §7.6 |
| GET | `/api/v1/admin/quizzes` | admin | Versions |
| POST | `/api/v1/admin/quizzes` | admin | → new empty draft |
| POST | `/api/v1/admin/quizzes/{id}/clone` | admin | → new draft from any version |
| GET | `/api/v1/admin/quizzes/{id}` | admin | Full tree |
| PUT | `/api/v1/admin/quizzes/{id}` | admin | Whole tree; draft only, else 409 `not_draft` |
| POST | `/api/v1/admin/quizzes/{id}/validate` | admin | → `{errors[], warnings[]}` |
| POST | `/api/v1/admin/quizzes/{id}/publish` | admin | – |
| DELETE | `/api/v1/admin/quizzes/{id}` | admin | Draft only |
| GET | `/api/v1/admin/users/{id}/onboarding` | admin | Submission history |
| POST | `/api/v1/admin/users/{id}/onboarding/reopen` | admin | – |
| GET | `/api/v1/admin/member-reviews?status=&cursor=` | admin | Queue |
| GET | `/api/v1/admin/member-reviews/{user_id}` | admin | Full review bundle |
| POST | `/api/v1/admin/member-reviews/{user_id}/complete` | admin | `{note?}` |
| PATCH | `/api/v1/admin/member-reviews/{user_id}` | admin | `{note}` |

Score overrides use the §10.5 endpoints with `evidence`.

## 9.7 Done when

- Answers to hidden questions never reach algo.
- Publishing a new version doesn't change existing submissions or scores.
- Frontend and core evaluators agree on all shared test vectors.
- Submitting without a CV and with GitHub sync failing still produces scores within 10 minutes.
- Member review never blocks any member action.
