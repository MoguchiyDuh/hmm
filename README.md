# hmm

Community platform for hackathon teams, projects, events and mentorship.

- **Registration:** GitHub sign-in, branching onboarding quiz, optional CV, per-area skill scores.
- **Hackathons:** a parser discovers events; members enroll, leads claim, one formation round packs everyone into complete teams; members confirm, a reserve fills open seats.
- **Between hackathons:** standalone projects with plans and releases, event attendance, "I want to learn X" mentorship.
- **Points and reputation:** XP needs a second party (confirmations from co-participants, leads, organizers, learners). Peer ratings build reputation per role. Suspicious activity goes to an admin queue.
- **Telegram bot** for notifications and one-tap actions.

## Stack

| Part | Stack |
|---|---|
| core | Python 3.13, FastAPI, SQLAlchemy 2 + psycopg 3, Alembic, procrastinate, PostgreSQL 17, MinIO |
| algo | pure Python package called in-process by core's worker; OpenAI-compatible chat endpoint |
| parser | standalone Python service, pushes events to core |
| bot | aiogram 3 |
| web | React, Vite, TypeScript, TanStack Router + Query, Tailwind + shadcn/ui |
| infra | Dockerfiles, dev Docker Compose; deployment is out of repo scope |

## Repository

```text
contracts/   shared pydantic models (algo interface, imports, notifications, fixtures)
core/        API, worker, DB schema
algo/        scoring, matching, team formation
parser/      event discovery and import
bot/         Telegram bot
web/         SPA incl. admin panel
deploy/      compose, Dockerfile, env template
docs/        the spec: start at docs/README.md
```

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), Docker, Node 24+ with pnpm (frontend only).

```sh
git clone https://github.com/MoguchiyDuh/hmm && cd hmm
uv sync --all-packages
cp deploy/.env.example deploy/.env
docker compose -f deploy/compose.yaml up -d --build
curl localhost:8000/healthz
```

Frontend:

```sh
cd web && pnpm install && pnpm dev
```

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first, then the spec for your group in [docs/](docs/README.md). Work is tracked in [issues](https://github.com/MoguchiyDuh/hmm/issues) by group label and milestone.

## License

[MIT](LICENSE)
