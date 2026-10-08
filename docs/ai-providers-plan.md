# AI providers and first-run setup

Status: phases 1–3 built 2026-10-08 (server, web, docs); phase 4 is later work.

## 1. Goal

OpenNotebook must run on any AI provider that can do the work, not only OpenRouter:

- Keys can be given as environment variables or entered in the web app.
- Every key is tested before it is saved: that it is accepted, and that the account has credit.
- Until a working provider is connected, the web app shows a setup tour instead of the studio.

## 2. Where things stand

- **One client.** Every model call goes through one `Ai` client (`server/opennotebook/ai/client.py`).
  - It speaks the OpenAI chat-completions protocol.
  - It is built once from `OPENNOTEBOOK_AI_BASE_URL` and `OPENNOTEBOOK_AI_KEY`.
  - About 20 call sites use `client.ai()`: `.complete`, `.stream`, `.embed`, `.catalogue`, `.has_key`.
- **OpenRouter is assumed in several places:**
  - `usage.include` and `usage.cost`;
  - the `/models` price list;
  - `modalities` and `image_config` for pictures;
  - Sonar's `citations` for web search;
  - `vendor/name` model ids, both in the defaults and in `check_model`;
  - the out-of-credit sentence.
- **Keys are env-only by design.** `domain/settings.py` says "Secrets are not settings". There is no setup gate in the web app.
- **Speech has its own providers** (edge, azure, openai) and its own env keys.

## 3. What the providers offer

From the 2026-10-08 research. Only two providers report a balance through their API.

| Provider | OpenAI-compatible base URL | Gaps | Key check | Balance |
|---|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | none | `GET /key` | `limit_remaining` on `/key` |
| OpenAI | `https://api.openai.com/v1` | none | `GET /models` | none; 429 `insufficient_quota` |
| Anthropic | `https://api.anthropic.com/v1/` | ignores `response_format`, drops audio; no embeddings or images | `GET /models` | none; 400/402 billing |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | beta | `GET /models` (400 `API_KEY_INVALID`) | none; 429 `RESOURCE_EXHAUSTED` |
| Groq | `https://api.groq.com/openai/v1` | strict schema on few models only | `GET /models` | none |
| Mistral | `https://api.mistral.ai/v1` | no images | `GET /models` | none |
| DeepSeek | `https://api.deepseek.com` | `json_object` only | `GET /user/balance` | **yes** |
| Together | `https://api.together.ai/v1` | — | `GET /models` | none |
| xAI | `https://api.x.ai/v1` | — | `GET /models` | none |
| Ollama, LM Studio, vLLM, or any OpenAI-compatible server | the user's URL | per server | `GET /models` | n/a (local) |

Perplexity retired its own chat-completions API on 2026-09-27. Web search keeps working through OpenRouter's `perplexity/sonar`, and through any search-capable model.

**LiteLLM** is MIT-licensed, but it is large and had a PyPI hijack in 2026-03. A thin adapter of our own fits the project better (owner preference: our own code).

## 4. Design

### 4.1 Connections

A **connection** is one provider account. It has:

- an `id` slug (`openrouter`, `openai`, …);
- a `kind` (a preset);
- a `base_url`, a key, and whether it is enabled;
- its last check: whether the key works, the balance when known, and when it was checked.

The presets are in a registry, `ai/providers.py`. For each kind it holds:

- the label and the default base URL;
- the env var names (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `TOGETHER_API_KEY`, `XAI_API_KEY`, `OLLAMA_BASE_URL`);
- whether a key is required;
- its capabilities: strict JSON schema, embeddings, images, audio input, cost reported in usage, prices in the model list;
- how to check a key and how to read a balance;
- a suggested model for each role.

Connections come from two places:

- **Environment.** The vars above. The legacy `OPENNOTEBOOK_AI_BASE_URL` and `OPENNOTEBOOK_AI_KEY` become the `default` connection: its kind is `openrouter` when the URL is OpenRouter's, `custom` otherwise. Env connections are shown in the UI as "Set in the server's environment". They can be tested but not edited.
- **The web app.** Stored in a new `ai_providers` table (migration 0009). The key is encrypted with Fernet (`cryptography`, Apache/BSD). The encryption key comes from `OPENNOTEBOOK_SECRET_KEY`; if that is unset, one is generated once into `<files_dir>/.secret.key` with mode 600.

**Who may change connections:** whoever may change instance settings, the same rule as `may_change` (the local owner in `local` mode).

**Keys are for the whole studio** (decided 2026-10-08). The open-source edition is cloned and run by one person or one team with their own keys.

A hosted edition will come later. It will run behind an OpenRouter-like broker with prepaid credit and a fee per generation. Nothing in this plan bills anyone: the broker is one more OpenAI-compatible connection, so it needs no special code here.

### 4.2 Models name their connection

- A model setting holds `connection:model`, for example `openai:gpt-5.4` or `openrouter:anthropic/claude-haiku-4.5`.
- An id without a known connection prefix belongs to the **primary** connection. This keeps every stored value and env default working. Ollama ids such as `llama3.2:3b` are safe, because the part before the colon is not a connection.
- The model defaults follow the primary connection's suggested model for each role.
- A role the primary connection cannot serve (no audio input, no images) falls to the first connected provider that can serve it. If none can, the feature says so in a sentence instead of failing mid-build.
- `check_model` accepts `connection:model` and checks it against that connection's model list.

### 4.3 The router

`client.ai()` returns a `Router` with the same surface as today's `Ai`: `complete`, `stream`, `embed`, `catalogue`, `aclose`. Call sites do not change.

- It resolves the model reference, then calls that connection's `Ai` with the bare model id.
- `has_key` becomes `await ai().ready()`. Five call sites change.
- The connection list is cached for 30 s and dropped at once when a connection is saved, the same way settings are cached.

Each connection's `Ai` adapts the request to its provider:

- `usage.include` and `stream_options` are sent only where the provider accepts them.
- A provider without strict JSON schema gets `json_object` with the schema in the system message, or plain prompting.
- Pictures are requested only where the provider can make them.
- Prices come from the model list where it has them (OpenRouter, Together). Elsewhere a call is "unpriced", as now. A small built-in price table for the suggested models comes later.

### 4.4 Testing a key

`ai/check.py`: `check(connection) -> Check`. The result is `ok`, `bad_key`, `no_credit`, `unreachable` or `not_compatible`, with a readable `sentence` and a `balance` when known.

1. **Is the key accepted?** Use the provider's key endpoint (OpenRouter `/key`, DeepSeek `/user/balance`), otherwise `GET /models`. 401/403 and Gemini's `API_KEY_INVALID` mean a bad key.
2. **Is there credit?**
   - Where a balance endpoint exists, read it. Zero or less means no credit.
   - Where none exists, send a one-token request to the cheapest suggested chat model. It costs a fraction of a cent and is recorded in the ledger as kind `setup`. A 402, or a 429 quota error, means no credit.
3. The result is stored with the connection and shown in the UI: "Connected · $4.12 left", "Connected · balance not reported", or the sentence for the failure.

### 4.5 API

- `GET /api/setup`: whether the studio is ready, whether this person may set it up, and the connections with their status.
- `GET /api/ai/providers`: the presets and the connections.
- `POST /api/ai/providers/check`: test a key that is not saved yet.
- `PUT /api/ai/providers/{id}`: test, then save. A failing key is refused with its sentence.
- `DELETE /api/ai/providers/{id}`.
- `POST /api/ai/providers/{id}/check`: test again.
- `GET /api/ai/providers/{id}/models`: the model list, for choosing models.

### 4.6 Web: the setup tour and Settings › AI providers

- **At start**, the app reads `/api/setup` before it draws anything. While the studio is not ready it shows only the setup tour, with these steps:
  1. **Welcome**: what OpenNotebook needs.
  2. **Choose a provider**: the presets, OpenRouter first as the one-key option for every feature.
  3. **Key**: paste it (or a URL for Ollama or a custom server), then press **Test**. The tour shows whether the key works and the balance, or the sentence for the failure. Only a passing key is saved.
  4. **Models**: the suggested model for each role, taken from the provider's list. Each can be changed, and roles the provider cannot serve are flagged. Another provider can be added here.
  5. **Done**: opens the studio.
- **Without permission:** a person who may not set up the studio (`keys` mode) sees one sentence telling them to ask whoever runs it.
- **Settings › AI providers:** a new tab that reuses the same components to add, test, remove and re-test connections.
- **Styling:** the setup tour and the tab use the existing `style.css` classes and dialog markup. No new styling system.

### 4.7 Readable errors

- The no-key sentences no longer name `OPENNOTEBOOK_AI_KEY`. They point to Settings › AI providers, or to the person who runs the studio.
- `credit_sentence` names the provider of the connection that failed.

## 5. Phases

1. **Server core:**
   - the registry, the connection store with encryption, the router, the per-provider adaptations, `check`, the API and migration 0009;
   - tests with a mocked transport for every provider kind.
2. **Web:**
   - the setup gate and tour, and the Settings tab;
   - the regenerated API client, and unit tests.
3. **Docs:** `.env.example`, `server/README.md` and the README quick start.
4. **Later:**
   - speech keys in the UI (Azure key and region, an OpenAI-compatible speech URL);
   - a built-in price table;
   - per-person keys, once Q4 is decided.

## 6. Verification

- `make check`: ruff, pyright, pytest and the web lint, tests and build.
- On the lab, a real run:
  - with the OpenRouter key from `ai.env`;
  - with a deliberately bad key, which must be refused with a readable sentence;
  - with the env key unset, which must bring up the tour.
