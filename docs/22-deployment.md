# 22. Runtime contract and configuration

Production hosting (servers, TLS, reverse proxy, CI/CD deploys, backups) is handled outside this repo. The repo ships images and a dev compose, and every service honors the contract below.

## 22.1 Services

| Service | Built from | Notes |
|---|---|---|
| `api` | `deploy/python.Dockerfile` (`hmm-core`) | `uvicorn hmm_core.main:app`, port 8000 |
| `worker` | same image | `hmm-worker` |
| `migrate` | same image | `alembic upgrade head`, must finish before `api`/`worker` start |
| `bot` | `deploy/python.Dockerfile` (`hmm-bot`) | outbound only |
| `parser` | `deploy/python.Dockerfile` (`hmm-parser`) | outbound only |
| `web` | `pnpm build` in `web/` | static files; `/api/*` must be proxied to `api` on the same origin |
| `postgres` | `postgres:18` | databases `hmm`, `hmm_parser` |
| `minio` | `minio/minio` | private bucket `hmm`; any S3-compatible store works |

`deploy/compose.yaml` is the dev stack: postgres, minio, migrate, api and worker; `--profile full` adds bot and parser. The web app runs with `pnpm dev` against it.

## 22.2 Environment

```text
PUBLIC_URL
DATABASE_URL
S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY S3_BUCKET
GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET GITHUB_TOKEN
SESSION_SECRET
ADMIN_GITHUB_LOGINS            # comma-separated, bootstrap only (§6)
BOT_SERVICE_TOKEN PARSER_SERVICE_TOKEN
LLM_BASE_URL LLM_API_KEY LLM_MODEL
BOT_TOKEN                      # bot only
PARSER_DATABASE_URL            # parser only
LOG_LEVEL
```

## 22.3 Observability
- Structured JSON logs with `request_id` (from `X-Request-Id` or generated) propagated to jobs.
- `GET /healthz` (process up), `GET /readyz` (DB reachable, migrations at head).
- Job failures and parser heartbeats surface in admin System page. External monitoring isn't required.
