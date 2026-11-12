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
│   └── src/hmm_contracts/
│       ├── common.py       # ids, enums, AreaKey, Problem
│       ├── algo.py         # Algorithm protocol + all I/O models
│       ├── formation.py    # placement_bounds(): shared team-count arithmetic
│       ├── imports.py      # EventImport schema incl. people lists (parser → core)
│       ├── notifications.py# notification types + payload models
│       └── fixtures/       # canonical JSON fixtures used by every group
├── core/                   # FastAPI app + worker + alembic
├── algo/                   # hmm_algo: production algorithm package
├── parser/
├── bot/
├── web/                    # pnpm, React SPA
└── deploy/                 # dev compose.yaml, Dockerfiles, .env.example
```

## 2.3 Stack

| Part | Stack |
|---|---|
| Python baseline | Python 3.13, uv workspace, ruff, ty, Pydantic v2 |
| core | FastAPI, SQLAlchemy 2 async + psycopg 3, Alembic, procrastinate (Postgres-backed job queue), httpx, pypdf, aioboto3/minio client |
| algo | pure Python + whatever the group needs (numpy, openai SDK against compat endpoint, etc.). No DB driver, no embeddings. |
| parser | group's choice in Python 3.13; HTTP to core only |
| bot | aiogram 3, httpx, long polling |
| frontend | TypeScript, React, Vite, TanStack Router + Query, openapi-typescript + openapi-fetch, Tailwind + shadcn/ui, Zod, MSW for mocks, pnpm |
| infra | Dockerfiles + dev Docker Compose, PostgreSQL 18, MinIO. Production hosting, TLS, proxy and backups are out of this repo's scope (§22). |
| LLM | any OpenAI-compatible chat endpoint (OpenCode Go key); `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Chat only, no embeddings. |

## 2.4 Process model (core)

- `api` — uvicorn, stateless, horizontally scalable.
- `worker` — `hmm-worker`, runs jobs in §17 including all algo calls.
- `migrate` — one-shot container running `alembic upgrade head` before `api`/`worker` start.

HTTP handlers never call the LLM or GitHub synchronously. They enqueue jobs and return.
