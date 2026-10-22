# Plan: study with your sources (search, context, notes, transformations)

Status: all four phases built 2026-10-22.
Read first: [design.md](../design.md) for the look, [phase1-spec.md](../phase1-spec.md) for sources and Ask.

## 1. What and why

[Open Notebook](https://github.com/lfnovo/open-notebook) (lfnovo, MIT) is the best-known self-hosted NotebookLM alternative. Its strength is working *with* sources: search, chat, saved notes, reusable prompts, and control over what the model sees. We already make richer outputs (narrated decks, whiteboard video, mind maps) and have Discover and reuse, which they lack. What we lack is the plain "work with my sources" loop. This plan closes that gap without leaving our identity.

What already exists (checked in the code, not assumed):

- Ask chat with `/` commands, web research and cited answers (`api/chat.py`, `agent/`, `web/src/ui/chat.tsx`).
- `/api/collections/{cid}/ask` takes a `sources` filter.
- Hybrid retrieval, full text plus embeddings, over `chunks` (`memory.search`). It is only used by the live voice Q&A, so people cannot search their own sources.
- Notes can be added as a source (`AddNote`), but a chat answer cannot be kept as one.
- Every reader (`reading.read_docs`, `memory.search`) takes every source; a person cannot leave one out without deleting it.

## 2. Phases

Each phase is shippable alone: migration if needed, server, tests, web, regenerated client.

### Phase 1: sources in or out of context

A source can be switched off. It stays in the collection and in the list, but nothing reads it: not Ask, not search, not any maker (slides, audio, video, mind map, notes).

- `sources.active BOOLEAN NOT NULL DEFAULT true` (migration 0011).
- `PATCH /api/collections/{cid}/sources/{name}` with `{"active": bool}`; `SourceOut.active`.
- `reading.read_docs` and `memory.search` skip inactive sources when no names are given. A name given on purpose still reads an inactive source? **No**: naming an inactive source is refused with a readable error saying to switch it on, so a stale agent call does not quietly use a source the person excluded.
- Cost estimates follow, since they come from `read_docs`.
- Reuse (copy) keeps the flag. Read-only copies show it but cannot change it.
- Web: a switch on each source row; an off row is dimmed and says "Not used".

### Phase 2: search your sources

- `GET /api/collections/{cid}/search?q=&limit=` returns passages from `memory.search`: source name and title, the excerpt, its offsets in the source text. Active sources only. Empty query is a 422 with a readable message.
- Web: a search box above the source list; results replace the list while there is a query, each showing the source and a highlighted excerpt; a click opens that source (existing source view).
- No model call is made for the full-text half; the embedding half costs one embedding call and falls back to full text alone when no embedding model is configured (already how `memory.search` behaves).

### Phase 3: keep an answer as a note

- Web: an assistant message with text gets "Save as note". It adds the answer, with its citations as a footnote list, as a note source via the existing add-note endpoint. No new server endpoint.
- The note is then a source like any other, so it can feed decks, search and the next question.

### Phase 4: transformations

A transformation is a named prompt run over the sources (or a few of them) that produces a note.

- Built in: Summary, Key claims, Flashcards, Timeline, Open questions.
- Custom: `transformations` table (owner, name, prompt), `GET/POST/PUT/DELETE /api/transformations`.
- `POST /api/collections/{cid}/transform` with `{transformation, sources?, focus?, save}`: runs the prompt over `read_docs` with the chat model, under the spending ledger like Ask, returns markdown with citations; with `save` it also keeps the result as a note source.
- Chat: `/transform <name> [focus]`, listed in the `/` menu and `/help`.
- Web: Settings gets a "Transformations" tab to add, edit and delete custom ones. Built-ins are shown, read only.

## 3. Not now, and why

- **Multi-speaker podcasts (up to 4).** Touches the script writer, voice casting and the player's speaker model. Worth its own plan.
- **More UI languages.** Needs a translation workflow first; the server already owns the wording ([design.md](../design.md)).
- **Provider capability matrix.** The setup tour already reports which features have no model.

## 4. How each phase is checked

`make check` for both halves, plus: `alembic check` and the OpenAPI diff for the schema changes; for each endpoint, tests for a not-owner request, an unknown collection, an inactive source, and the readable error text. Run on a machine with Postgres 18 and pgvector.

## 5. Order and risk

1 then 2 (search must honour phase 1), then 3, then 4. The only schema changes are `sources.active` and `transformations`. The riskiest part is phase 1 touching every reader; the tests list them so none is missed.
