FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ARG PACKAGE
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
COPY contracts/pyproject.toml contracts/pyproject.toml
COPY core/pyproject.toml core/pyproject.toml
COPY algo/pyproject.toml algo/pyproject.toml
COPY parser/pyproject.toml parser/pyproject.toml
COPY bot/pyproject.toml bot/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-workspace --package ${PACKAGE}

COPY contracts contracts
COPY core core
COPY algo algo
COPY parser parser
COPY bot bot
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable --package ${PACKAGE}

FROM python:3.13-slim-bookworm

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
