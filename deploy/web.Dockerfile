FROM node:24-bookworm-slim AS builder

WORKDIR /app/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM caddy:2-alpine

COPY --from=builder /app/web/dist /srv
COPY deploy/Caddyfile /etc/caddy/Caddyfile
