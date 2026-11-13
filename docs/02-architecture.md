# 2. Architecture, repo, stack

## 2.1 Runtime

```text
                ┌──────── reverse proxy (out of repo) ───────┐
 browser ──────►│  /        → web static files               │
                │  /api/*   → api:8000                        │
                └───────────────────────────────────────────┘
 bot ───────────► api:8000  (/api/v1 acting as user, /internal)
 parser ────────► api:8000  (/internal)
 api ──┬──► postgres (core db, procrastinate queue)
       ├──► minio (CV files)
       └──► GitHub OAuth
 worker ─┬─► postgres, minio, GitHub API
         └─► algo package (in-process) ─► OpenAI-compatible LLM API
 parser ──► own state db (hmm_parser) + internet + LLM API
```

## 2.2 Repository

```text
hmm/
├── pyproject.toml          # uv workspace: contracts, core, algo, parser, bot
├── contracts/              # pydantic models shared across python groups
│   ├── src/hmm_contracts/
│   │   ├── common.py       # ids, enums, AreaKey, Problem
│   │   ├── algo.py         # Algorithm protocol + all I/O models
│   │   ├── formation.py    # placement_bounds(): shared team-count arithmetic
│   │   ├── imports.py      # EventImport schema incl. people lists (parser → core)
│   │   └── notifications.py# notification types + payload models
│   └── fixtures/           # canonical JSON fixtures used by every group
├── core/                   # FastAPI app + worker + alembic (§2.5)
├── algo/                   # hmm_algo: production algorithm package
├── parser/
├── bot/
├── web/                    # pnpm, React SPA
└── deploy/                 # compose files, Dockerfiles, Caddyfile, backups, env templates (§22)
```

## 2.3 Stack

| Part | Stack |
|---|---|
| Python baseline | Python 3.13, uv workspace, ruff, ty, Pydantic v2 |
| core | FastAPI, SQLAlchemy 2 async + psycopg 3, Alembic, procrastinate (Postgres-backed job queue), httpx, pypdf, aioboto3/minio client |
| algo | Pure Python plus whatever the group needs (numpy, openai SDK against a compatible endpoint, etc.). No DB driver, no embeddings. |
| parser | Group's choice in Python 3.13; HTTP to core only |
| bot | aiogram 3, httpx, long polling |
| frontend | TypeScript, React, Vite, TanStack Router + Query, openapi-typescript + openapi-fetch, Tailwind + shadcn/ui, Zod, MSW for mocks, pnpm |
| infra | Dockerfiles + dev Docker Compose, PostgreSQL 18, MinIO. Production hosting, TLS, proxy and backups are out of this repo's scope (§22). |
| LLM | Any OpenAI-compatible chat endpoint; `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Chat only, no embeddings. |

## 2.4 Process model (core)

- `api`: uvicorn, stateless, horizontally scalable.
- `worker`: `hmm-worker`, runs the jobs in §17, including every algo call.
- `migrate`: one-shot container running `alembic upgrade head` before `api` and `worker` start.

HTTP handlers never call algo, the LLM or the GitHub API. They enqueue jobs and return. The only outbound call from a handler is the OAuth code exchange and profile fetch in the GitHub sign-in callback (§6).

## 2.5 Core code layout

```text
core/src/hmm_core/
├── app.py               # create_app(): routers, lifespan, health probes
├── main.py              # ASGI entry point: hmm_core.main:app
├── models.py            # every ORM model, imported as `from hmm_core import models`
├── export_openapi.py    # writes or checks core/openapi.json
├── migrations/          # alembic
├── platform/            # shared infrastructure, no business rules
│   ├── config.py        # environment config (§22.2)
│   ├── db.py            # Base, timestamps, engine, request session
│   ├── security.py      # auth dependencies: UserAuth, MemberAuth, AdminAuth, BotAuth, ParserAuth
│   ├── errors.py        # ProblemError → RFC 9457 responses
│   ├── pagination.py    # cursor pagination
│   ├── jobs.py          # procrastinate app, JobName, defer_job, hmm-worker
│   ├── settings.py      # runtime settings (§24) and their storage
│   ├── audit.py         # record_audit
│   ├── clock.py         # injectable clock
│   ├── schemas.py       # shared API types and refs (UserRef, EventRef, text limits)
│   ├── models.py        # audit_log, settings, algo_cache
│   └── openapi.py       # OpenAPI generation
└── <domain>/
    ├── router.py
    ├── schemas.py
    ├── models.py
    └── service.py
```

Domains: `identity` (§6), `profiles` (§7), `areas` (§8), `onboarding` (§9), `scores` (§10), `catalog` (§11), `formation` (§12), `results` (§13), `recommendations` (§14), `notifications` (§15), `admin` (§16), `feed` (§25), `attendance` (§26), `projects` (§27), `mentorship` (§28).

Rules:
- One domain per spec section group; a domain owns its tables, endpoints and logic.
- `router.py` handles HTTP only: parse input, call the service, build the response. It declares `router` (mounted at `/api/v1`) and `internal` (mounted at `/api/v1/internal`).
- `service.py` holds business rules, queries, locks (§3.4), audit (§3.5) and job enqueueing. A domain calls another domain through that domain's service, never through its router.
- Routers import services as modules named after the domain (`from hmm_core.identity import service as identity`, then `identity.sign_in(...)`), so handler names never clash with service functions.
- `schemas.py` holds the API models for the domain's endpoints; they extend `ApiModel` and are the OpenAPI contract.
- `models.py` holds the domain's ORM tables. Code outside `models.py` uses them as `models.<Name>`; API schemas are imported by name.
- `identity/` is the reference implementation of these rules.
