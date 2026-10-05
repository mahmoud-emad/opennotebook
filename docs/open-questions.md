# OpenNotebook, open questions

Decisions the owner has not made yet, kept in one place so they are not lost while the [stack migration](stack-migration-plan.md) is built. Each says what it blocks, when it has to be answered, and what the work assumes until then, so nothing waits on it unless it has to.

When a question is answered, move it to [Answered](#answered) with the date and the answer, and update the documents it names.

## Product and distribution

### Q1. Open source, enterprise, or both?

- **Why it matters:** it decides the project's licence, how it is packaged, what support means, and whether features are split between a free and a paid edition.
- **Blocks:** Q2, Q4, the deployment docs, and the public release.
- **Decide by:** before the first public release.
- **Until then:** the repository stays MIT (`LICENSE`), as it is today, and nothing is split into editions.

### Q2. Where does it run?

Self-hosted with Docker Compose, a cloud deployment with managed Postgres, or both.

- **Why it matters:**
  - **Search:** some managed Postgres services do not offer `pg_textsearch`, the BM25 upgrade (plan §5).
  - **Generated files:** on a disk volume, or in S3-compatible storage.
  - **Operations:** backups, scaling the worker, HTTPS and domains.
- **Blocks:** the production deployment (plan phase 4).
- **Decide by:** before the first production deploy.
- **Until then:**
  - Docker Compose for development.
  - Everything configured by environment variables.
  - Files behind one storage interface, so a disk or S3 can be chosen later.
  - No Postgres extension beyond pgvector.

### Q3. Will customers run it on their own servers?

- **Why it matters:** the speech container (Speaches) includes GPL code: `piper-tts` (now `piper1-gpl`), and `phonemizer` / espeak-ng pulled in by `kokoro-onnx`.
  - **As our own network service:** GPL is not triggered.
  - **Shipped for customers to run:** the GPL's obligations apply to that container.
- **Options:**
  - a legal sign-off;
  - a speech image without Piper, using misaki (Apache-2.0) for phonemes;
  - a different TTS server.
- **Blocks:** shipping to customers; nothing in the migration.
- **Decide by:** after the migration, before any on-premises customer.
- **Until then:** speech stays a separate, swappable service behind the OpenAI-compatible API (plan §11).

## Accounts, billing and credits

### Q4. Subscriptions, bring your own key, or both?

The owner's direction (2026-10-04): a credit and usage calculator will follow, for subscriptions or for people bringing their own AI key. Which one is not decided.

- **Why it matters:**
  - **Bring your own key:** keys become per-user secrets, stored encrypted, and calls go out with the person's key.
  - **Subscriptions:** plans, credit balances, a price list (provider cost or our own price, with or without a margin), and payment.
- **Blocks:** the calculator service and anything about plans or balances.
- **Decide by:** before the calculator work starts.
- **Until then:**
  - Every model, speech and search call is written to `usage_events` (plan §5), with its owner, purpose, model, tokens or seconds, and cost.
  - The calculator can be built on that ledger later without changing the code that makes the calls.
  - The AI key stays one instance-wide environment variable.

### Q5. What is the credit calculator, exactly?

A separate service, or a module of the server? Does it enforce limits before a call (a balance check, like today's spending limit, which is checked against the high estimate) or only count after? Is a credit a dollar, a token or something else?

- **Blocks:** the calculator itself.
- **Decide by:** with Q4.
- **Until then:** the existing per-build spending limit (`MAX_BUILD_USD`, default $0.50) stays, counted per user.

### Q6. How do people sign in?

- **Options:**
  - email and password;
  - a magic link by email;
  - Google or GitHub;
  - SSO (OIDC or SAML) for enterprise.
- **Blocks:** any deployment with more than one person.
- **Decide by:** before the first multi-user deployment; ideally during plan phase 1.
- **Until then:**
  - The `users` table exists, and every record has an owner.
  - Sign-in sits behind one FastAPI dependency, with a single local user in development.
  - Agents and scripts use API keys that belong to a user.

### Q7. Do collections belong to a person, or to a team?

- **Why it matters:** sharing a collection, or a company workspace, means an owner that is a workspace, not a person, plus roles. It is cheaper in the first schema than added later.
- **Blocks:** the first Alembic migration, if teams are wanted soon.
- **Decide by:** during plan phase 1.
- **Until then:** `owner_id` is a user.

### Q8. Who administers an instance?

- **Why it matters:** with users, the models, endpoints and defaults (`instance_settings`, plan §5) need someone allowed to change them.
- **Decide by:** with Q6.
- **Until then:** instance settings come from the environment and a seeded table; there is no admin screen.

## Data

### Q9. Retention, deletion and export

How long are sources, outputs, chat history and usage kept? Can a person export everything, or delete their account and all of it (GDPR)?

- **Blocks:** a public or enterprise launch.
- **Until then:**
  - Deleting a collection deletes everything in it, as today.
  - Usage rows are kept, because billing will need them.

### Q10. Search in languages other than English

- **Why it matters:** the studio writes in 11 languages and accepts sources in any of them. A `tsvector` built with the `english` configuration stems English only, so keyword search on other languages is weaker.
- **Options:**
  - the `simple` configuration;
  - one configuration per source, chosen by its detected language;
  - lean on vectors for non-English sources.
- **Decide by:** plan phase 2b, by measuring on real sources.
- **Until then:** the plan assumes `english`.

## Engineering, decided by measuring

These are answered by the work itself, not by the owner. They are listed so the result is written down.

| # | Question | Answered in |
|---|---|---|
| E1 | Does markitdown keep sources verbatim on our fixtures (no `_`/`*` escaping, no lost text), or do we port our own converter? | Plan phase 0 |
| E2 | Do tool-call streaming and structured outputs work through the openai SDK on every model in the settings catalogue, via OpenRouter? | Plan phase 0 |
| E3 | Is `ts_rank_cd` good enough for retrieval, or is BM25 (`pg_textsearch`) needed? Depends on Q2 | Plan phase 2b |
| E4 | Does Silero (`vad-web` in the browser, ONNX on the server) end a spoken question as well as today's detector? | Plan phase 3 |
| E5 | When can the JSON-RPC adapter be removed: which outside agents still call it? | The release after cutover |
| E6 | Do mind maps and study notes need to become background jobs (they are synchronous today, with 60 s and 120 s limits)? | Plan phase 2b |

## Answered

| Date | Question | Answer |
|---|---|---|
| 2026-10-04 | Does anything besides the web app drive the studio? | Yes. Everything is driven by the server; the web app is only a client and performs no operations; all AI calls are on the server. A JSON-RPC adapter is kept for one release |
| 2026-10-04 | Carry over existing data? | No, start fresh |
| 2026-10-04 | User accounts in this migration? | Yes: a `users` table and an owner on every record; a credit calculator follows later (Q4, Q5) |
| 2026-10-04 | Chat history | On the server; everything loads from the server |
| 2026-10-04 | Keep FastAPI? | Yes (plan §0) |
