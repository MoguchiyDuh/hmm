# 2. Architecture, repo, stack

## 2.1 Runtime

```text
                ┌──────────── web (Caddy) ───────────────┐
 browser ──────►│  /        → static SPA                 │
                │  /api/*   → api:8000                   │
                └────────────────────────────────────────┘
 api ──┬──► postgres (hmm db, procrastinate queue)
       ├──► minio (CV files)
       └──► GitHub OAuth
 worker ─┬─► postgres, minio, GitHub API
         └─► hmm.algo (in-process) ─► OpenAI-compatible LLM API
 bot ────┬─► postgres
         └─► Telegram Bot API (long polling)
```

The Telegram bot (§20) and the event parser (§19) run in-process against the domain modules, never over HTTP.

## 2.2 Repository

```text
hmm/
├── pyproject.toml     # one uv project: package `hmm`, scripts `hmm-worker`, `hmm-bot`
├── alembic.ini
├── src/hmm/           # API, worker, algorithm interface, migrations (§2.5)
├── tests/             # pytest suite, factories, canonical JSON fixtures
├── web/               # SPA (§21)
├── deploy/            # compose files, Dockerfiles, Caddyfile, backups, env templates (§22)
└── docs/              # this specification
```

## 2.3 Stack

| Part | Stack |
|---|---|
| Python | Python 3.13, uv, ruff, ty, Pydantic v2 |
| Server | FastAPI, SQLAlchemy 2 async + psycopg 3, Alembic, procrastinate (Postgres-backed job queue), httpx, pypdf, aioboto3 |
| Algorithm | `hmm.algo`: pure Python, no DB access, no embeddings |
| Bot | aiogram 3, long polling |
| Frontend | §21 |
| Infra | Docker Compose (dev and production), Caddy, PostgreSQL 18, MinIO, S3 backups, release script (§22) |
| LLM | Any OpenAI-compatible chat endpoint; `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Chat only. |

## 2.4 Process model

- `api`: `uvicorn hmm.main:app`, stateless, horizontally scalable.
- `worker`: `hmm-worker`, runs the jobs in §17, including every algo call.
- `bot`: `hmm-bot`, Telegram long polling and notification delivery (§20). One instance.
- `migrate`: one-shot `alembic upgrade head` before `api`, `worker` and `bot` start.

HTTP handlers never call algo, the LLM or the GitHub API. They enqueue jobs and return. The only outbound call from a handler is the OAuth code exchange and profile fetch in the GitHub sign-in callback (§6).

## 2.5 Code layout

```text
src/hmm/
├── app.py             # create_app(): routers, lifespan, health probes
├── main.py            # ASGI entry point: hmm.main:app
├── worker.py          # worker entry point (hmm-worker), job failure alerts
├── bot/               # Telegram bot (hmm-bot), §20
├── common.py          # ids, enums, ErrorCode, Problem, Contract base model
├── models.py          # re-exports every ORM model: `from hmm import models`
├── ticks.py           # periodic sweeps (event_tick, community_tick)
├── export_openapi.py  # prints the OpenAPI document
├── algo/              # Algorithm protocol and I/O models, pack(), get_algorithm() (stub: FakeAlgorithm)
├── migrations/        # alembic, single baseline revision
├── platform/          # shared infrastructure, no business rules
│   ├── config.py      # environment config (§22.2)
│   ├── db.py          # Base, engine, request session, get_or_404, locked
│   ├── security.py    # UserAuth, MemberAuth, AdminAuth
│   ├── errors.py      # ProblemError → RFC 9457 responses
│   ├── pagination.py  # cursor pagination
│   ├── jobs.py        # procrastinate app, JobName, JOBS registry, defer_job, Runtime
│   ├── logs.py        # JSON logging, request ids
│   ├── settings.py    # runtime settings (§24) and their storage
│   ├── algo.py        # algorithm cache, call_algo timeouts
│   ├── storage.py     # object storage
│   ├── maintenance.py # cleanup job
│   ├── audit.py       # record_audit
│   ├── clock.py       # injectable clock
│   ├── schemas.py     # shared API types and refs (user_ref, notification_person)
│   └── models.py      # audit_log, settings, algo_cache
└── <domain>/
    ├── router.py      # HTTP only
    ├── schemas.py     # API models
    ├── models.py      # ORM tables
    ├── jobs.py        # job handlers, if any
    └── <topic>.py     # business logic, split by topic (service.py when there is one topic)
```

Domains: `identity` (§6), `profiles` (§7), `areas` (§8), `onboarding` (§9), `scores` (§10), `catalog` (§11), `formation` (§12), `results` (§13), `recommendations` (§14), `notifications` (§15), `admin` (§16), `feed` (§25), `attendance` (§26), `projects` (§27), `mentorship` (§28).

Rules:
- A domain owns its tables, endpoints and logic. Logic modules hold business rules, queries, locks (§3.4), audit (§3.5) and job enqueueing.
- `router.py` parses input, calls logic modules and returns their response schemas. Domains call each other through logic modules, never through routers.
- Import modules, not names, across domains: `from hmm.catalog import events as catalog_events`, then `catalog_events.create_event(...)`. Inside a domain, `from hmm.D import M` is fine. Module imports tolerate cycles; name imports in a cycle fail at import time.
- ORM models are used as `models.<Name>`; API schemas are imported by name.
- `hmm.algo` imports only `hmm.common`. `hmm.platform` imports no domain except `identity` (sessions, auth) and `notifications.payloads` (shared refs).
- Every job is declared in `platform/jobs.py` (`JobName`, `JOBS`) and bound with `@job(JobName.X)` in a domain `jobs.py` or `ticks.py`.
