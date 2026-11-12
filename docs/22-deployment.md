# 22. Deployment and configuration

## 22.1 Compose services

| Service | Image | Notes |
|---|---|---|
| `caddy` | caddy | TLS, `/` → web static, `/api/*` → api |
| `api` | core | `uvicorn hmm_core.main:app` |
| `worker` | core | `procrastinate worker` |
| `migrate` | core | `alembic upgrade head`, runs before api/worker |
| `postgres` | pgvector/pgvector:pg17 | volumes; DBs `hmm`, `hmm_parser` |
| `minio` | minio | bucket `hmm` private |
| `bot` | bot | |
| `parser` | parser | |
| `backup` | postgres | nightly `pg_dump` of `hmm` to volume, 14-day retention |

Only `caddy` exposes ports. Internal network for the rest.

## 22.2 Environment

```text
PUBLIC_URL
DATABASE_URL
S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY S3_BUCKET
GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET GITHUB_TOKEN
SESSION_SECRET
ADMIN_GITHUB_LOGINS            # comma-separated
BOT_SERVICE_TOKEN PARSER_SERVICE_TOKEN
LLM_BASE_URL LLM_API_KEY LLM_MODEL EMBEDDING_MODEL
BOT_TOKEN                      # bot only
PARSER_DATABASE_URL            # parser only
LOG_LEVEL
```

## 22.3 Observability
- Structured JSON logs with `request_id` (from `X-Request-Id` or generated) propagated to jobs.
- `GET /healthz` (process up), `GET /readyz` (DB reachable, migrations at head).
- Job failures and parser heartbeats surface in admin System page. External monitoring isn't required.
