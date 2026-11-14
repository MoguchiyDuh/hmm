# 22. Deployment and configuration

The repo ships everything needed to run hmm in production on any Linux server with Docker: images, a production compose file, TLS termination, backups and a release workflow. Nothing in it depends on a particular provider or server size.

## 22.1 Services

| Service | Image | Notes |
|---|---|---|
| `postgres` | `postgres:18` | databases `hmm` (core and job queue) and `hmm_parser` |
| `minio` | `MINIO_IMAGE` | private bucket `S3_BUCKET`; any S3-compatible store works |
| `minio-init` | `MINIO_IMAGE` | one-shot: creates the bucket and keeps it private |
| `migrate` | `hmm-core` | one-shot `alembic upgrade head`; `api` and `worker` start after it succeeds |
| `api` | `hmm-core` | `uvicorn hmm_core.main:app`, port 8000, healthy when `/readyz` passes |
| `worker` | `hmm-core` | `hmm-worker` (§17) |
| `bot` | `hmm-bot` | outbound only |
| `parser` | `hmm-parser` | outbound only |
| `web` | `hmm-web` | Caddy: TLS, static SPA, `/api/*` → `api:8000`; the only service with published ports |
| `backup` | `hmm-backup` | scheduled dumps of both databases and the bucket to an external S3-compatible target |

Files:

| File | Purpose |
|---|---|
| `deploy/compose.yaml` | development stack; `--profile full` adds bot and parser |
| `deploy/compose.prod.yaml` | production stack, runs published images |
| `deploy/python.Dockerfile` | one image per Python package (`--build-arg PACKAGE=hmm-core`, `hmm-bot`, `hmm-parser`), non-root |
| `deploy/web.Dockerfile` | builds `web/` and packages it with Caddy and `deploy/Caddyfile` |
| `deploy/backup/` | backup image, `backup.sh`, `restore.sh` |
| `deploy/.env.example` | development environment |
| `deploy/.env.prod.example` | production environment template |
| `.github/workflows/release.yml` | builds and publishes images on a `v*` tag, then deploys |

`/healthz` and `/readyz` are reachable only inside the compose network. Postgres, MinIO and the API are never published.

## 22.2 Environment

Application:

| Variable | Used by | Meaning |
|---|---|---|
| `PUBLIC_URL` | api, worker, bot | Public HTTPS origin, no trailing slash. Cookies, `Origin` checks, error `type` URLs and OAuth callbacks derive from it. |
| `DATABASE_URL` | api, worker, migrate | Core database URL |
| `S3_ENDPOINT` `S3_ACCESS_KEY` `S3_SECRET_KEY` `S3_BUCKET` | api, worker, minio, backup | Object storage for CV files |
| `GITHUB_CLIENT_ID` `GITHUB_CLIENT_SECRET` | api | GitHub OAuth app; callback `PUBLIC_URL/api/v1/auth/github/callback` |
| `GITHUB_TOKEN` | worker | GitHub API token for profile sync |
| `SESSION_SECRET` | api | Signs the OAuth state cookie |
| `ADMIN_GITHUB_LOGINS` | api | Comma-separated bootstrap admins (§6.3) |
| `BOT_SERVICE_TOKEN` `PARSER_SERVICE_TOKEN` | api, bot, parser | Service tokens (§3.2) |
| `TELEGRAM_BOT_USERNAME` | api | Bot username for Telegram deep links (§6.5) |
| `ALGORITHM` | worker | `fake` selects `hmm_algo.testing.FakeAlgorithm` for local development only; unset in production (§18.4) |
| `LLM_BASE_URL` `LLM_API_KEY` `LLM_MODEL` | worker, parser | OpenAI-compatible chat endpoint |
| `CORE_URL` | bot, parser | Internal API URL, `http://api:8000` |
| `BOT_TOKEN` | bot | Telegram bot token |
| `PARSER_DATABASE_URL` | parser | Parser state database URL |
| `LOG_LEVEL` | all | Log level, default `INFO` |

Production stack:

| Variable | Default | Meaning |
|---|---|---|
| `IMAGE_PREFIX` | required | Image name prefix, e.g. `ghcr.io/<owner>/hmm` |
| `IMAGE_TAG` | `latest` | Release tag to run |
| `MINIO_IMAGE` | `cgr.dev/chainguard/minio:latest` | MinIO server and client image |
| `DOMAIN` | required | Site hostname |
| `ACME_EMAIL` | required | Certificate account email |
| `HTTP_PORT` `HTTPS_PORT` | `80` `443` | Published ports (HTTPS also on UDP for HTTP/3) |
| `POSTGRES_PASSWORD` | required | Must match the passwords in `DATABASE_URL` and `PARSER_DATABASE_URL` |
| `BACKUP_S3_ENDPOINT` `BACKUP_S3_BUCKET` `BACKUP_S3_ACCESS_KEY` `BACKUP_S3_SECRET_KEY` | required | Backup target, separate from `S3_BUCKET` |
| `BACKUP_S3_REGION` | `us-east-1` | Signing region of the backup target |
| `BACKUP_S3_PREFIX` | `hmm` | Snapshot prefix in the backup bucket |
| `BACKUP_SCHEDULE` | `0 3 * * *` | Cron expression |
| `BACKUP_TIMEZONE` | `UTC` | Timezone of the schedule |
| `BACKUP_RETENTION_DAYS` | `30` | Snapshots older than this are deleted |
| `LOG_MAX_SIZE` `LOG_MAX_FILES` | `10m` `3` | Container log rotation |
| `<SERVICE>_MEMORY` `<SERVICE>_CPUS` | see `deploy/.env.prod.example` | Limits for `POSTGRES`, `MINIO`, `INIT`, `MIGRATE`, `API`, `WORKER`, `BOT`, `PARSER`, `WEB`, `BACKUP` |

Release workflow secrets, in the GitHub `production` environment: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_PATH`.

## 22.3 First deployment

1. Server: Docker Engine with the Compose plugin, SSH access for a deployment user in the `docker` group, a deployment directory (`DEPLOY_PATH`).
2. DNS: `DOMAIN` points at the server. Firewall allows TCP 80 and 443, UDP 443 and SSH.
3. Create the backup bucket and credentials (upload, list, download, delete) at any S3-compatible provider.
4. Create the GitHub OAuth app with the callback URL from §22.2 and the Telegram bot.
5. Copy the stack and the environment template:
   ```sh
   scp deploy/compose.prod.yaml "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH/compose.prod.yaml"
   scp deploy/.env.prod.example "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH/.env"
   ```
6. On the server, fill in `.env` (`chmod 600 .env`), replacing every placeholder with real values and random secrets, then validate and log in to the registry:
   ```sh
   docker compose -f compose.prod.yaml config --quiet
   docker login ghcr.io
   ```
7. Add the release secrets to the GitHub `production` environment.
8. Tag a release: `git tag v1.0.0 && git push origin v1.0.0`. The workflow builds amd64 and arm64 images, pushes them to GHCR, copies `compose.prod.yaml` to the server and runs the stack with that tag.

## 22.4 Upgrades and rollback

- Upgrade: push a new `v*` tag. Migrations run before `api` and `worker` start.
- Manual run of a published release:
  ```sh
  export IMAGE_TAG=v1.2.3
  docker compose -f compose.prod.yaml pull
  docker compose -f compose.prod.yaml up -d --remove-orphans --wait
  ```
- Rollback: the same commands with the previous tag. A release whose migration changed the schema is rolled back by restoring the backup taken before it (§22.5).

## 22.5 Backups and restore

- `backup` runs on `BACKUP_SCHEDULE`: `pg_dump -Fc` of `hmm` and `hmm_parser` and a mirror of `S3_BUCKET`, uploaded as one timestamped snapshot; snapshots older than `BACKUP_RETENTION_DAYS` are pruned.
- Scheduled snapshots are taken live. A consistent snapshot across databases and files stops writers first:
  ```sh
  docker compose -f compose.prod.yaml stop web api worker bot parser backup
  docker compose -f compose.prod.yaml run --rm --no-deps backup backup
  docker compose -f compose.prod.yaml up -d --wait
  ```
- Restore replaces both databases and synchronizes the bucket to the snapshot:
  ```sh
  docker compose -f compose.prod.yaml stop web api worker bot parser backup
  docker compose -f compose.prod.yaml run --rm --no-deps --entrypoint bash backup \
    /usr/local/bin/restore.sh <timestamp>
  docker compose -f compose.prod.yaml up -d --wait
  ```

## 22.6 Observability

- Structured JSON logs with `request_id` (from `X-Request-Id` or generated) propagated to jobs; rotated by Docker.
- `GET /healthz` (process up), `GET /readyz` (DB reachable, migrations at head).
- Job failures and parser heartbeats surface in the admin System page.
