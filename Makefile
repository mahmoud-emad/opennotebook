UI_DIR := crates/opennotebook_ui

.PHONY: all build build-server build-ui run lint fmt test clean

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
