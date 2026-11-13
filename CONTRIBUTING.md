# Contributing

## Teams

| Group | Members | Directory | Label | Spec to read first |
|---|---|---|---|---|
| core | **@MoguchiyDuh**, @tlikulin | `core/` | `group:core` | docs §2–§17, §22, §25–§28 |
| algo | **@qredd121**, @antonzhakov | `algo/` | `group:algo` | §4, §12, §14, §18, §25, §28 |
| parser | @PhK-dev | `parser/` | `group:parser` | §11, §19, §26 |
| bot | @M1goD-creator | `bot/` | `group:bot` | §6, §15, §20, §28 |
| frontend | @Qw1Qer | `web/` | `group:frontend` | §3, §9, §12, §21, §25–§28 |
| everyone | all | `contracts/` | `group:contracts` | §2–§4, §23 |

Team leads are in bold. Group members are code owners of their directory (`.github/CODEOWNERS`) and review its PRs; the lead has the final say.

## Setup

```sh
uv sync --all-packages
cp deploy/.env.example deploy/.env
docker compose -f deploy/compose.yaml up -d --build
```

Frontend: `cd web && pnpm install && pnpm dev` (proxies `/api` to `localhost:8000`).

Checks (CI runs the same; database tests need `TEST_DATABASE_URL` pointing at an empty PostgreSQL 18 database):

```sh
uv run ruff format --check && uv run ruff check && uv run ty check
uv run alembic upgrade head && uv run alembic check
uv run python -m hmm_core.export_openapi --check
uv run pytest -q
```

## Workflow

1. Take an issue: assign yourself. No issue → open one with the **Task** template first.
2. Branch from `main`: `<group>/<issue-number>-<short-slug>`, e.g. `core/42-enrollment-endpoints`.
3. Commit with [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): subject`, scope = group. Examples: `feat(core): add enrollment endpoints`, `fix(bot): escape html in templates`.
4. Open a PR early as a **draft**; link the issue (`Closes #42`).
5. CI must be green. One approval is required, from your team lead or another group's member for cross-group changes.
6. **Squash merge** only. The PR title becomes the commit message, so it must be a Conventional Commit too.
7. `main` is protected: no direct pushes, no force pushes. Keep it always deployable.

Small PRs win: aim for under 400 changed lines. Merge at least once a day; long-lived branches rot fast.

## Contracts and cross-group changes

- `contracts/` is shared by every Python group. Any change there, any change to an API shape another group uses, or any behavior change against `docs/` needs a **Contract change** issue first.
- Every affected group approves in a comment, then the change ships in its own PR.
- Until a real implementation exists, depend on fakes: `hmm_algo.testing.FakeAlgorithm` (core), MSW mocks (web), `uv run hmm-bot-stub-core` and the notification fixtures (bot).

## Code rules

- Python: 3.13, type hints everywhere, `ruff` + `ty` clean, no `print` in services (use `logging`).
- Core code follows the domain layout in docs §2.5; copy the patterns in `core/src/hmm_core/identity/`.
- An API change regenerates `core/openapi.json` (`uv run python -m hmm_core.export_openapi`) in the same PR. A schema change ships an Alembic migration in the same PR.
- Errors from the API are RFC 9457 problem JSON with a stable `code` (docs §3.1).
- No secrets in git. `.env` files are ignored; add new variables to `deploy/.env.example` and `deploy/.env.prod.example`.
- English everywhere: code, API, logs, notifications.

## Communication

- Questions about the spec → comment on the relevant issue, or open a Contract change issue if it changes behavior.
- Blocked → add `status:blocked` and mention who you're waiting for.
