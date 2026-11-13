<div align="center">

# hmm

**Find your team. Win hackathons. Keep building together.**

hmm is a home for a tech community: it finds hackathons for you, puts you in a team that has every skill it needs,<br>
and keeps the team going after the event with projects, mentors and a profile that grows with everything you do.

[![CI](https://github.com/MoguchiyDuh/hmm/actions/workflows/ci.yml/badge.svg)](https://github.com/MoguchiyDuh/hmm/actions/workflows/ci.yml)
[![Release](https://github.com/MoguchiyDuh/hmm/actions/workflows/release.yml/badge.svg)](https://github.com/MoguchiyDuh/hmm/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)
![PostgreSQL 18](https://img.shields.io/badge/postgres-18-4169E1?logo=postgresql&logoColor=white)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

[Features](#features) · [Architecture](#architecture) · [Quick start](#quick-start) · [Development](#development) · [Deployment](#deployment) · [Docs](docs/README.md) · [Contributing](CONTRIBUTING.md)

</div>

---

## Features

| | |
|---|---|
| **Every hackathon in one place** | Events are collected automatically from the web, so you never miss a deadline. |
| **A team in one click** | Come alone or with friends. hmm fills each team with the right mix of people, so no team ends up without a designer or a backend developer. |
| **A profile that speaks for you** | Sign in with GitHub, answer a short quiz, add a CV if you like. Your skills and history grow with every event and project. |
| **Life after the hackathon** | Turn your idea into a real project, invite people, plan milestones and ship releases. |
| **Learn from people who know** | Stuck on something? Ask for help and get matched with a member who can mentor you. |
| **Fair recognition** | Credit comes from the people you actually built with, not from self-promotion. |
| **Telegram, too** | Get updates and answer invites straight from Telegram. |
| **Tools for organizers** | An admin panel to run events, look after members and keep the community healthy. |

## Architecture

```mermaid
flowchart LR
    user([Member / admin]) -->|HTTPS| caddy
    tg([Telegram]) <--> bot

    subgraph stack [Docker Compose]
        caddy[web<br/>Caddy + React SPA] -->|/api| api[api<br/>FastAPI]
        bot[bot<br/>aiogram] -->|/internal| api
        parser[parser<br/>event discovery] -->|/internal| api
        api --> pg[(PostgreSQL 18)]
        worker[worker<br/>jobs + algo] --> pg
        api --> minio[(MinIO<br/>CV files)]
        worker --> minio
        backup[backup] -.-> pg
        backup -.-> minio
    end

    worker -->|chat completions| llm([OpenAI-compatible LLM])
    parser --> sources([Event sources])
    backup -.->|snapshots| s3([External S3])
```

- **api** is stateless and only enqueues work; **worker** runs every background job, including all algorithm calls.
- **bot** and **parser** never touch the database: they call core over HTTP with service tokens.
- **web** is the only public service: Caddy terminates TLS, serves the SPA and proxies `/api`.

| Part | Directory | Stack |
|---|---|---|
| Contracts | [`contracts/`](contracts) | Pydantic v2 models shared by every Python package, canonical JSON fixtures |
| Core | [`core/`](core) | FastAPI, SQLAlchemy 2 async + psycopg 3, Alembic, procrastinate, PostgreSQL 18, MinIO |
| Algorithm | [`algo/`](algo) | Pure Python, called in-process by the worker; any OpenAI-compatible chat endpoint |
| Parser | [`parser/`](parser) | Standalone Python service that pushes events to core |
| Bot | [`bot/`](bot) | aiogram 3, long polling |
| Web | [`web/`](web) | React, TypeScript, Vite, TanStack Router + Query, Tailwind + shadcn/ui |
| Deploy | [`deploy/`](deploy) | Dockerfiles, Compose (dev + production), Caddy, S3 backups, GitHub Actions → GHCR |

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), [Docker](https://docs.docker.com/engine/install/) with Compose, and for the frontend Node 24 + [pnpm](https://pnpm.io/installation).

```sh
git clone https://github.com/MoguchiyDuh/hmm.git
cd hmm
uv sync --all-packages
cp deploy/.env.example deploy/.env
docker compose -f deploy/compose.yaml up -d --build
curl http://localhost:8000/healthz
```

Then start the frontend:

```sh
cd web
pnpm install
pnpm dev
```

| What | Where |
|---|---|
| Web app (dev server) | http://localhost:5173 |
| API | http://localhost:8000/api/v1 |
| OpenAPI docs | http://localhost:8000/docs |
| MinIO console | http://localhost:9001 |
| PostgreSQL | `localhost:5432`, user `hmm` |

GitHub sign-in needs an [OAuth app](https://github.com/settings/developers) with the callback `http://localhost:8000/api/v1/auth/github/callback`; put its id and secret into `deploy/.env`. Bot and parser start with `docker compose -f deploy/compose.yaml --profile full up -d`.

## Development

```sh
uv run ruff format && uv run ruff check     # format and lint
uv run ty check                              # type check
uv run pytest -q                             # all Python tests
uv run alembic upgrade head                  # apply migrations
uv run alembic revision --autogenerate -m "add x"
uv run python -m hmm_core.export_openapi    # regenerate core/openapi.json
```

Database tests need `TEST_DATABASE_URL` pointing at an empty PostgreSQL 18 database, for example:

```sh
docker run -d --name hmm-test-db -e POSTGRES_USER=hmm -e POSTGRES_PASSWORD=hmm -e POSTGRES_DB=hmm_test \
  -p 127.0.0.1:55432:5432 postgres:18
export TEST_DATABASE_URL=postgresql+psycopg://hmm:hmm@127.0.0.1:55432/hmm_test
```

Work against fakes until the real piece exists:

| You build | Fake to use |
|---|---|
| core jobs | `hmm_algo.testing.FakeAlgorithm` |
| bot | `uv run hmm-bot-stub-core` + `contracts/fixtures/notifications` |
| web | MSW mocks from the OpenAPI examples (`pnpm dev:mock`) |

CI runs the same checks on every pull request: `python` (lint, types, migrations, OpenAPI, tests), `web` (typecheck, build) and `compose` (stack config).

### Repository layout

```text
hmm/
├── contracts/   shared models: algo interface, imports, notifications, fixtures
├── core/        API, worker, database schema and migrations
├── algo/        scoring, matching, team formation
├── parser/      event discovery and import
├── bot/         Telegram bot
├── web/         single-page app, including the admin panel
├── deploy/      Dockerfiles, compose files, Caddyfile, backups, env templates
└── docs/        the specification, start at docs/README.md
```

## Deployment

Production runs on any Linux server with Docker; nothing is tied to a provider.

1. Point your domain at the server and open ports 80, 443 (TCP + UDP).
2. Copy [`deploy/compose.prod.yaml`](deploy/compose.prod.yaml) and [`deploy/.env.prod.example`](deploy/.env.prod.example) (as `.env`) to the server and fill in every value.
3. Add the deploy secrets to the GitHub `production` environment.
4. Tag a release:

```sh
git tag v1.0.0
git push origin v1.0.0
```

The [release workflow](.github/workflows/release.yml) builds multi-arch images, pushes them to GHCR and rolls the stack out over SSH. Migrations run before the API starts; Caddy obtains TLS certificates automatically; the `backup` service snapshots both databases and the file bucket to external S3 on a schedule.

Full guide, every environment variable, upgrades, rollback and restore: [docs §22](docs/22-deployment.md).

## Documentation

The [specification](docs/README.md) is the contract between the five groups (core, algo, parser, bot, frontend). Every section has numbered subsections, an endpoints table and a "Done when" list. The delivery plan is in [§23](docs/23-delivery-plan.md).

## Contributing

1. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the spec sections for your group.
2. Pick an [issue](https://github.com/MoguchiyDuh/hmm/issues) from the current [milestone](https://github.com/MoguchiyDuh/hmm/milestones) and assign yourself.
3. Branch, commit with Conventional Commits, open a draft PR, get CI green and one approval.

## License

[MIT](LICENSE)
