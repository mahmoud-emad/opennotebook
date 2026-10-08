# OpenNotebook

[![CI](https://github.com/mahmoud-emad/opennotebook/actions/workflows/ci.yml/badge.svg)](https://github.com/mahmoud-emad/opennotebook/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

OpenNotebook is an open-source study studio, in the spirit of NotebookLM, that you can run yourself.

<img width="1706" height="931" alt="A collection in OpenNotebook, with its sources and what was made from them" src="https://github.com/user-attachments/assets/35eaeaa8-2068-4aa9-991e-88c50d6ce76c" />

Put your sources into a collection: PDFs, slides, web links, pasted notes, or a research report it writes for you. Then turn them into something you can learn from:

- **Narrated slide decks.** You can stop them at any point and ask a question out loud.
- **Video overviews.** Slides or a hand-drawn whiteboard, with chapters and captions that follow along word by word.
- **Audio overviews.** One host or two, talking it through.
- **Mind maps and study notes.**

Every claim links back to the passage it came from, so you can check it. When a collection is good, share it on Discover. Others can read it as it is, or copy it, make it their own, and share it again.

## Try it

You need Docker and an AI provider: [OpenRouter](https://openrouter.ai), OpenAI, Anthropic, Google Gemini, Mistral, Groq, DeepSeek, Together, xAI, a local [Ollama](https://ollama.com), or any OpenAI-compatible server.

```sh
git clone https://github.com/mahmoud-emad/opennotebook.git
cd opennotebook
cp deploy/.env.example deploy/.env   # set POSTGRES_PASSWORD
make up
```

Then open <http://localhost/ui/>. On first run, a short setup tour asks for a provider and its key. It tests the key, checks that the account has credit, and suggests a model for each kind of work. To skip the tour, put the key in `deploy/.env` instead (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …).

`make up` builds the images on your machine. To use the published ones instead:

```sh
docker compose -f deploy/compose.yaml pull
docker compose -f deploy/compose.yaml up -d --no-build
```

To serve it on your own domain with HTTPS, set `DOMAIN` and `PUBLIC_URL` in `deploy/.env`. Every setting is explained in [deploy/.env.example](deploy/.env.example).

## Models and voices

- **Models.** Connect one or more providers, in the setup tour, in Settings › AI providers, or with environment variables. Each kind of work uses a model your providers offer, and you can change it in Settings › Models. OpenRouter covers every feature with one key. Direct providers cover most of them; the tour shows which features have no model yet.
- **Voices.** By default it uses Microsoft's neural voices through Edge's free Read Aloud service, so you don't need a key. For heavier use, switch to Azure Speech (`OPENNOTEBOOK_TTS_PROVIDER=azure`), or to an OpenAI-compatible speech server such as [Speaches](https://speaches.ai) (`openai`). Spoken questions are transcribed by that same server.

Everything else (styles, voices, language, a spending limit) can be changed in the app's Settings.

## Working on it

The code is split into three folders:

| Folder | What's in it |
| --- | --- |
| `server/` | FastAPI on Python 3.14, Postgres 18 with pgvector, and background jobs on Procrastinate. All the real work happens here, including every model call. |
| `web/` | A React and TypeScript app. It talks to the server only through its REST API. |
| `deploy/` | Docker Compose with Caddy, Postgres, and an optional speech server. |

You'll need Python 3.14 with [uv](https://docs.astral.sh/uv/), Node 24 with pnpm, and Postgres 18 with pgvector. Put `DATABASE_URL` and `TEST_DATABASE_URL` in `~/.config/opennotebook/db.env` (the Makefile reads it from there), then run:

```sh
make dev     # the api on :8000, the job worker, and the web app on :5173/ui/
make check   # formatting, lint, types and tests, for both server and web
```

If you change the API, run `make api-client` to regenerate the web app's client.

See [CONTRIBUTING.md](CONTRIBUTING.md) before you open a pull request. The design and the reasoning behind each feature are in [docs/](docs/).

## License

MIT. See [LICENSE](LICENSE).
