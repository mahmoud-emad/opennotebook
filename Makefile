UI_DIR := crates/opennotebook_ui

.PHONY: all build build-server build-ui run lint fmt test clean \
	dev migrate api-client check check-server check-web fmt-new \
	up down e2e

# The new stack (docs/stack-migration-plan.md): FastAPI in server/, the React
# app in web/. Database settings come from DATABASE_URL and TEST_DATABASE_URL,
# read from this file when it exists.
ENV_FILE ?= $(HOME)/.config/opennotebook/db.env
-include $(ENV_FILE)
export DATABASE_URL TEST_DATABASE_URL
UV ?= uv
PNPM ?= pnpm

all: build

## build: the server and the web UI, release
build: build-server build-ui

build-server:
	cargo build --release -p opennotebook_server

# The UI is a wasm crate outside the workspace; dx builds it, and install.sh
# makes its asset refs relative and puts it where the server serves /ui/.
build-ui:
	@command -v dx >/dev/null || { echo "dx not found: cargo install dioxus-cli"; exit 1; }
	cd $(UI_DIR) && ./install.sh

## run: build and start the server
run:
	cargo run --release -p opennotebook_server

## lint: formatting and clippy, both workspaces
lint:
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings
	cd $(UI_DIR) && cargo fmt --check
	cd $(UI_DIR) && cargo clippy --target wasm32-unknown-unknown -- -D warnings

fmt:
	cargo fmt --all
	cd $(UI_DIR) && cargo fmt

## test: both workspaces
test:
	cargo test --workspace
	cd $(UI_DIR) && cargo test

clean:
	cargo clean
	cd $(UI_DIR) && cargo clean

# ── The new stack ─────────────────────────────────────────────────────────────

## dev: the api on :8000, the worker, and the web app on :5173; the api and web reload on change
dev: migrate
	@trap 'kill 0' EXIT; \
	(cd server && $(UV) run opennotebook serve --reload) & \
	(cd server && $(UV) run opennotebook worker) & \
	(cd web && $(PNPM) dev) & \
	wait

## migrate: bring the database up to the newest schema, on every branch of it
migrate:
	cd server && $(UV) run alembic upgrade heads

## api-client: regenerate the web app's client from the server's OpenAPI
api-client:
	cd web && $(PNPM) api

## check: everything CI runs for the new stack
check: check-server check-web

check-server:
	cd server && $(UV) run ruff format --check . && $(UV) run ruff check .
	cd server && $(UV) run pyright
	cd server && $(UV) run pytest -q

check-web:
	cd web && $(PNPM) lint && $(PNPM) test && $(PNPM) build

fmt-new:
	cd server && $(UV) run ruff format . && $(UV) run ruff check --fix .

# ── Production and end-to-end ─────────────────────────────────────────────────

# The production stack in deploy/: Caddy, the api, the worker, Postgres.
# Settings come from deploy/.env (copy deploy/.env.example).
# The dev database's DATABASE_URL, exported above, is kept out: compose would
# take it over deploy/.env and point the containers at the wrong Postgres.
COMPOSE ?= env -u DATABASE_URL -u TEST_DATABASE_URL docker compose -f deploy/compose.yaml

## up: build and start the production stack in the background
up:
	@test -f deploy/.env || { echo "deploy/.env is missing: copy deploy/.env.example and fill it in"; exit 1; }
	$(COMPOSE) up -d --build

## down: stop the production stack; its data volumes are kept
down:
	$(COMPOSE) down

## e2e: the Playwright suite against a running studio (make dev); E2E_URL picks another
e2e:
	cd web && $(PNPM) e2e
