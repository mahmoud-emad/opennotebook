# OpenNotebook, stack migration plan

Moving OpenNotebook off Rust: a **TypeScript web app**, a **FastAPI server**, and **Postgres** for everything it keeps. This is the plan. Nothing in it is built yet.

It rests on two pieces of research done on 2026-10-04: an inventory of the system as it stands (the Rust repo at `c497c4a` plus the uncommitted work after it), and a survey of the target stack with every version and licence checked against the registries. Facts from the inventory carry a file reference; facts from the survey carry a link in [section 13](#13-sources).

## How to read this one

The labels follow the [phase 2 spec](phase2-spec.md):

- **Measured.** A number taken from the repo or a registry, not estimated.
- **Decided.** A judgement, with the alternative named so it is cheap to reopen.
- **Open.** A question for the owner, not yet answered. They live in [open-questions.md](open-questions.md), with what each one blocks.

The owner answered the first round of questions on 2026-10-04; [section 0.1](#01-owner-decisions-2026-10-04) records the answers, and the sections below are written to them.

## 0. Decisions locked

| | Decision | Runner-up | Why |
|---|---|---|---|
| Web app | **React 19 + Vite 8**, a static SPA; TanStack Router and Query; Tailwind v4 + shadcn/ui | Svelte 5 / SvelteKit 3 (SPA) | Largest ecosystem and hiring pool; shadcn/ui and streaming-Markdown components exist ready-made; Vite 8 (Rolldown) builds in seconds. No SSR: the app needs no SEO, and a static bundle behind a CDN-style server is the fastest thing to load |
| API contract | **REST + OpenAPI**, client generated with **hey-api** | Keep JSON-RPC | FastAPI writes the OpenAPI document; hey-api turns it into a typed fetch SDK and TanStack Query hooks, so a server change breaks the web build, not the page. JSON-RPC would throw that away |
| Server | **FastAPI 0.142** on **Python 3.14**, Pydantic 2, uvicorn; uv, ruff, pyright | Litestar 2.24 | FastAPI has SSE built in since 0.135; Litestar's v3 rewrite is still to land, so starting on 2.x buys a migration |
| Database | **Postgres 18** with **pgvector 0.8** | — | One store for documents, jobs, search and vectors. PG 18 adds `uuidv7()` and async I/O |
| Data access | **SQLAlchemy 2.1 async** on **psycopg 3**, **Alembic** migrations | asyncpg | One driver for the app and the job queue |
| Search | `tsvector` + GIN for keywords, pgvector HNSW for vectors, fused with RRF in SQL | pg_textsearch (BM25) later | No extension licence questions. ParadeDB is AGPL and is ruled out |
| Background jobs | **Procrastinate** workers, our own `jobs` table for progress, `LISTEN/NOTIFY` to SSE | Hand-rolled `SKIP LOCKED` queue | No Redis; cancel, retry and stalled-job recovery come with it |
| Model calls | **openai Python SDK** pointed at OpenRouter, our own agent loop | pydantic-ai | The grounding and citation rules are ours and must stay readable in one place |
| Documents to Markdown | **Port `opennotebook_convert` to Python** on python-docx-level XML, python-pptx, openpyxl and pypdfium2/pdfplumber; markitdown measured against it in phase 0 | markitdown | The verbatim guarantee is the product. markitdown's DOCX path goes through markdownify, which escapes `_` and `*`, and that breaks byte-for-byte quoting |
| Voice detection | **Silero** on both sides: `@ricky0123/vad-web` in the browser, the same ONNX model in Python on the server | Keep the Rust/wasm detector | Today one detector runs in both places so they cannot drift (`vad/src/lib.rs`). Silero keeps that property with no Rust left |
| Speech | Unchanged: any OpenAI-compatible TTS/STT server (Speaches by default) | — | It is a separate service already |
| Web fetch | **httpx** + **trafilatura** (≥ 1.8, Apache-2.0) | readability-lxml | Markdown output and metadata in one call |
| Deploy | **Docker Compose**: Caddy (web + proxy), api, worker, postgres, speaches | nginx | Caddy serves hashed assets as immutable, proxies `/api`, does HTTPS, and passes SSE through |

**Decided:** FastAPI stays. The only alternative worth naming is Litestar, and its v3 timing argues against it today.

### 0.1 Owner decisions, 2026-10-04

| Question | Answer | What it changes |
|---|---|---|
| Does anything besides the web app drive the studio? | **Yes. Everything is driven by the server; the web app is only a client.** It performs no operations of its own, and every AI call happens on the server | The API is the product: complete enough for an outside agent to do anything the web app does (section 4). The JSON-RPC adapter is kept for one release |
| Carry over existing data? | **No, start fresh** | No importer. The first Alembic migration is the whole schema |
| User accounts? | **Yes, a `users` table in this migration, and an owner on every record.** A credit and usage calculator follows later, for subscriptions or bring-your-own-key, which is undecided | `users`, `owner_id` everywhere, and a per-user usage ledger the calculator can read (section 5) |
| Chat history | **On the server; everything is loaded from the server** | `chat_messages` table and API; the browser keeps nothing but small view preferences |
| Self-hosted or cloud | Undecided (open source or enterprise, not chosen) | Nothing may assume one. Kept in [open-questions.md](open-questions.md) |
| Customers running it themselves | Undecided; revisit after the migration | The speech container's GPL parts stay a noted risk. Kept in [open-questions.md](open-questions.md) |

## 1. What is being moved

**Measured** (inventory, at `c497c4a`):

| | Size |
|---|---|
| Rust | 54.6k lines in 13 crates; the server alone is 17.5k |
| Web app | Dioxus 0.7 compiled to wasm: 9.4k lines, 33 components, 13 files |
| Player | `server/src/player.html`, 2,400 lines of plain JS (117 KB) |
| API | 5 domains, 47 JSON-RPC methods, 71 types (`opennotebook_api/oschema/`) plus 22 non-RPC routes (bytes, SSE, uploads) |
| Model call sites | 16 (`agent.rs`, `research.rs`, `script/generate.rs`, `build/slides.rs`, `ask.rs`, …) |
| Tests | 522, none of which need running services |
| Build output on disk | 52 GB (48 GB server workspace, 4.5 GB web app) |

The web app is where Rust costs most and gives least: a niche framework, a wasm bundle, its own install script, few people who know it. The server is where the behaviour lives, and most of this plan is about carrying that behaviour across without losing any of it.

## 2. Target architecture

```
                 ┌──────────────── Caddy ────────────────┐
  browser ──────▶│ /            → web/dist (static SPA)  │
                 │ /api/*       → api:8000               │
                 └───────────────────┬───────────────────┘
                                     │
          ┌──────────────────────────┼──────────────────────────┐
          ▼                          ▼                          ▼
   api (FastAPI)              worker (Procrastinate)      speaches (TTS/STT)
   REST + SSE                 prep, ingest, research,     OpenAI-compatible
   LISTEN job_progress        covers, naming
          │                          │
          └────────────┬─────────────┘
                       ▼
             postgres 18 + pgvector            files volume
             rows, search, vectors, jobs       WAV, slide HTML, uploads
```

- **The api** answers requests, streams the chat agent and the voice answers, and holds one `LISTEN` connection. It fans progress out to SSE subscribers.
- **The worker** runs everything that takes longer than a request: decks and audio overviews (2–20 min), ingest, deep research, cover design, naming. It replaces the prep child process the server spawns today (`dispatch.rs:21-35`).
- **Postgres** holds every record that is a SQLite row, a JSON file or a TOML table today.
- **The files volume** keeps the bytes Postgres should not: narration WAVs, slide HTML and original uploads. The path layout stays the same (`audio/<sid>/<line>.wav`, `decks/<sid>/…`), behind one storage interface so S3 can replace it later without touching callers.

### Repository layout

```
web/        Vite + React + TS app (player included, as a route)
server/     Python package `opennotebook` (api, worker, domain, migrations)
deploy/     compose.yaml, Caddyfile, .env.example
docs/
crates/     the Rust system, kept running until cutover, then deleted
```

## 3. The web app

### Stack

| Concern | Choice | Notes |
|---|---|---|
| Build | Vite 8 | Route-level code splitting; hashed assets |
| UI | React 19, TypeScript strict | |
| Routing | TanStack Router | Type-safe routes and search params. Same URLs as today: `/ui/`, `/ui/collections`, `/ui/c/<cid>`, `/ui/c/<cid>/mind-map/<id>`, `/ui/c/<cid>/study-notes/<id>`, plus the legacy redirects (`ui/src/routes.rs`) |
| Server state | TanStack Query | Polling while something is preparing, as `main.rs` does today |
| API client | hey-api generated from `/openapi.json` | Committed to the repo; the version pinned (it is 0.x) |
| Styling | Tailwind v4 + shadcn/ui | `theme.css` (111 custom properties, WCAG AA tested in `sdk/src/theme.rs`) becomes the Tailwind `@theme`, so both themes and every token carry over |
| Icons | `bootstrap-icons` (MIT) | The same 58 glyphs as today, so nothing changes visually |
| Markdown | Streamdown, or react-markdown + remark-gfm + rehype-sanitize | No raw HTML. Citation chips become a remark plugin that renders a React component, replacing `with_chips` string surgery (`ui/src/mindmap.rs`) |
| Streams | `fetch` + `ReadableStream` SSE parser | `EventSource` cannot POST, and the chat posts its history |
| Voice | `@ricky0123/vad-web` (Silero v5) | Lazy-loaded with push-to-talk only: the ORT wasm is several MB |

### A thin client

**Decided by the owner:** the web app performs no operations. Today it does several, and each one moves to the server:

| The web app does today | Moves to |
|---|---|
| Starts a build when the agent's stream says `build` (`ui/src/collection.rs`, `chat.rs`): the agent asks, the page acts | The agent's `start_build` tool starts the job itself on the server, and the stream reports it as a step |
| Runs the slash commands `/slides`, `/audio`, `/mindmap` and `/notes` by calling the generate actions | `POST /api/collections/{cid}/chat/commands`: the server runs the command, and returns the same events a chat turn does. The menu's list comes from the server too (`GET /api/commands`), so the web app and any other client see the same commands |
| Keeps the Ask conversation in `localStorage` (`opennotebook.chat.<cid>`) | `chat_messages` on the server, read with the collection |
| Writes the `/help` text from its own list | The server's command list |
| Rewords server errors (`ui/src/errors.rs`) | The server sends the readable sentence; the client shows it as it is |
| Queues a second build while one is starting (`queued_build`) | The job queue |

What stays in the browser is drawing: layout (the mind map's geometry), playback of audio the server made, recording the microphone, the voice detector that decides when a question ends, and view preferences such as the theme, grid or list, and caption on or off.

### What carries over, screen by screen

| Today (Rust) | Lines | Becomes |
|---|---|---|
| `collection.rs`: sources panel, Studio tiles, options, outputs list, Ask tab host | 1,846 | `routes/c.$cid.tsx` + `features/sources`, `features/studio`, `features/outputs` |
| `main.rs`: shell, top bar, cards, banner, snackbar | 1,166 | `app/Shell.tsx`, `components/Snackbar.tsx` |
| `mindmap.rs`: SVG map, PNG/MD/OPML export | 1,027 | `features/mindmap/`. Its layout comes from `sdk/src/mindmap_layout.rs` (583 lines, tested), ported to TS with those tests |
| `settings.rs` | 958 | `features/settings/`, rendered from the server's catalogue as it is today |
| `chat.rs`: Ask tab, `/` command menu, step lines | 926 | `features/chat/` |
| `outputs.rs`, `dialogs.rs`, `notes.rs`, `home.rs`, `pick.rs` | 2,562 | One feature folder each |
| `errors.rs`: every error as a plain sentence | 281 | `lib/errors.ts`, with its tests. The server sends the same wording, so the player and the app stop keeping two copies |
| `source.rs`: citation drawer | 210 | `features/sources/SourceDrawer.tsx` |
| `player.html` | 2,400 | `routes/play.$sid.tsx`, a lazy route of the same app: one build, one theme, one error layer |

**Decided:** the player moves into the app. Today it is a separate page with its own copy of the theme, icons and error wording, and placeholders substituted on the server (`player.rs:66-78`). As a lazy route it shares all three and costs the home page nothing.

### "Super fast"

- **To load.** A static SPA behind Caddy: `index.html` uncached, every asset hashed and `immutable`. The first chunk holds the shell and home only. The player, mind map, Markdown renderer and voice detector load when they are first used. The budget is under 150 KB gzipped for the first screen, enforced with a size check in CI.
- **To build.** Vite 8 builds in seconds, and its dev server applies a change without a reload. Today a change to the Rust app is a wasm rebuild.
- **To change.** Types flow from the server's models through OpenAPI to the client, so a renamed field is a compile error, not a blank panel.

## 4. The server

### Package layout

```
server/opennotebook/
  api/          FastAPI routers, one per domain; SSE endpoints
  domain/       collections, sources, sessions, mindmaps, notes, settings
  agent/        the chat agent loop and its tools
  ai/           OpenAI-SDK client, spend ledger, model catalogue and prices
  convert/      documents to Markdown, verbatim (port of opennotebook_convert)
  memory/       chunking, tsvector + vector search, RRF, Q&A extraction
  script/       outline, script, budgets, citations, mind map, notes
  build/        slides, kits, clean, validate, narration, WAV
  speech/       TTS/STT client, WAV contract, Silero gate
  research/     web search, fetch, readability, deep research
  jobs/         Procrastinate app, task definitions, progress
  db/           SQLAlchemy models, session, Alembic migrations
```

Each folder maps onto one Rust crate or one server module. Each is ported with that crate's tests as its specification (section 9).

### API: from JSON-RPC to REST

The 47 methods map onto resources. The non-RPC routes (bytes, SSE, uploads) are already REST and keep their shape.

| Today | REST |
|---|---|
| `collection_create / list / get / retitle / pin / delete / cover_refresh` | `POST/GET /api/collections`, `GET/PATCH/DELETE /api/collections/{cid}`, `POST /api/collections/{cid}/cover` |
| `source_add_urls / add_text / add_file / list / remove` | `POST /api/collections/{cid}/sources` (one body per kind; files multipart, not base64), `GET`, `DELETE …/sources/{name}` |
| `source_ask`, `session_ask` | `POST /api/collections/{cid}/ask`, `POST /api/sessions/{sid}/ask` |
| `web_search`, `deep_research` | `POST /api/search`, `POST /api/collections/{cid}/research`: now a job, no longer a request that holds the connection for minutes |
| `session_build / prepare / estimate / get / list / retitle / pin / delete` | `POST /api/collections/{cid}/outputs` (kind: slides or audio), `POST …/outputs/estimate`, `GET/PATCH/DELETE /api/sessions/{sid}` |
| `playback_*` | `GET/PUT /api/sessions/{sid}/playback`, persisted (today it lives in memory and is lost on restart, `playback.rs:101-108`) |
| `mindmap_*`, `notes_*` | `/api/collections/{cid}/mindmaps[/{id}]`, `/api/collections/{cid}/notes[/{id}]`, plus `…/estimate` |
| `settings_get / set / styles_list` | `GET /api/settings`, `PATCH /api/settings/{key}`, `GET /api/styles` |
| `/api/session/chat` (SSE) | `POST /api/collections/{cid}/chat`, same event vocabulary (`t`: thinking, step, step_note, step_done, source, reply, state; `agent.rs:27-38`). `build` becomes a step: the server starts the job. The turn is saved to `chat_messages`; `GET /api/collections/{cid}/chat` reads the conversation |
| slash commands (client-side today) | `GET /api/commands`, `POST /api/collections/{cid}/chat/commands` |
| `/api/session/events` (SSE) | `GET /api/sessions/{sid}/events`, the same named events, now driven by `NOTIFY` instead of a one-second poll (`events.rs:57-62`) |
| `/api/session/ask` (voice, SSE) | `POST /api/sessions/{sid}/voice`, the same events (`heard`, `said`, `audio`, `done`, `silent`, `failed`; `ask.rs`) |
| `audio`, `episode`, `slide`, `cover`, `style_sample`, `source/read` | `GET` byte routes under the resource they belong to, with the same caching (ETag on slides, `immutable` on versioned covers) |

Errors become HTTP status codes with a `{"detail": "<a sentence a person can act on>"}` body. The wording rules of `errors.rs` move to the server, so every client gets readable text without keeping its own copy.

**Decided by the owner:** outside clients drive the studio too, so:

- **The REST API is complete.** Anything the web app can do, an agent can do with the same calls: create a collection, add sources, chat, run a command, build, follow progress, play.
- **A JSON-RPC adapter** (`POST /api/{domain}/rpc`) is kept for one release. It forwards each of today's 47 methods to the same handlers, so existing agents keep working while they move to REST. It is deleted in the release after cutover.
- **Agents authenticate like people do**: with an API key that belongs to a user (section 5), so their usage is counted against that user.

### Model calls

- `openai.AsyncOpenAI(base_url=OPENNOTEBOOK_AI_BASE_URL, api_key=…)` on Chat Completions only: tools, streaming and `response_format: json_schema` where the model supports it. OpenRouter fields go in `extra_body`.
- A 402 maps to `QuotaExceeded`, as `opennotebook_ai` does now, including the nested-provider-message unwrapping fixed in `b3193ec`.
- **The spend ledger** becomes a `contextvars` scope around each job, so every call inside it adds to `sessions.spent_usd`, as the task-local ledger does today (`session/src/spend.rs:152-160`). Prices come from OpenRouter's `/models`, cached for 600 s (`estimate_live.rs`).
- **The agent loop** (8 rounds, 4 fetches a call, replacing pages that fail, relaying `ask_sources` answers whole) is ported as it is. Its rules are in `agent.rs`, and its tests come with it.

### Voice

The server's silence gate runs before the model call, so silence is never billed (`ask.rs:540-560`). It becomes Silero through `onnxruntime` (MIT) in Python, the same model the browser runs, with the thresholds that are in `vad/src/lib.rs:114-121` today. Answers stream as 24 kHz mono PCM16, as now.

## 5. Postgres

### Schema

Every table that holds a person's things has `owner_id uuid NOT NULL REFERENCES users(id)`, and every query is scoped by it. The empty-collection limit, the spending limit and every list count per owner.

| Table | From | Notes |
|---|---|---|
| `users` | new | `id uuidv7`, `email` (unique), `display_name`, `created_at`, `disabled_at`. How people sign in is open ([open-questions.md](open-questions.md)); the table does not depend on the answer |
| `api_keys` | new | `id`, `owner_id`, `name`, `hash` (never the key itself), `last_used_at`, `revoked_at`. For agents and scripts |
| `usage_events` | the spend ledger (`session/src/spend.rs`), which today only reaches `sessions.spent_usd` | One row per model, speech or search call: `owner_id`, what it was for (`kind`, `collection_id`, `session_id`, `job_id`), `model`, input and output tokens, characters or seconds for speech, `cost_usd` as the provider reported it or as priced, and `priced_by`. This is what the future credit calculator reads; nothing about plans or credits is decided yet, so nothing about them is stored yet |
| `collections` | `docs(kind='collection')` JSON | Columns for what is queried (`title`, `pinned`, `updated_ms`); `cover` as JSONB |
| `sources` | `staging/<cid>/*.md` files | `cid`, `name`, `title`, `url`, `text` (verbatim Markdown), `chars`, `created_at`. The text moves into the database; the original upload goes to the files volume |
| `sessions` | `docs(kind='session')` JSON | Columns for state, kind, style, spend and the timestamps. `slides` (with their lines) as JSONB, because they are always read and written whole |
| `mindmaps`, `study_notes` | `mindmaps/<cid>/<id>.json`, `notes/<cid>/<id>.json` | Summary fields as columns, the body as JSONB |
| `chunks` | `mem_chunks` + `mem_chunks_fts` | `tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED` with GIN; `embedding vector(n)` with HNSW, nullable |
| `qa_pairs` | `mem_qa` + `mem_qa_fts` | The same pattern |
| `jobs` | `jobs` (SQLite) | `id uuidv7`, `sid`, `kind`, `status`, `step`, `steps_done`, `steps_total`, `error`, `procrastinate_job_id`, timestamps |
| `playback` | in memory | Survives a restart |
| `instance_settings` | `settings.toml` | What an operator sets: endpoints, the model catalogue, defaults. Secrets (the AI key) stay in the environment, never in the database |
| `user_settings` | `settings.toml` | What a person chooses: style, voices, language, live-conversation options, their spending limit. Falls back to the instance defaults |
| `chat_messages` | the browser's `localStorage` | `owner_id`, `collection_id`, `role`, `text`, `steps` (JSONB: the work lines), `citations` (JSONB), `created_at`. **Decided by the owner:** on the server; the web app loads it like everything else |

Every write that today takes the in-process per-collection lock (`collection.rs:139-151`) becomes a transaction with `SELECT … FOR UPDATE` on the collection row. That lock works across processes, so the worker gets it too. Today the prep child cannot take the lock, and relies on `output_still_wanted` checks instead.

### Search

The hybrid query is one SQL statement:
- one CTE ranks by `ts_rank_cd` over the GIN index, another by cosine distance over HNSW;
- each takes `RANK() OVER`, and a `FULL OUTER JOIN` sums `1/(60 + rank)`.

That is the RRF with k = 60 that `memory/src/rank.rs` computes in memory today. Vectors stay optional (`OPENNOTEBOOK_EMBED_MODEL`), as now.

**Decided:** `ts_rank_cd`, not BM25. The upgrade is `pg_textsearch` (PostgreSQL licence), which some managed Postgres services do not offer. ParadeDB's `pg_search` is AGPL and is out.

### Migrations and data

- Alembic owns the schema from the first table. Every change is a migration file, reviewed with the code that needs it.
- **Decided by the owner:** the new stack starts empty. There is no importer, so the first migration is simply the whole schema.

**Decided:** settings split in two. Today there is one global catalogue (`session/src/settings.rs`, 29 entries). With users, the model and endpoint entries belong to whoever runs the instance, and the rest belong to each person. The runner-up is one global table, as now, which would make every person's voice and style choice everyone's.

## 6. Background work

| Job | Today | Becomes |
|---|---|---|
| Deck / audio overview prep (research → ingest → script → deck → validate) | Child process re-executing the server binary; one at a time by a global semaphore (`job.rs:149`); `SIGTERM` to stop | A Procrastinate task on the `prep` queue with concurrency 1, which keeps one at a time. Stop is `abort`, which arrives in the task as `CancelledError`. Each phase checks that the output is still wanted, as now (`pipeline.rs:104-112`) |
| Ingest | Inside the prep | Its own task, so adding sources indexes them straight away |
| Deep research | A synchronous RPC of up to 15 minutes | A task, with progress on the collection's event stream |
| Naming and cover | `tokio::spawn`, coalesced per collection (`collection.rs:471-528`) | A task with a `queueing_lock` per collection, which gives the same coalescing |
| Mind map, study notes | Synchronous inside the request (60 s / 120 s) | Unchanged at first; move to tasks if timeouts show up |

**Progress:** the worker writes `jobs.step` / `steps_done`, then runs `pg_notify('job_progress', job_id)`. The payload is the id only, because NOTIFY payloads are capped at 8 KB and fire on commit. The api's `LISTEN` connection reads the row and pushes `prep.progress` to that session's SSE subscribers. A reconnecting client reads the row first, so nothing is missed.

**Restart recovery:** Procrastinate's stalled-job retry replaces `recover_orphans` (`job.rs:305-340`). The read-time reconcile that turns a `preparing` row with a dead job into `failed` (`session_impl.rs:1184-1250`) is ported unchanged, because it is what keeps the outputs list honest.

## 7. Documents to Markdown

The rule from `convert/src/lib.rs`: no model, no network, never reword. It may drop layout, never wording.

**Plan:** port `opennotebook_convert` (Rust, 3.2k lines, written in this repo, 36 tests) to `server/opennotebook/convert`, on:

| Format | Library | Licence |
|---|---|---|
| DOCX | `lxml` over the package parts (the Rust version reads the XML the same way); python-docx where it helps | BSD / MIT |
| PPTX | python-pptx, slide order from `presentation.xml` | MIT |
| XLSX | openpyxl, values only, the 2,000-row cap | MIT |
| PDF | pypdfium2 for text, pdfplumber where layout helps | Apache-2.0 / BSD-3, MIT |

Its tests come with it: the verbatim fixtures, the scan that must come back under 80 visible bytes so ingest refuses it, the corrupt-input cases, and the zip budget.

**Phase 0 measures markitdown against the same tests.** If it passes them, with no escaping of `_` or `*` and no lost text, use it and delete the port. It is maintained by others.

**Ruled out:** PyMuPDF and pymupdf4llm, which are AGPL or need a paid licence; docling, which needs torch and layout models, makes the image several GB, and OCRs, and OCR output is not verbatim source text.

## 8. Phases

Two tracks run in parallel once the contract exists. The web app does not wait for the server: it builds against the OpenAPI document with mocked responses (MSW) until each endpoint lands.

| Phase | What | Done when | Size (Decided, a range) |
|---|---|---|---|
| **0. Spikes** | Converter fidelity (markitdown vs the port) on the fixtures; tool-call streaming through the openai SDK on each listed model; vad-web latency and size; hey-api output on a sample router; Procrastinate abort on a long task | Each spike has a written yes or no in this doc's amendments | 3–4 days |
| **1. Contract and skeleton** | `server/` and `web/` scaffolds, compose with Postgres, the first Alembic migration (every table, `users` and `owner_id` included), sign-in stubbed behind one dependency so the method can be chosen later, API keys, all routers stubbed with their Pydantic models, OpenAPI published, the web client generated, CI (ruff, pyright, pytest with a Postgres container; tsc, eslint, vitest, the bundle-size check) | `docker compose up` serves an empty app; the generated client type-checks | 1 week |
| **2a. Web app** (parallel) | Shell, home, collection page, sources, Studio, outputs, settings, chat, mind map, notes, drawer, snackbar and errors; then the player route and voice | Every screen works against mocks, then against the real API endpoint by endpoint | 3–4 weeks |
| **2b. Server core** (parallel) | Collections, sources and convert, memory and search, ask with citations, settings, estimates and spend, mind maps, notes | Each domain's ported tests pass | 3–4 weeks |
| **3. Builds and voice** | Jobs, prep pipeline (script, slides, kits, clean, narration, audio overviews), events over NOTIFY, deep research, covers and naming, the voice ask route with the Silero gate | A deck and an audio overview build end to end; a spoken question is answered | 2–3 weeks |
| **4. Parity and cutover** | Side-by-side runs on the same sources; the JSON-RPC adapter checked against today's methods; Playwright end-to-end on the main flows; fix the gaps | The parity checklist (section 9) is all green | 1–2 weeks |
| **5. Remove Rust** | Delete `crates/`, the Rust toolchain, `install.sh`, and the Rust parts of the Makefile and README | The repo builds with uv and npm alone | 1 day |

That is 8–12 weeks for one engineer, or about 6 with one on each track. Phase 2a can start the day phase 1's contract is published.

## 9. Parity: the behaviour to carry over

The 522 Rust tests are the specification. The ones that encode product behaviour, and must exist again before cutover:

- **Verbatim and grounding**
  - Conversion: every fixture verbatim, scans refused (`convert/tests/*`, `ingest/tests/ingest.rs`).
  - Notes: a citation to a passage that never says the claim is dropped and counted (`script/src/notes.rs`).
  - Mind map: a node the sources never mention is dropped with its children (`script/src/mindmap.rs`).
  - Script: an empty collection refuses rather than inventing (`script/tests/script.rs`).
  - Citations: renumbered by first use, out-of-range markers dropped (`script/src/cite.rs`).
- **Script quality**
  - Budgets: title 60, line 320, slide narration 650, 17 characters a second, lines cut at whole sentences (`script/src/budget.rs`).
  - The edit pass is accepted only if it parses, keeps the speakers and loses under 40% (`generate.rs:1802-1920`).
  - Truncation is detected on every call.
- **Lifecycle**
  - `ready` is written once, after validation.
  - A deleted output or collection is never resurrected (`collection.rs:21-35`).
  - Five empty collections at most.
  - One prep at a time.
  - Reconcile at read time.
- **Cost**
  - The estimates match the measured builds (`estimate.rs`: "a five-slide deck matches the measured builds", "a twenty-minute session stays under fifty cents").
  - The limit is checked against the high estimate.
- **Slides:** kit palettes pass WCAG AA; `clean` strips colour, gradients, background shapes, emoji and scripts; a missing slide is drawn plain from its copy.
- **Voice**
  - Silence is gated before the model call and never billed.
  - The resume gate applies at 2.5 s.
  - Answers stream as 24 kHz PCM16.
  - The episode gaps are 220 / 320 / 650 ms.
- **Errors:** every failure reaches the person as a sentence with what to do (`ui/src/errors.rs` tests).

Each item becomes a pytest or vitest test in the port of the module it belongs to, and a checklist row in phase 4.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Verbatim conversion regresses in a new library | The converter is ported with its tests, and markitdown is used only if it passes them |
| Streaming tool calls differ by model on OpenRouter | Spike in phase 0 on every model in the settings catalogue; keep the agent loop our own |
| The rewrite drifts from today's behaviour | Section 9 as a gate. Both stacks run side by side on the same sources in phase 4 |
| SSE connection limits (6 per host on HTTP/1.1) | Caddy serves HTTP/2; the client multiplexes one session-events stream per open output, as `LIVE_MAX` does today (`ui/src/collection.rs:1123`) |
| `LISTEN` through PgBouncer in transaction mode | The api's listener uses a direct connection |
| 0.x dependencies (hey-api, vad-web, markitdown) | Pinned exact versions; generated code committed |
| Procrastinate is looking for maintainers | Jobs go through one small module. The fallback is a `SKIP LOCKED` queue on the same `jobs` table, about 200 lines |
| `ts_rank_cd` retrieval is weaker than BM25 | Measure on real collections in phase 2b; `pg_textsearch` is the upgrade |
| GPL inside the speech container (piper-tts, espeak-ng via kokoro-onnx) | Fine as a separate network service. Whether customers run it themselves is open ([open-questions.md](open-questions.md)) |
| Adding users changes every query | `owner_id` is in the first migration and every repository function takes the owner; a test per domain proves one user cannot read another's records |
| A long rewrite with two systems to keep alive | No new features on the Rust side after phase 1, bug fixes only |

## 11. What does not change

- The product: every screen, flow, setting and default stays as it is today; the 29 settings and their defaults are in `session/src/settings.rs`.
- The model defaults (`anthropic/claude-haiku-4.5` for scripts and slides, `google/gemini-2.5-flash-lite` for chat, maps and notes, `openai/gpt-audio-mini` for answers, `perplexity/sonar` for search), and OpenRouter as the default endpoint.
- The speech server and its 24 kHz mono WAV contract.
- The eight slide kits, the cover designs and the theme tokens.
- The environment variables, where they still apply. The data directory becomes `DATABASE_URL` plus a files volume.

## 12. Open questions

They are kept in one place, [open-questions.md](open-questions.md), with what each one blocks and when it has to be answered. None of them blocks phases 0 to 3.

## 13. Sources

Versions and licences were checked on 2026-10-04.

- Web: [React](https://registry.npmjs.org/react), [Vite 8](https://vite.dev/blog/announcing-vite8), [shadcn/ui changelog](https://ui.shadcn.com/docs/changelog), [react-markdown](https://github.com/remarkjs/react-markdown), [Streamdown](https://github.com/vercel/streamdown), [hey-api](https://github.com/hey-api/openapi-ts), [FastAPI: generating clients](https://fastapi.tiangolo.com/advanced/generate-clients/), [SvelteKit 3](https://svelte.dev/blog/sveltekit-3-is-here)
- Server: [FastAPI](https://pypi.org/project/fastapi/), [FastAPI SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/), [Litestar v3](https://litestar.dev/blog/v3-announcement/), [Python releases](https://www.python.org/downloads/), [ty](https://pypi.org/project/ty/)
- Postgres: [PostgreSQL 18](https://www.postgresql.org/about/news/postgresql-18-released-3142/), [end of life dates](https://endoflife.date/postgresql), [NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html), [psycopg](https://pypi.org/project/psycopg/), [SQLAlchemy](https://pypi.org/project/SQLAlchemy/), [pgvector](https://github.com/pgvector/pgvector), [RRF example](https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py), [pg_textsearch](https://github.com/timescale/pg_textsearch), [ParadeDB (AGPL)](https://github.com/paradedb/paradedb)
- Jobs: [Procrastinate](https://pypi.org/project/procrastinate/), [cancellation](https://procrastinate.readthedocs.io/en/stable/howto/advanced/cancellation.html), [pgqueuer](https://github.com/janbjorge/pgqueuer)
- Documents: [markitdown](https://pypi.org/project/markitdown/), [markitdown dependencies](https://github.com/microsoft/markitdown/blob/main/packages/markitdown/pyproject.toml), [docling](https://pypi.org/project/docling/), [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/readme.html), [pymupdf4llm (AGPL)](https://github.com/pymupdf/pymupdf4llm)
- Models: [openai-python](https://github.com/openai/openai-python/releases), [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs), [pydantic-ai](https://pypi.org/project/pydantic-ai/), [LiteLLM licence](https://github.com/BerriAI/litellm/blob/main/LICENSE)
- Speech and voice: [Speaches](https://github.com/speaches-ai/speaches), [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI), [piper1-gpl](https://github.com/OHF-Voice/piper1-gpl), [vad-web](https://github.com/ricky0123/vad), [Silero VAD](https://github.com/snakers4/silero-vad)
- Web fetch and deploy: [trafilatura](https://github.com/adbar/trafilatura), [httpx](https://github.com/encode/httpx), [Caddy](https://github.com/caddyserver/caddy/releases)

## 14. Progress

### Phase 1, 2026-10-04: contract and skeleton (done)

Built on lab01 against Postgres 18.6 + pgvector 0.8.6. `make check` runs the whole gate.

- **`server/`** (FastAPI 0.142, Python 3.14, uv):
  - **Schema.** Alembic `0001` creates every table in section 5, with `users`, `api_keys`, `usage_events` and `owner_id` throughout. `0002` adds Procrastinate's queue. Upgrade, downgrade and upgrade again leave nothing behind, and `alembic check` is clean.
  - **Sign-in.** It sits behind one dependency, `auth.current_user`. There are two modes: `local` (a request with no key is the one owner) and `keys` (every request needs an API key, of which only the SHA-256 is stored). People's sign-in will be a third mode.
  - **Errors.** The readable-error layer moved to the server (`errors.py`, ported from `ui/src/errors.rs` with its tests). Every 4xx and 5xx is `{"detail": "<sentence>"}`, request-validation errors included.
  - **Routers.** Every route in section 4 is routed, and 31 paths are published in OpenAPI.
    - Working now: collections (the five-empty limit is counted per person and is race-safe on the owner row), notes as sources, reading and removing sources, outputs (read, rename, pin, delete), playback (persisted), mind maps and notes (read, rename, delete), chat history (read, clear), the `/` command list, and API keys.
    - Everything else answers 501 "This is not available in the new studio yet." until its domain is ported.
  - **Tests.** 32 pytest tests against a real database, warnings treated as errors. They include a test that one person cannot reach another's collection through any route. ruff and pyright (strict) are clean.
- **`web/`** (React 19, Vite 8, TanStack Router and Query, Tailwind v4, hey-api 0.99):
  - **Generated client.** It is generated from the server's OpenAPI (`make api-client`) and committed.
  - **Theme.** `theme.css` was carried over unchanged and mapped into Tailwind's `@theme`.
  - **Pages.** The shell, the theme switch, the snackbar, home (with New collection on home only), and a collection page with sources.
  - **Size.** The first screen is 124 KB gzipped, against a 150 KB budget enforced by `scripts/size.mjs`. The build takes under a second.
  - **TypeScript.** Pinned to 6.0, because typescript-eslint does not support 7 yet.
- **Not yet:** Docker Compose and Caddy (lab01 has no Docker; the dev loop is `make dev`), and CI wiring.

### Phase 0, converter spike: markitdown is out, the port stays

The converter was ported to `server/opennotebook/convert/`, with all 36 Rust tests and one more; on the lab its 52 test cases pass. The PDF path now reads PDFium's text layer (pypdfium2) instead of `pdf_oxide`. PPTX is read as raw XML with lxml, because python-pptx hides placeholder types and SmartArt text. Every error is a full sentence that says what to do.

markitdown 0.1.8 was run on the same fixtures and fails the verbatim rule:
- **Escaping:** `_` and `*` are escaped (`af\_bella`, `5 \* 3`).
- **Lost text:** a Word field's text is dropped, and a Title heading loses its `#`.
- **Tables:** a `|` in a cell splits it.
- **XLSX:** sheets come out as `Unnamed: 0` / `NaN` / `3.0`, with no row cap.
- **Bad input:** garbage `.docx` and `.pdf` files are accepted as text, and a zip bomb is fully inflated.

It passed the PDF verbatim test, the scan, tracked changes, text boxes, lists and footnotes, but not enough of the rest. **Decided:** keep the port.

### Phase 2b, 2026-10-04: first server domains

- **Settings** (`domain/settings.py`, `domain/styles.py`)
  - All 29 settings and the 8 slide styles are ported with their tests.
  - **Instance scope (7 settings):** the model settings. **User scope (22):** everything else.
  - Lookup order: environment, then the person's row, then the instance row, then the default.
  - Model ids are checked against the AI endpoint's model list. Prices are shown as hints, as before.
  - Instance settings can be changed only by the local owner, because there is no admin role yet (open question).
- **AI client** (`ai/`)
  - The openai SDK, with our own retries: out of credit is never repeated.
  - OpenRouter's relayed errors are unwrapped, sorted and worded.
  - Streaming assembles tool calls.
  - Every call is priced (by the provider, then the price list, otherwise counted as unpriced) and written to `usage_events` inside a `ledger.spending(...)` scope.
  - The SDK uses `httpx2`, its fork of httpx, so test transports and caught errors must be httpx2's.
- **Sources**
  - Notes: any non-empty text, as in Rust.
  - Files: through the converter, with the original kept on the files volume.
  - Web pages: plain HTTP + trafilatura, as Rust did, with the same bot-check, thin-page and 404 refusals as sentences.
  - New: links to the studio's own network are refused, redirects included.
- **Gate:** 135 server tests, ruff, and pyright strict, all green.
- **Next:** memory and search (chunks, `tsvector` + RRF), ask with citations, the chat agent and its `/` commands, mind maps, notes. Then jobs and builds (phase 3), and the web screens (phase 2a).

### 2026-10-05: the web app ported as it was, Discover and sharing included

- **The web app is a straight port, not a redesign.** It uses the old `style.css` and `theme.css` verbatim, the same markup, class names, wording and icons, and the screens are ported file by file from `crates/opennotebook_ui`. Tailwind and TanStack Router are gone.
- **The old UI comes from two lines of history:** `development`, plus commit `fa51e90` on `origin/main`. That commit adds Discover as the home page, My collections, sharing, Reuse and the read-only share page.
- **Server ports:**
  - ask with citations, mind maps and study notes;
  - covers (the same drawing as before, checked against the Rust renderer on 192 pages) and auto-naming;
  - search (memory);
  - shares, the Discover feed and Reuse (migration 0005).
- **Gates:** 290 server tests and 75 web tests pass. The first screen is 134.5 KB; the collection page loads only when a collection is opened.
- **Not ported yet:** builds (jobs, the script, slides and narration), the chat agent stream and its `/` commands on the server, the player, and voice.

### 2026-10-05: the rest of the server, and production

- **Chat:** Ask runs on the server. The agent loop, its tools and the `/` commands are ported, and `start_build` starts the job itself; the web app only sends what was typed and draws the events. The conversation is kept on the server.
- **Voice:** spoken questions during playback are answered by the narrator who was interrupted. Silence is caught by Silero (bundled, MIT) before any model call, so it is never billed.
- **Jobs:** collection naming and cover design run on the queue, one waiting run per collection.
- **JSON-RPC adapter:** the 47 old methods are served at their old paths for one release, marked deprecated, so existing agents keep working.
- **Community, beyond the old app:** Discover lists the items inside shared collections, which play or open in place. An author decides whether reused copies may be edited and shared again, and the original counts its reuses.
- **Settings:** General is the first tab, where Settings opens. It holds the theme, the language, and how collections are named and drawn.
- **Production:** Docker Compose with Caddy (`make up`), CI on every push, and Playwright tests of the main flows.
- **Still to do:** a real build on real models and speech compared with the Rust app (phase 4), then removing `crates/` (phase 5).
