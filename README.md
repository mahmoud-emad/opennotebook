# OpenNotebook

A NotebookLM-style learning studio. You gather sources into a collection (files, links, pasted text, or a web research report) and it makes things from them: narrated slide decks (a spoken script over the slides in one or two voices, played in the browser from start to finish, and interruptible with a spoken question), audio overviews, mind maps and study notes, every claim cited to its passage.

It is one Rust binary that serves a JSON-RPC API, the narration and slide bytes, and a Dioxus web app on one address. Everything it keeps is in one data directory, most of it in one SQLite file.

## Docs

- [Phase 1 specification](docs/phase1-spec.md): resources in, narrated deck out; what is built and why, with every measured number and the amendments that corrected it
- [Phase 2 specification](docs/phase2-spec.md): push to talk interruption, the decisions it rests on, and which of them are measurement and which are judgement
- [Design](docs/design.md): how the studio looks and why, the tokens both themes share, and the tests that hold them
- [Mind map specification](docs/mindmap-spec.md): a mind map of a collection's sources, where clicking a topic asks the chat about it with citations
- [Study notes specification](docs/study-notes-spec.md): a study guide of a collection's sources, with a quiz, a glossary and every claim cited to its passage
- [Audio overview specification](docs/audio-overview-spec.md): an audio overview of a collection's sources, in four formats, to listen to and join with spoken questions
- [Stack migration plan](docs/stack-migration-plan.md): moving to a TypeScript web app, a FastAPI server and Postgres, with what carries over and in which order
- [Open questions](docs/open-questions.md): decisions not made yet (distribution, hosting, sign-in, billing and credits), what each one blocks, and what the work assumes until then

## What it needs

- **Rust 1.96** or newer, and for the web app the Dioxus CLI: `cargo install dioxus-cli` (the `dx` command).
- **An OpenAI-compatible LLM endpoint.** OpenRouter by default: set `OPENROUTER_API_KEY`. Or point `OPENNOTEBOOK_AI_BASE_URL` at Ollama (`http://localhost:11434/v1`), LM Studio, vLLM or any other `/chat/completions` server. The default model ids are OpenRouter's (Claude Haiku 4.5, Gemini 2.5 Flash Lite, Perplexity Sonar for web search, GPT Audio Mini for spoken answers), so on another endpoint set the model settings too.
- **For audio, an OpenAI-compatible speech server.** By default a local [Speaches](https://speaches.ai) at `http://localhost:8000/v1`, which serves Kokoro voices for speech and Whisper for transcription. [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) or OpenAI also work by changing the URL. Slides, mind maps and study notes work without one.

## Build and run

```sh
make build                                   # server (release) and web app
cargo run --release -p opennotebook_server   # or: make run
```

Then open <http://127.0.0.1:7878/>. On start it prints the address and the data directory.

`make build` is `make build-server` (`cargo build --release -p opennotebook_server`) plus `make build-ui`, which runs `crates/opennotebook_ui/install.sh`: it builds the wasm app with `dx` and installs it into `<data dir>/ui/`, where the server serves it at `/ui/`. The UI crate is its own workspace, because a wasm crate cannot share a target dir with the native crates, so `cargo build --workspace` does not touch it. Without the web app the API still serves.

Optional: `scripts/build-vad-wasm.sh` builds the voice activity detector the player uses to tell when a spoken question has ended, and installs it as `<data dir>/vad.wasm`. Without it the player falls back to a simple loudness threshold.

`make test` and `make lint` run the tests and clippy over both workspaces. The tests need no running services.

## Configuration

Every setting is read from the environment first, then from `settings.toml` in the data directory, then from its default; the environment wins. The settings dialog in the app writes `settings.toml` (mode 0600, because it can hold the API key). The file is a flat table of strings:

```toml
OPENROUTER_API_KEY = "sk-or-..."
OPENNOTEBOOK_LANGUAGE = "French"
```

| Setting | Default | What it does |
|---|---|---|
| `OPENNOTEBOOK_HOME` | platform data dir | The data directory. Environment only. |
| `OPENNOTEBOOK_LISTEN` | `127.0.0.1:7878` | Address to listen on. Loopback by default: the studio has no login, so exposing it (`0.0.0.0:7878`) is a deliberate choice. |
| `OPENNOTEBOOK_UI_DIR` | `<data dir>/ui` | Where the built web app is. |
| `OPENROUTER_API_KEY` | | API key for the LLM endpoint. |
| `OPENNOTEBOOK_AI_API_KEY` | | API key for the LLM endpoint; wins over `OPENROUTER_API_KEY`. |
| `OPENNOTEBOOK_AI_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint. A key is required only for OpenRouter. |
| `OPENNOTEBOOK_TTS_BASE_URL` | `http://localhost:8000/v1` | Speech server for narration and spoken answers. |
| `OPENNOTEBOOK_STT_BASE_URL` | the TTS URL | Transcription server. |
| `OPENNOTEBOOK_TTS_MODEL` | `speaches-ai/Kokoro-82M-v1.0-ONNX` | Speech model. Voices are Kokoro ids (`af_bella`, `bm_george`, ...). |
| `OPENNOTEBOOK_STT_MODEL` | `Systran/faster-whisper-small` | Transcription model. |
| `OPENNOTEBOOK_SPEECH_API_KEY` | | Key for the speech server, if it needs one. |
| `OPENNOTEBOOK_EMBED_MODEL` | empty (off) | Embedding model for vector search alongside full-text search, e.g. `openai/text-embedding-3-small`, or `nomic-embed-text` on Ollama. Off, retrieval is full-text (SQLite FTS5) only. |
| `OPENNOTEBOOK_QA_MODEL` | `openai/gpt-4o-mini` | Model that extracts Q&A pairs from each source at ingest. |
| `OPENNOTEBOOK_SCRIPT_MODEL` | `anthropic/claude-haiku-4.5` | Writes outlines and narration scripts. |
| `OPENNOTEBOOK_SLIDE_MODEL` | `anthropic/claude-haiku-4.5` | Writes the slides and draws their figures. |
| `OPENNOTEBOOK_CHAT_MODEL` | `google/gemini-2.5-flash-lite` | The chat assistant, Ask, and collection naming. |
| `OPENNOTEBOOK_MINDMAP_MODEL`, `OPENNOTEBOOK_NOTES_MODEL` | `google/gemini-2.5-flash-lite` | Mind maps and study notes; they read every source whole, so they need a long context window. |
| `OPENNOTEBOOK_SEARCH_MODEL` | `perplexity/sonar` | Web search and deep research. |
| `OPENNOTEBOOK_ANSWER_MODEL` | `openai/gpt-audio-mini` | Answers a spoken question during playback; must accept audio input. |
| `OPENNOTEBOOK_VAD_WASM` | `<data dir>/vad.wasm` | Path to the voice detector. Environment only. |

The settings dialog also covers generation defaults (slides per deck, narration length, style, audio overview format and length, research depth), the two speakers' names, voices and roles, language, live conversation behaviour, and the spending limit (`OPENNOTEBOOK_MAX_BUILD_USD`, $0.50 by default). Their keys, labels, ranges and defaults are one catalogue in `crates/opennotebook_session/src/settings.rs`, which the dialog is drawn from and the server validates against.

## Data directory

`$OPENNOTEBOOK_HOME`, else `~/Library/Application Support/opennotebook` on macOS and `~/.local/share/opennotebook` on Linux.

```
<data dir>/
  opennotebook.db       SQLite (WAL): sessions, collections, prep jobs, the retrieval index
  settings.toml         operator settings, mode 0600
  staging/<cid>/        a collection's sources, one Markdown file each
  sessions/<sid>/       what a session was built from
  audio/<sid>/          narration WAVs
  decks/<sid>/          slides
  mindmaps/<cid>/       one JSON file per mind map
  notes/<cid>/          one JSON file per set of study notes
  prep/                 the spec file each prep job is started with
  banter/               cached spoken interjections
  ui/                   the installed web app
  vad.wasm              the voice detector, if built
```

Deleting the directory resets the studio.

## Collections

A collection is one set of sources and everything made from them, as many of each kind as you ask for. Its id (the cid, `s<epoch ms>`) is the staging id its sources are added under:

| What | Where |
|---|---|
| Sources | `<data dir>/staging/<cid>/`, one Markdown file each |
| Mind maps, study notes | `<data dir>/mindmaps/<cid>/`, `<data dir>/notes/<cid>/`, one JSON file each |
| Decks and audio overviews | a session row each, with its own sid and `collection: <cid>`; its files under `decks/<sid>`, `audio/<sid>`, `sessions/<sid>` |
| The collection itself | a `collection` row in `opennotebook.db`: title, whether the studio names it, pin, times, and the sids of its outputs |

A session from before collections is listed as a collection of its own, and a staging directory with no row as a draft; each gets a row the first time it is renamed, pinned or added to. While a collection's title is automatic the studio names it from its sources in the background whenever they change.

Deleting an output stops its prep job if it is still running, removes its row, then its files and its retrieval index, best effort. Deleting a collection does that for every output and removes its sources, maps, notes and row. Work that finishes after a delete writes nothing: a late source, map, notes or prep finds the collection gone and drops its result, so a deleted collection stays deleted. An agent's flow is `collection_create`, then `source_add_*` with the cid, then `session_build{collection: cid, sid: ""}` and `mindmap_create` / `notes_create` with the cid, as often as it likes.

Building a deck or an audio overview takes minutes, so it runs as a prep job: the server starts itself again as `opennotebook_server prep --spec <file> --job <id>`, and the child reports progress on its row in the `jobs` table. One prep runs at a time, a crash in one cannot take the server down, and a prep keeps running across a server restart.

## Slides and styles

Studio writes its own slides. One model call per four slides (Claude Haiku 4.5 by default, Sonnet 5.5 in Settings › Models) lays out each slide's copy from the script and draws its figure as inline SVG; the calls run in parallel, and at the same time as the narration is synthesised. Every slide then gets its style's kit, which supplies the fonts, every colour, the textures and the components, so the model never picks a text colour and the five slides of a deck cannot drift apart. The model is told to leave colour and positioning to the kit, and what it writes is cleaned before the kit goes on in case it does not: its colours, gradients, opacity, background shapes, emoji and any CSS beyond layout are removed (`clean.rs`). A slide that comes back without its title is asked for again. A batch that comes back short is asked again; a slide still missing is drawn plain from its copy, so a build never fails over a figure.

The eight styles are NotebookLM's infographic styles: Editorial, Professional, Bento grid, Instructional, Scientific, Sketch note, Clay and Bricks. The kits live in `crates/opennotebook_build/src/kits.rs`, and every palette is held to WCAG AA by its tests. The picker's thumbnails are screenshots of `/api/session/style_sample?style=<id>`, made with `scripts/style-thumbnails.py` (Playwright, against a running server).

## Cost

A five-slide deck costs a few cents on the default models: about $0.03 to $0.05 measured on OpenRouter's prices, most of it the slide model. The cost dialog shows the estimate before a build, and a build whose estimate could go over the spending limit ($0.50 by default, Settings › Costs & limits) is refused before it starts. Deck narration length is under Settings › Generation defaults, from 2 to 20 minutes; the narration is spoken by the speech server, local and free by default, so length mostly adds script tokens. Every model call records its spend from the provider's reported cost, and the price catalog for estimates is read from the endpoint's `/models` list.

## Crates

| Crate | Role |
|---|---|
| `opennotebook_server` | The binary: the JSON-RPC domains, the narration, slide and event byte routes, the player page, the web app, and the prep job it runs as a child of itself |
| `opennotebook_api` | The API contract: types, server traits, dispatchers and typed clients, generated at build time from `oschema/` (wasm and native) |
| `opennotebook_sdk` | The client side shared with the web app: the generated clients and the shared tables (styles, voices, icons, theme) |
| `opennotebook_session` | The data directory, `settings.toml`, the SQLite database, the session and collection store, spend records, and the configured LLM provider |
| `opennotebook_ai` | A small client for OpenAI-compatible chat completions: tools, streaming, audio and image parts, reported cost |
| `opennotebook_memory` | Retrieval over a collection's sources in SQLite: chunked FTS5 search, optional vectors fused by reciprocal rank, and extracted Q&A pairs |
| `opennotebook_ingest` | The one ingest path: converts resources, writes the session directory, indexes it, extracts Q&A, and proves it is retrievable |
| `opennotebook_script` | Outline and per-slide speaker-tagged script grounded in the collection, with character budgets applied on the way out; mind maps and study notes |
| `opennotebook_build` | The slide writer and style kits, narration synthesis, duration from the WAV header, the checks, and the prep job row |
| `opennotebook_speech` | Text to speech and speech to text over OpenAI-compatible audio endpoints, always returning 24 kHz mono 16-bit WAV |
| `opennotebook_vad` | Voice activity detection, native and wasm32 from one source: when a spoken turn ends, and whether a clip held speech |
| `opennotebook_convert` | Word, PowerPoint, Excel and PDF to Markdown, verbatim, no model and no network |
| `opennotebook_ui` | The Dioxus web app (separate workspace, built with `dx`) |

## API

The contract is `crates/opennotebook_api/oschema/`, one directory per domain, so the schema is what you read and what you change; `build.rs` generates the Rust types, traits, dispatchers and clients from it.

```
POST /api/<domain>/rpc            JSON-RPC 2.0 for mindmap, notes, session, settings, sources
GET  /api/<domain>/openrpc.json   that domain's OpenRPC document
GET  /api/domains.json            every domain and its methods
GET  /health.json, /api/ping      liveness
```

| Domain | Methods |
|---|---|
| `session` | `collection_create/list/get/retitle/pin/cover_refresh/delete`, `session_build/get/list/estimate/ask/prepare/delete/retitle/pin`, `playback_get/play/pause/progress/finish` |
| `sources` | `draft_create`, `source_add_urls/text/file`, `source_list`, `source_remove`, `web_search`, `deep_research`, `source_ask` |
| `mindmap` | `mindmap_create/estimate/list/list_all/get/delete/retitle` |
| `notes` | `notes_create/estimate/list/list_all/get/delete/retitle` |
| `settings` | `settings_get`, `settings_set`, `styles_list` |

Byte routes, beside the RPC on the same address:

```
GET  /api/session/events      SSE: session.state, slide.enter, line.start, line.end, playhead, prep.progress
GET  /api/session/audio       a narration line's WAV
GET  /api/session/episode     an audio overview as one WAV
GET  /api/session/slide       a slide, HTML or PNG by the format it was built in
GET  /api/session/cover       a collection's cover
GET  /api/session/player      the player page
POST /api/session/ask         a spoken question during playback
POST /api/session/chat        the chat assistant
     /api/session/source/...  staging sources: text, fetch, upload, list, read
```

The web app is at `/ui/`, and `/` redirects there.

## Where it stands

Phase 1 is complete: resources in, narrated deck out, plays start to finish, in one voice or two with no schema change and no player change. Phase 2 is built: push to talk interruption, answered in the voice of the narrator who was interrupted, then back to where the deck was. Mind maps, study notes and audio overviews are built on the same collections.

## Licence

MIT, see [LICENSE](LICENSE). The document converter, `opennotebook_convert`, is original OpenNotebook code under the same licence.
