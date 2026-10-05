# syntax=docker/dockerfile:1
# The web image: the React app built to web/dist, served by Caddy with the
# proxy to the api in front. Built from the repository root:
#   docker build -f deploy/web.Dockerfile .

# ── build: web/dist, the same build CI runs, size budget included ────────────
FROM node:24.21.0-trixie-slim AS build
RUN npm install --global pnpm@12.9.1
WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ── run: Caddy with the built app under /srv/ui ──────────────────────────────
FROM caddy:2.11.4-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /web/dist /srv/ui
