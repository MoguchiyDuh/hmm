FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG PACKAGE
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
COPY contracts contracts
COPY core core
COPY algo algo
COPY parser parser
COPY bot bot
COPY alembic.ini ./
RUN uv sync --locked --no-dev --package ${PACKAGE}

ENV PATH="/app/.venv/bin:$PATH"
