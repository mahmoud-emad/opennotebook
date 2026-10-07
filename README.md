# OpenNotebook

A social learning studio, in the spirit of NotebookLM. Gather sources into a collection (files, links, pasted notes or a web research report) and make things from them: narrated slide decks you can interrupt with a spoken question, audio overviews, mind maps and study notes, every claim cited to its passage. Share a collection on Discover, and reuse what others shared, as a read-only copy or one you can change and share again.

## Stack

- **server/**: FastAPI on Python 3.14, Postgres 18 with pgvector, background jobs on Procrastinate. All the work happens here, model calls included.
- **web/**: a React and TypeScript app served at `/ui/`. It is only a client of the server's REST API.
- **deploy/**: Docker Compose with Caddy, Postgres and an optional speech server.

## Run it

With Docker:

```sh
cp deploy/.env.example deploy/.env   # set POSTGRES_PASSWORD and OPENNOTEBOOK_AI_KEY
make up                              # then open http://localhost/ui/
```

For development you need Python 3.14 with [uv](https://docs.astral.sh/uv/), Node 24 with pnpm, and Postgres 18 with pgvector. Put `DATABASE_URL` and `TEST_DATABASE_URL` in `~/.config/opennotebook/db.env` (the Makefile reads it), then:

```sh
make dev     # api on :8000, the job worker, and the web app on :5173/ui/
make check   # formatting, lint, types and tests, server and web
```

## What it needs

- **An OpenAI-compatible model endpoint.** OpenRouter by default: set `OPENNOTEBOOK_AI_KEY` (`OPENROUTER_API_KEY` works too). Any `/chat/completions` server works through `OPENNOTEBOOK_AI_BASE_URL`; the default model ids are OpenRouter's, so set the models in Settings for another endpoint.
- **Voices:** Microsoft's neural voices by default, free through Edge's Read Aloud service with no key. For production, set `OPENNOTEBOOK_TTS_PROVIDER=azure` with an Azure Speech key; or `openai` for an OpenAI-compatible server such as [Speaches](https://speaches.ai). Spoken questions are transcribed by that server at `OPENNOTEBOOK_TTS_BASE_URL`.

Every variable is listed with its default in [deploy/.env.example](deploy/.env.example). Everything a person chooses (styles, voices, language, spending limit) is in the app's Settings.

## Docs

- [Stack migration plan](docs/stack-migration-plan.md): the move from Rust to this stack, its decisions and progress
- [Open questions](docs/open-questions.md): decisions not made yet and what each one blocks
- [Design](docs/design.md): how the studio looks and why
- Specifications: [phase 1](docs/phase1-spec.md) (narrated decks), [phase 2](docs/phase2-spec.md) (spoken questions), [mind maps](docs/mindmap-spec.md), [study notes](docs/study-notes-spec.md), [audio overviews](docs/audio-overview-spec.md), [video overviews](docs/video-overview-spec.md)

## Licence

MIT, see [LICENSE](LICENSE).
