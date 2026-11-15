<div align="center">

# hmm

**Find your team. Win hackathons. Keep building together.**

hmm is a home for a tech community: it finds hackathons for you, puts you in a team that has every skill it needs,<br>
and keeps the team going after the event with projects, mentors and a profile that grows with everything you do.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)
![PostgreSQL 18](https://img.shields.io/badge/postgres-18-4169E1?logo=postgresql&logoColor=white)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

[Features](#features) · [Architecture](#architecture) · [Quick start](#quick-start) · [Development](#development) · [Deployment](#deployment) · [Docs](docs/README.md)

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

    subgraph stack [Docker Compose]
        caddy[web<br/>Caddy + SPA] -->|/api| api[api<br/>FastAPI]
        api --> pg[(PostgreSQL 18)]
        worker[worker<br/>jobs + algo] --> pg
        api --> minio[(MinIO<br/>CV files)]
        worker --> minio
        backup[backup] -.-> pg
        backup -.-> minio
    end

    worker -->|chat completions| llm([OpenAI-compatible LLM])
    backup -.->|snapshots| s3([External S3])
```

- **api** is stateless and only enqueues work; **worker** runs every background job, including all algorithm calls.
- The Telegram bot and the event parser run in-process against the domain modules; there is no internal HTTP API.
- **web** is the only public service: Caddy terminates TLS, serves the SPA and proxies `/api`.

| Part | Location | Stack |
|---|---|---|
| App | [`src/hmm/`](src/hmm) | FastAPI, SQLAlchemy 2 async + psycopg 3, Alembic, procrastinate, PostgreSQL 18, MinIO |
| Algorithm | [`src/hmm/algo/`](src/hmm/algo) | Pure Python interface and fakes, called in-process by the worker |
| Tests | [`tests/`](tests) | pytest + xdist against PostgreSQL, canonical JSON fixtures |
| Web | [`web/`](web) | SPA |
| Deploy | [`deploy/`](deploy) | Dockerfiles, Compose (dev + production), Caddy, S3 backups |

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Docker](https://docs.docker.com/engine/install/) with Compose.

```sh
cd hmm
uv sync
cp deploy/.env.example deploy/.env
docker compose -f deploy/compose.yaml up -d --build
curl http://localhost:8000/healthz
```

| What | Where |
|---|---|
| API | http://localhost:8000/api/v1 |
| OpenAPI docs | http://localhost:8000/docs |
| MinIO console | http://localhost:9001 |
| PostgreSQL | `localhost:5432`, user `hmm` |

GitHub sign-in needs an [OAuth app](https://github.com/settings/developers) with the callback `http://localhost:8000/api/v1/auth/github/callback`; put its id and secret into `deploy/.env`.

## Development

```sh
uv run ruff format && uv run ruff check     # format and lint
uv run ty check                              # type check
uv run pytest -q                             # full suite, parallel
uv run pytest -q -n0 tests/test_x.py -k name # one test, no workers
uv run alembic upgrade head                  # apply migrations
uv run alembic revision --autogenerate -m "add x"
uv run python -m hmm.export_openapi > openapi.json
```

Database tests need `TEST_DATABASE_URL` pointing at a PostgreSQL 18 server. The suite migrates a template database only when migration files change and clones it per worker:

```sh
docker run -d --name hmm-test-db -e POSTGRES_USER=hmm -e POSTGRES_PASSWORD=hmm -e POSTGRES_DB=hmm_test \
  -p 127.0.0.1:55432:5432 postgres:18
export TEST_DATABASE_URL=postgresql+psycopg://hmm:hmm@127.0.0.1:55432/hmm_test
```

The worker uses `hmm.algo.get_algorithm()`, currently a stub returning `hmm.algo.fake.FakeAlgorithm`. Shared test builders live in `tests/factories.py`, the harness in `tests/harness.py`.

Before committing, also check the migration round-trip and the compose files when they change:

```sh
uv run alembic upgrade head && uv run alembic check
uv run alembic downgrade base && uv run alembic upgrade head
docker compose -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/.env.prod.example -f deploy/compose.prod.yaml config --quiet
```

Commits follow `type(scope): subject`; schema changes ship with a migration.

### Repository layout

```text
hmm/
├── src/hmm/     API, worker, algorithm interface, migrations (docs §2.5)
├── tests/       suite, factories, harness, JSON fixtures
├── web/         single-page app
├── deploy/      Dockerfiles, compose files, Caddyfile, backups, env templates
└── docs/        the specification, start at docs/README.md
```

## Deployment

Production runs on any Linux server with Docker; nothing is tied to a provider.

1. Point your domain at the server and open ports 80, 443 (TCP + UDP).
2. Copy [`deploy/compose.prod.yaml`](deploy/compose.prod.yaml) and [`deploy/.env.prod.example`](deploy/.env.prod.example) (as `.env`) to the server and fill in every value.
3. Build and push the images to your registry (`IMAGE_PREFIX` in `.env`), then start the stack:

```sh
IMAGE_PREFIX=registry.example.com/hmm IMAGE_TAG=v1.0.0 ./deploy/release.sh
ssh server 'cd /srv/hmm && IMAGE_TAG=v1.0.0 docker compose -f compose.prod.yaml up -d --remove-orphans --wait'
```

Migrations run before the API starts; Caddy obtains TLS certificates automatically; the `backup` service snapshots the database and the file bucket to external S3 on a schedule.

Full guide, every environment variable, upgrades, rollback and restore: [docs §22](docs/22-deployment.md).

## Documentation

The [specification](docs/README.md) has numbered sections, each with an endpoints table and a "Done when" list. The delivery plan is in [§23](docs/23-delivery-plan.md).

## License

[MIT](LICENSE)
