# Kaggle

Probed 2026-11-15. Adapter: `kaggle`. Kinds: contest (some titled "hackathon").

## Access

- Official API, RPC-style: `POST https://api.kaggle.com/v1/competitions.CompetitionApiService/ListCompetitions`, called through the `kaggle` SDK 2.2.5 (`KaggleApi().competitions_list(...)`).
- Auth: account API token in `KAGGLE_API_TOKEN` (new-style `KGAT_…` token, Bearer). Legacy `kaggle.json` username/key also accepted by the SDK.
- `kaggle` 2.2.5 imports `six` without declaring it: install with `--with six` or add it as a dependency.
- Alternative without a token: the site's internal endpoint `POST https://www.kaggle.com/api/i/competitions.CompetitionService/ListCompetitions` with `{"pageSize":100,"listOption":"LIST_OPTION_ACTIVE"}` answers anonymously (survey, not re-probed) and adds `totalCompetitors`, `numPrizes`, `rulesRequired`. Its `sortBy` and `listOption` were ignored, so filter client-side.

## Adapter re-probe (2026-11-15)

- Official RPC body uses the SDK's enum names: `{"group": "COMPETITION_LIST_TAB_GENERAL" | "COMPETITION_LIST_TAB_COMMUNITY", "sortBy": "COMPETITION_SORT_BY_PRIZE", "pageSize": 20, "page": N, "pageToken": …}`. Lower-case `group` values are silently ignored. `general` = 20 rows, no next token. `community` returns no `nextPageToken` either, but legacy `page` works; sorted by prize it is 20 cash rows per page down to 1,000 Usd by page 8, mixed with ended competitions.
- Anonymous internal endpoint (`{"pageSize": 100, "listOption": "LIST_OPTION_ACTIVE"}`, browser UA, no cookie or XSRF needed) returns **762** rows of all history sorted by prize, `nextPageToken` = offset (`"100"`, `"200"`, …), 270–390 KB per page. It omits community competitions. `reward` is `{id: "USD", quantity: 400000}`; slug is `competitionName`; new-entrant deadline is `prohibitNewEntrantsExplicitDeadline`. A `selector` wrapper body is rejected with 400.

## Discovery

`competitions_list(group, category, sort_by, page, search, page_size, page_token)`.

| param | values | observed |
|---|---|---|
| `group` | `general`, `entered`, `community`, `hosted`, `unlaunched`, `unlaunched_community` | `inClass` from the CLI docs is rejected by the SDK |
| `category` | `all`, `featured`, `research`, `recruitment`, `gettingStarted`, `masters`, `playground` | |
| `sort_by` | `grouped`, `prize`, `earliestDeadline`, `latestDeadline`, `numberOfTeams`, `recentlyCreated` | honored |
| `page_size` / `page_token` | ≤ 20 effective | `page_size=200` still returns 20; the token pages further |
| `page` | legacy paging | without a group, returns the whole history (2013+), sorted |
| `search` | free text | `search="hackathon"` returns hackathon-titled competitions across groups |

- `group=general` returns only active competitions: 20 today, one page, no next token. 6 Featured, 3 Playground, 11 Getting Started.
- `group=community` returns user-hosted competitions sorted with junk first (deadlines in 2100–2222, class assignments, "Kudos" rewards). Real prize events hide there, e.g. `soccer-feature-design-hackathon` (10,000 Usd, ends 2027-02-17, teams ≤ 5, created 2026-11-07).
- Cheapest full pass: `general` (1 call) + `community` paged with `sort_by=recentlyCreated` until `dateCreated` is older than the last run.

## Fields

| source field | example | maps to (§19.3) | notes |
|---|---|---|---|
| `id` | `179751` | `source_id` `kaggle:179751` | stable integer |
| `ref` / `url` | `https://www.kaggle.com/competitions/gemma-4-developer-agent` | `website_url`, `registration_url` | slug is the CLI handle |
| `title` | `Soccer Feature Design Hackathon` | `title` | |
| `description` | `Turning Play-by-Play From Two Data Providers…` | `summary` | one line; no long description in the list |
| `organizationName` / `hostName` | `Google DeepMind` / `Kaggle` | `organizer` | `organizationName` empty on community competitions |
| `category` | `Featured`, `Playground`, `Getting Started`, `Community` | filter, `kind` = `contest` | |
| `enabledDate` | `2026-11-10T17:01:56.370Z` | `starts_at` | launch time, UTC |
| `deadline` | `2027-02-17T23:59:00.000Z` | `ends_at` | final submission, UTC |
| `newEntrantDeadline` | `2026-11-25` (gemma-4-developer-agent) | `registration_deadline` | absent on Getting Started and some community ones |
| `mergerDeadline` | `2027-02-17T23:59:00.000Z` | none | team merge cut-off; could go into `description_md` |
| `maxTeamSize` | `5` | `team_size_max` | `1` = individual; values up to 20 seen |
| `reward` | `75,000 Usd`, `3 Swag`, `Knowledge`, `Kudos` | `prize` | parse `<amount> <currency>`; non-cash values → no prize |
| `teamCount` | `5808` | none | popularity signal |
| `tags` | `{name, fullPath, …}` | `themes` | verbose objects; keep `name` |
| `evaluationMetric`, `isKernelsSubmissionsOnly`, `maxDailySubmissions`, `licenseName`, `awardsPoints`, `dateCreated` | | none | `dateCreated` useful for incremental community scans |

Missing: start of registration, location (always online), language, eligibility (lives in rules page, not in the API), team minimum.

## Revisits and identity

- `source_id` = `kaggle:<id>`. Deadlines change by re-listing; there is no ETag or modified timestamp.
- A finished competition drops out of `general`; it stays fetchable through `search` and legacy `page` paging.
- Results: `competition_leaderboard_view(slug)` returns the top 20 entries (`teamId`, `teamName`, `score`, `submissionDate`), no members. Full public leaderboard via `competition_leaderboard_download` (CSV, untested). Writeup-judged hackathons (e.g. `gemini-3`) return an empty leaderboard.

## Rate and failure behavior

- HTTP 429 after about 10 calls in quick succession; 3–5 s spacing was enough afterwards.
- The SDK prints `Next Page Token = …` to stdout on every paged call; redirect stdout when running inside the worker.

## Volume and relevance

- Global, online-only, no Serbia angle.
- Worth importing today: 6 Featured with 50,000–400,000 Usd prizes, plus a handful of cash community competitions. Getting Started (deadline 2030) and Knowledge/Kudos/Swag rewards are noise.

## Quirks

- `page_size` is capped at 20 silently.
- Community deadlines in year 2100+ are placeholders for "never".
- CLI docs list `inClass` as a group; the SDK rejects it.

## Config sketch

```toml
[[source]]
id = "kaggle"
adapter = "kaggle"
interval = "24h"
rate = "12/min"
secret_env = ["KAGGLE_API_TOKEN"]
groups = ["general", "community"]
skip_categories = ["Getting Started"]
require_cash_reward = true
```
