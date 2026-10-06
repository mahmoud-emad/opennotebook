# OpenNotebook, phase 1 specification

Static session: resources in, narrated deck out, plays start to finish. No interruption, no live mic, no streaming.

> **Written against an earlier service stack.** This spec dates from September 2026, when the studio was one service among several: an external slide renderer, a retrieval service with two separate stores, a local Kokoro TTS server, an embedding service, a job supervisor that also held secrets, and a key-value store. The decisions and the measurements stand; the plumbing does not. Today the studio is one binary, `opennotebook_server`: retrieval is `opennotebook_memory` (one SQLite store), sessions, collections and prep jobs are SQLite rows, a prep runs as a child process of the server, model calls go through `opennotebook_ai` to any OpenAI-compatible endpoint, speech goes through `opennotebook_speech` to any OpenAI-compatible speech server, and the studio writes its own slides. Below, a component of that time is named by its role. The phase 0 verification record and the stack survey this spec cites were not carried into this repository.

Everything here rests on the phase 0 verification run and on the slices built since. Where this spec states a number or a behaviour, it was measured or called, not read from a schema. It has been amended several times as building revealed things the draft had wrong; [Amendments](#amendments) is the dated list, newest first, and is the place to look before trusting a number.

## 0. Decisions locked

These are settled for phase 1. Changing one is a spec change, not a judgement call during implementation.

| Decision | Choice | Why |
|---|---|---|
| Audience | One listener, one browser page | Every verified path is single-listener plumbing. A room needs LiveKit plus a synthetic publisher that exists nowhere in the fleet. |
| Audio engine | Local Kokoro, through a local TTS server | **The lock stands; the stated basis does not.** 0.20x to 0.24x realtime was measured on other hardware. On `dev3omda` it is 1.09x to 1.22x. See the 2026-09-14 amendment on the stale basis. |
| LLM for outline and script | An in-process LLM client, linked directly (today `opennotebook_ai`) | The client the slide renderer and the voice service already used. Admitting a separate AI broker service for two calls would buy streaming phase 1 does not use and metering nobody wants. |
| Narration delivery | Pre-rendered to files before playback | Removes the only live-audio-out dependency, the AI broker's SSE, from phases 1 to 3. |
| Playhead owner | The studio | `slide_html_get` returns self-contained HTML, verified with all network blocked and no base URL. The slide renderer never learns a session exists. |
| Narration storage | Studio-side, keyed by `SlideRef` | The slide renderer has no field for spoken text and no duration. Leave it untouched; revisit only if a second consumer appears. |
| Speaker count | A field, not a phase | Every narration line carries `speaker_id` from day one. One narrator is the one-speaker case. Two speakers is config. |
| Line timing | `duration_ms` from the WAV header; `cues[]` stays empty | Phase 1 advances at line boundaries and nothing reads sub-line timing. Deriving cues costs a further 0.28x realtime and returns 11.6 s granularity that cannot survive a re-render. |
| Q&A dimensions | Fixed set: `architecture`, `technology`, `product`, `business` | The four of the retrieval service's seventeen that describe what a document says rather than what a work session did. Passed in by the studio, never defaulted inside the function. See below. |
| Verbatim extraction | Vendor `convert2md` | Four deps, no AI client, `Vision` trait passed `None`. Verified by reading the implementation. |
| Session prep | A supervised job with progress | `deck_create` runs 20 to 37 s per slide. A 20-slide deck is 7 to 12 minutes. Not a request anyone waits on. |

The AI broker service is not a dependency in phase 1. It stays available as a quality upgrade, not a fallback.

### Why those four dimensions

The retrieval service shipped seventeen, and reading all seventeen descriptions they fall into three families.

**Seven describe a work session, not a document.** `bugfix` is "bugs identified, their root causes, and the fixes applied". `failures` is "work that was refused, loops that did not terminate". `tooling` is "which tools succeeded or failed". `preferences` is "durable preferences a person expressed". Also `invariants`, `performance`, `security`. Every one is scoped to something that happened while working, so on a user's uploaded PDF they extract nothing. They exist because this registry was built for an engineering org's own agent transcripts.

**Six are narrow slices of org material**: `commercial`, `financial`, `legal`, `news`, `people`, and to a degree `business`. Each fires only on a document of that specific kind.

**Four describe what a thing is and how it works**: `architecture` ("what it is for, where it sits in the system, who consumes it"), `technology` ("technical decisions, system integrations, protocols"), `product` ("features, user-facing behaviour"), `api` ("function signatures, exported types").

A listener in a narrated session asks what this is, how it works, and why it matters. That is the third family. The set is `architecture`, `technology`, `product`, plus `business` carried up from the second family, because a user who drops in a pitch deck or a strategy memo has material the other three cannot see. `api` is dropped: signatures and call patterns are reference-doc territory, too fine-grained to narrate.

**The honest caveat.** None of the seventeen is a general "explanatory content" dimension. They were written for one corpus and a learning studio ingests arbitrary material, so these four are the best available fit rather than a designed one. If retrieval feels thin in phase 2, the answer is more likely a new dimension than a different pick from these. (`server/opennotebook/memory/qa.py` carries the four over as `DIMENSIONS`, and a dimension is now a name and a description, `dimension_description()`.)

**Cost.** Measured 8.6 s for two small fixtures at two dimensions, and 23.8 s for three files at two dimensions in phase 0, so roughly linear in files times dimensions at about 4 s each. Going from two dimensions to four doubles it. Ten source documents at four dimensions is therefore around 2 to 3 minutes, which is why `qa_extract` now appears in section 3's prep budget.

**What it forecloses.** A session over a contract, a board pack or an org chart retrieves worse than one over a technical document, because `legal`, `financial` and `people` are not extracted. A session over a codebase misses `invariants` and `security`. The set is fixed for phase 1, so a user cannot tune it, and there is no per-session override.

**Two alternatives, rejected for now.** *Deriving the dimensions from the material* would give section 3's outline call a second responsibility, choosing extraction dimensions, that nothing has yet shown it needs; it can be added later without changing this contract. *Asking the user* puts seventeen internal dimension names in front of someone who has just dropped in a PDF, which is a configuration screen where a progress bar belongs.

The parameter stays required with no default. The studio passes the fixed set; it is not baked into the function, so changing it is a change to a caller and not a code change in the ingest path.

Phase 1 therefore depended on four services, and all four were exercised in the phase 0 run: the slide renderer, the retrieval service, the local Kokoro TTS server and the embedding service. Plus a job supervisor for jobs and secrets, and a key-value store underneath the retrieval service. Today none of these is a separate process: OpenNotebook needs an OpenAI-compatible LLM endpoint and, for audio, an OpenAI-compatible speech server.

## 1. Ingest, one function, two writes

The single most dangerous thing phase 0 found: the retrieval service had two stores behind one word. `collection_import` feeds Q&A and ontology. `index_add` feeds semantic search. Neither knows about the other, neither keeps them consistent, and neither complains when they drift.

### The rule

Exactly one function in the studio performs ingest. Nothing else anywhere calls `collection_import` or `index_add` directly. Enforce it in review.

```
ingest_resources(session_sid, files[]) -> IngestResult
```

### The sequence

1. **Convert, verbatim only.** Each file through `server/opennotebook/convert` (`pdf.py`, `docx.py`, `pptx.py`, `xlsx.py`) to Markdown. Bytes in, model out, no network.
2. **Refuse what cannot be extracted.** A PDF whose pages fall under the 80 byte selectable-text threshold has no text layer. Refuse it with a clear message. Do not OCR and do not flag-and-continue: nothing downstream in phase 1 reads the flag, and OCR in the grounding store will be quoted back as if it were the document.
3. **Write to the session directory.** Materialise the Markdown into the session's directory with a `.collection` marker.
4. **`collection_import`** that directory.
5. **Read back and `index_add`** the same text into the same collection name.
5b. **`qa_extract`** on that collection, with the fixed dimension set from section 0. Without this step there are no Q&A pairs to assert on: `collection_import` alone yields zero, verified by probe on 2026-09-08. Step 6 asserted against a sequence that never produced what it asserts on.
6. **Assert both doors, by round trip.** Run `search` on that collection for a distinctive phrase known to be in the ingested material and assert at least one hit. Assert `qa_list > 0` alongside it, which covers the other door. If either fails, fail the ingest loudly. This is the whole defence against silent drift.

   The search assertion is a round trip on purpose, and `index_count > 0` is not a substitute. A count only proves rows were written. `search` embeds the query at read time, so the embedder is a live dependency of retrieval and not just of ingest, and no count of stored rows can observe that. Measured on 2026-09-08: with the embedding service stopped and restarted, the retrieval service kept reporting it Inactive for tens of seconds afterwards, during which `index_count` returned 3 and `qa_list` returned 14 while `search` returned an error. Both count-based assertions passed while retrieval was dead.

### The session directory names the collection

`collection_import` accepts a `name` field and ignores it. It names the collection after the imported directory's basename. Verified on 2026-09-08: importing `~/hct/probe` with `name: "probe_qa_gap"` returned a collection named `probe`.

**So the session directory's basename IS the collection name**, and the studio derives it from the directory rather than from the field. That is what makes the two writes safe: `collection_import` takes a path and `index_add` takes a name, and if the studio trusted the `name` field it passed, the two calls could name different collections and each would report success. The drift section 1 exists to prevent would then be created by the very function meant to prevent it.

This turns the session directory name into a uniqueness guarantee the rest of the spec leans on and never stated:

- **The session sid is the directory name is the collection name.** One value, three roles, so there is nothing to keep in sync.
- **The sid must be unique per workspace, not merely per session.** Two sessions colliding on a sid do not get two collections, they get one collection written twice, with the second import silently adopting the first one's documents.
- **It must be a legal single path segment and a legal collection name at once.** No separators, no leading dot, no characters the registry rejects.

Phase 1 does not specify how the sid is generated. It specifies that whatever generates it owns that guarantee.

### Session directory

Local filesystem, one directory per session, `.collection` marker at the root. The retrieval service's registry path needed real files on disk, so this was unavoidable rather than a design preference.

The session directory is the only storage phase 1 has. It is sourced from whatever the user uploaded in that request. A durable copy of the originals, and rehydrating a session from it after teardown, is phase 2 or later work. Phase 1 does not need to implement rehydrate. It needs to not preclude it, which means the session directory is derived state and nothing irreplaceable lives only there.

### One line on wire shapes, so nobody re-derives it

An oschema method's params take three forms: a named `$ref` type nests under its parameter name (`{"req": {...}}`), a bare scalar is named by its parameter (`{"sid": "..."}`), and only an inline anonymous object flattens (`{"name": ..., "force": ...}`). Every retrieval method ingest touched was the first form. The point that matters: Rust going through the generated SDK cannot get this wrong, because the Input types are generated from the same spec. It is a raw-JSON-RPC hazard only, and it was hit from a Python harness, not from the studio.

One more naming trap on the same domain: `QaSearchReq` takes `collections`, a list, while its sibling `QaScopeReq` used by `qa_list` and `qa_extract` takes `collection`, a single string. Same domain, same concept, different name and different arity.

### What ingest does NOT do

`bg_pdf_extract_async` is disqualified for the grounding store. Its prompt instructs the model to "intelligently restructure" and to "synthesize fragmented text into coherent paragraphs", preserving only numbers, dates and names. That is correct for drafting slides and poison for a store whose premise is quoting the user's own documents.

Slides may be drafted from an AI-structured extract. Grounding may not. These are two paths and they stay two paths.

## 2. The narration store

Studio-owned. This schema is the part that must be right on the first try, because phase 2 and phase 3 both read it.

```
Session
  sid
  title
  collection_name        # one name, both retrieval doors
  deck_ref               # collection + presentation in the slide renderer
  speakers[]             # Speaker; speaker_count is DERIVED from its length
                         # and is not a field. A stored count can disagree
                         # with the speakers present, and then two fields
                         # describe one fact.
  slides[]               # SessionSlide, ordered
  state                  # preparing | ready | failed
  prep_job_sid           # the prep job's row

Speaker
  speaker_id             # stable within a session
  voice_id               # e.g. af_bella, am_adam
  display_name
  role                   # free text; phase 1 does not interpret it

SessionSlide
  slide_ref              # addresses the slide renderer; studio adds no field there
  ordinal
  aspect                 # 1920x1080 (html) or 1376x768 (png)
  lines[]                # NarrationLine, ordered

NarrationLine
  line_id
  speaker_id             # REQUIRED, always, even with one speaker
  text
  audio_path             # rendered wav
  duration_ms            # read from the rendered WAV header, never estimated
  cues[]                 # {at_secs, end_secs, text}; EMPTY in phase 1
```

### Where the store lives

Section 0 names a key-value store as a phase 1 dependency and this section says the store is studio owned, but neither said where it lives or in what shape. Decided here.

**One JSON document per session.** At the time that was a key in the key-value store; today it is one row of the `docs (kind, id, body)` table in `opennotebook.db`, the SQLite database in the data directory, with kind `session` and the session sid as id. The sid is the same value that names the session directory and the retrieval collection.

Why not the alternatives. Files in the session directory were rejected because the session directory is derived state that teardown deletes. A graph or vector store models relationships and similarity, and a session is neither.

Why one document rather than one key per slide. The player reads a whole session, and splitting the write reintroduces exactly the two writes problem section 1 exists to prevent: two keys can disagree and nothing notices. One key is one atomic write.

**Two things measured on 2026-09-08 that this shape depended on.** A JSON document round tripped through the key-value store byte for byte, including embedded quotes and commas. And a missing key returned an empty value rather than an error, so absence and an empty string were indistinguishable by value alone; the store had to decide "not found" on the key's type, never on an empty value. That is the same silent empty class as the retrieval service's two doors, in a third place, and it is why the SQLite store answers a missing row with `None` rather than an empty document.

**Flagged as unspecified.** Nothing in the spec asked for this shape, so it is a decision rather than an instruction. Two things would change it: a session growing past a few megabytes of JSON, or section 3 wanting to write slides incrementally as each one renders instead of once at the end. Either pushes toward a key per slide, and both are cheap to reach from here because the key prefix already namespaces by session.

### Three constraints on this schema

**`speaker_id` is required on every line from day one.** This is why two-speaker is not a phase. A store that gains a speaker field later forces a migration and a player rewrite; a store that has it from the start makes "two speakers" a prompt change and a second voice id.

**`duration_ms` is measured, not estimated.** Read it from the WAV header of the file that will actually be played. Kokoro is not deterministic: the same voice and the same text produced different bytes and about 6 ms of length drift across renders. The renders correlate above 0.99 and the pitch matches, so it is genuinely the same speaker, but any duration carried over from a previous render is wrong by an unbounded amount.

**`cues[]` is empty in phase 1, and its eventual producer is an open question.** Do not write "the voice service populates cues" into this spec or into the code. `transcribe_cues` works and is lossless, but on synthesised speech it returned two cues of exactly 11.61 s for a 23.22 s clip, breaking mid-sentence between "a start." and "offset,". That is not VAD failing. It is VAD having nothing to segment on, because TTS output has no pauses. A better cue method will not fix it, because the property belongs to the input.

When something does need sub-line timing, the likely answer is to make the sentence the synthesis unit rather than to transcribe after the fact. Synthesise per sentence and every boundary is exact and free from the WAV headers: no STT pass, no 0.28x cost, and no drift across re-renders because each chunk carries its own duration. The cost is prosody across sentence joins, which may or may not be acceptable. That is not a phase 1 decision and this spec does not make it.

## 3. Session build, a job not a request

Submitted as a job, following the precedent the slide renderer used for its own renders. Emits progress. The user watches a prep screen. Today the job is a row in the SQLite `jobs` table and the work runs in a child process, `opennotebook_server prep --spec <file> --job <id>`, one prep at a time; it survives a server restart.

```
1. ingest_resources()                        seconds
2. outline from the collection               one LLM call
3. script generation, per slide              one LLM call per slide
4. deck_create(DeckCreateSpec)               20 to 37 s per slide   <-- dominant
5. narration synthesis, per line             0.20x to 0.24x realtime
6. render validation                         see below
7. state -> ready
```

Step 4 dominates everything. For a 20-slide deck:

| Stage | Time | Condition |
|---|---|---|
| `deck_create` | 7 to 12 minutes | 20 slides at the measured 20 to 37 s each |
| `qa_extract` | 2 to 3 minutes | ten source documents at four dimensions, from a measured 4 s per file-dimension |
| Narration synthesis | about 2 minutes | roughly 30 s of audio per slide at 0.22x, with `cues[]` empty |
| Narration plus eager cue derivation | about 5 minutes | the same, if cues are ever populated at build time, at a further 0.28x |

The middle row is what phase 1 does. The third row is recorded so the number carries its condition instead of floating free, because populating cues eagerly roughly doubles the audio stage.

Progress reporting should be honest about which stage is running, because "generating slides" is where nearly all the wall clock goes.

### Two numbers section 3 did not give, now decided

**Five slides by default.** Not because five feels right, but because `deck_create`'s own `count` defaults to five when absent, and this script is what gets handed to `deck_create`. Two different defaults for one number is a bug waiting to happen. The caller may override it; nothing derives it from the volume of source material, because nothing measured says how much material makes a slide.

**650 characters of narration per slide.** Derived rather than picked. Kokoro measured 84 words in 23.22 s, so about 3.6 words per second. This section budgets roughly 30 s of audio per slide, which is about 108 words, and at close to six characters per word that is 650. The narration budget and the prep estimate therefore move together instead of drifting apart: change one and the other is wrong in a visible way.

### Script generation with character budgets

`deck_create` reported `status: succeeded, ok: true` on a slide with text overflowing its box, an accent underline striking through a word, and the literal placeholder `SUBHEAD` left in. `ok: true` does not mean presentable, and phase 1 runs unattended.

The cheap defence is upstream, not downstream: hard character budgets per slide field when the script is generated, and never emit a field the template will fill with a placeholder. Render-and-inspect is the expensive fix and it is not phase 1.

### Render validation is a liveness check, and only that

Every slide returns non-empty from `slide_html_get` or `slide_image_get` according to its format, every line has an `audio_path` with a non-zero `duration_ms`, and the ingest assertions from section 1 held. Anything else fails the job rather than producing a half-session marked ready.

**This catches an absent render, never a bad one.** The slide that motivated the whole `ok: true` trap returned 131,541 characters of perfectly well-formed HTML, and in it the title overflowed its box, the accent rule struck through a word, and the literal placeholder `SUBHEAD` was still there. A non-empty check passes that slide. It would pass it every time.

So quality does not rest here. It rests entirely on the character budgets applied upstream when the script is generated, which is why those are enforced on the string on its way out rather than requested of the model. If a slide is ugly, the budget was wrong or the template was; validation was never going to be what noticed.

## 4. The player contract

A plain browser page and an SSE event stream. No LiveKit, no SFU, no WebRTC.

### Rendering

Each slide's HTML in an `iframe srcdoc`. Verified self-contained: fed to a headless browser with `Network.setBlockedURLs: ["*"]`, `offline: true` and `Page.setDocumentContent` so there was no base URL at all. It rendered correctly with zero broken images, zero external URLs, zero `@font-face`. Fonts resolve to a system stack. The single image is inline base64.

Letterbox, do not assume 16:9. HTML slides are 1920x1080; PNG slides come back at 1376x768, which is 1.792:1. A player mixing both must letterbox.

### Playback

The studio holds the playhead. It plays `NarrationLine` audio in order, advances the iframe at slide boundaries, and emits events. The slide renderer's present mode is not used and its keyboard navigation is irrelevant.

### Where the bytes come from

**Bytes ride `/api/`, beside the JSON-RPC routes, on the one address the server listens on.** The studio serves narration audio from `/api/session/audio`, slide HTML from `/api/session/slide`, and section 4's event stream from `/api/session/events`. When this was written the studio sat behind a router on a Unix socket, and the survey had claimed that bytes and long-lived streams needed a socket of their own; executed on 2026-09-09, that was wrong, and byte routes and SSE served correctly next to JSON-RPC. Today the question does not arise: the FastAPI app in `server/opennotebook/main.py` serves the API and the byte routes (`server/opennotebook/api/media.py`) on one address, `:8000` in development, and the Vite dev server passes `/api` through to it (`web/vite.config.ts`).

### The change source, decided

§4 says the events go "over SSE" and never says how the studio learns that something changed. Decided during the player slice, and it is **two answers rather than one**, which is why it looked hard:

- **Prep state is written by another process.** `preparing -> ready | failed`, and the phase a prep is on, are written by the prep child process. The process serving the API cannot observe that except by asking, so this half is **polled** — the session row plus the prep job row, once a second, stopping as soon as the session reaches a terminal state.
- **Playback state is written by the serving process.** `playing`, `paused`, `finished`, the slide and line boundaries and the playhead are all created there, by the browser calling in. Polling that would be polling our own memory, so this half is **pushed** the moment it is made.

The stream is a merge of the two and neither half could replace the other. A one-second poll is invisible against phases measured at 20 to 37 seconds of rendering or 4.4 to 5.7 seconds of synthesis per line, and it is one local database read against §0's single listener.

Two alternatives were considered and are better later rather than now. The job supervisor's own event stream would have coupled the player to its event vocabulary and still not carried `Session.state`. A notification channel would have the prep child publish and the server subscribe, which is the right shape for many watchers and strictly more moving parts for one. Either becomes correct when a session has more than one watcher.

### Progress covers the whole prep, not the last two thirds

`steps_total` used to be set by `build_session`, so it stayed at 0 — the job contract's own "this job does not report" — through ingest and script generation, which are minutes of the wall clock. The phase list is now the pipeline's and has six entries: `ingest`, `script`, `deck`, `measure`, `narrate`, `validate`. §3 asks progress to be honest about which stage is running, and it now is for all of them.

`prep.progress {step, steps_done, steps_total}` is a **sixth event, added beyond §4's five**, because none of the five can carry a stage name: `session.state` is `preparing` for the entire prep. It is a strict addition, so a player written against §4 alone still works and simply shows less.

### Events, studio to browser, over SSE

```
session.state      preparing | ready | playing | paused | finished
slide.enter        {slide_ref, ordinal}
line.start         {line_id, speaker_id, duration_ms}
line.end           {line_id}
playhead           {slide_ordinal, line_id, offset_ms}
```

`line.start` carries `speaker_id` so the UI can show who is talking with one speaker or two. This is the phase 2 seam that costs nothing now.

## 5. Seams for phase 2 and 3

Phase 1 should not build these. It should not make them expensive either.

- **A pause that survives mid-line.** Phase 2 pauses on a keypress. If phase 1's playback can only stop at line boundaries, phase 2 rewrites it. Make pause work at an arbitrary offset and record the offset. Note that an arbitrary offset in milliseconds is all phase 2 needs to resume; resuming at a sentence boundary is a different and harder thing, and it is the open question in section 2.
- **The last N lines window.** Phase 2 sends the user's question to a model with the current slide and recent narration as context. Phase 1 already has both in the store; just make them queryable as a unit.
- **Which voice held the floor.** Phase 2 answers in the voice that was speaking. `speaker_id` on `line.start` is already that.
- **Nothing about the state machine.** Do not draft it, do not stub it. It is phase 2's actual work and guessing its shape now will be wrong.

### Push to talk is the answer, not a step towards barge in

Phase 1 ordered interruption as push to talk first and live barge in later, on the strength of the provider's own guide calling its realtime WebSocket experimental, very CPU heavy and demo only. That ordering was a guess from a document. It was measured on 2026-09-10 and again on 2026-09-14 against `development` at `9c847d3`, and the measurement says push to talk is where phase 2 stops, not where it starts.

The realtime path emits no transcription event at all. Not slow, not inaccurate: absent, across both input rates, both frame sizes, all three session config styles, three pacings, five trailing silence lengths and a freshly restarted process. The offline path transcribes the same bytes in the same process exactly. Reported upstream to the speech server's maintainers.

It also leaks connections. A client killed mid stream leaves its socket behind, the decoder stays pinned at 4.11 cores, and the endpoint stops accepting new work until the service is restarted. Also reported upstream.

So live barge in is **blocked on a realtime transcription path that works, not scheduled**. It is not a phase 3 item with a date; it is an item that cannot start until somebody gets one transcript out of that endpoint. Phase 2 should not carry a migration path towards it, should not shape its turn model around an onset event that does not arrive, and should not describe push to talk as interim.

Two measurements worth carrying into the phase 2 spec even so. `speech_started` is not a VAD onset event: it is queued only when the recogniser produces text, so it means a word was decoded rather than that a person started speaking, and a turn model built on OpenAI's semantics would be wrong about it. And the offline path costs about 26 core seconds per question in a burst, against a continuous 4.11 core drain for as long as a realtime socket carries audio, which is the shape difference that makes push to talk cheaper on a shared box rather than merely simpler.

## 6. Non-goals, explicitly out

Interruption of any kind. Live mic. Streaming synthesis. Two-listener or room sessions. The AI broker service. LiveKit. An external file store. `ontology_extract` and the concept map. Populating `cues[]`. Any change to the slide renderer.

The external file store is out because phase 1 has no durability requirement that the session directory does not meet, and because it is the one service the phase 0 run never called. Its surface is schema-read only. Bringing it in would put an unverified dependency in the critical path of a phase whose whole point is that every path is verified.

### Known gap: nothing deletes anything

A successful ingest leaves a collection behind in both retrieval stores and a directory on disk, and nothing in this spec removes either. There is no session teardown, no expiry, and no way to delete a session's material short of calling `index_delete_db` and `registry.collection_delete` by hand. The integration tests accumulate one collection per run, which is how this surfaced.

Phase 1 does not solve it. It is named here so phase 1 does not end believing it is handled. Whoever picks up teardown should know the two writes need two deletes. (Since solved: deleting an output or a collection removes its rows, its files and its retrieval index; see the README's Collections section.)

**One thing phase 1 had to do at once, because later would have been a migration.** The retrieval service's `collection_delete` was keyed on a registry id returned only by `collection_import`, not on the collection name, so `IngestResult` persisted it as `collection_sid`. `opennotebook_memory` deletes a collection by name, so that id is no longer needed.

### One correction to carry forward

The survey said Q&A and concept map come "for free" from the retrieval service. Half of that is verified: `qa_extract` produced 6 pairs from 3 files in 23.8 s, and `qa_search` ranks them. `ontology_extract` could not be exercised. It wanted an ontology registered in the key-value store, `ontology_list` was empty on this box, and `ontology_attach` has nothing to attach. The concept-map surface is unverified, not working. Whoever reaches for it registers an ontology first.

## 7. Known traps, as assertions

Each of these is a silent failure. Each gets a test that fails loudly.

| Trap | Assertion |
|---|---|
| Two stores, one word | After ingest: `search` for a known distinctive phrase returns a hit, AND `qa_list > 0`, both on the collection the session will query |
| All-or-nothing needs rollback, not just early conversion | Converting every file before the first write covers only pre-write failures. Anything that fails after `collection_import` leaves a collection that exists in one store and not the other. Measured 2026-09-08: with the scan refusal disabled, an image-only PDF reached `index_add` and left a collection with 1 indexed document and 0 Q&A pairs. Any failure after the first write undoes both sides. |
| A rollback that fails must be reported | Rollback is best effort, but silence is not. A rollback that itself fails leaves exactly the half-populated collection the rule exists to prevent, and nothing observes it. Carry the rollback failure as context on the original error; never swallow it. |
| A count cannot prove retrieval works | `search` embeds the query at read time, so the embedder is a live dependency of the read path. `index_count > 0` tests only the write path and passes while `search` errors. Never assert a count where a round trip is meant. |
| Concurrent `qa_extract` then `qa_search` on fresh collections can fail | Two tests seeding their own collection in one workspace, one millisecond apart, produced a bare `-32000 "vector_search"` from `qa_search` with no `data`. Serialising them made it pass with no other change. Section 3's prep job hits exactly this shape if it ever parallelises slide work. Note what the error does NOT do: unlike every other refusal in this project it names no missing prerequisite, offers no hint and carries no retryable flag, so there is nothing in it to act on. |
| `qa_search` ranks but does not carry content | Its `QaHit` is `id` and `score`, and `text` is optional and came back absent on a live call. `unwrap_or_default()` there grounds the model on empty strings and the generated output still reads fine. Take ranking from `qa_search`, content from `qa_list`, joined on id, and drop a hit whose pair will not resolve rather than passing an empty string on. Fifth instance of the silent-empty pattern. |
| `finish_reason` is the truncation discriminator | A model that runs out of room returns prose that reads whole, and nothing in the text shows it. Never use a completion's text without checking why the model stopped. |
| `search` scores are negative distances | Never threshold on `score > 0`. `qa_search` returns 0 to 1 similarities. Different scales, do not share a constant. |
| `ok: true` on a broken slide | Character budgets upstream; no field emitted that becomes a placeholder |
| Non-deterministic TTS | `duration_ms` read from the WAV that will be played, never carried across a re-render |
| Cue granularity is a property of the input | `transcribe_cues` documents VAD segmentation but falls back to fixed windows on TTS input, which has no pauses to segment on. The 11.6 s granularity is not a tuning problem and will not improve with a better cue method. Do not design sub-line sync around it. |
| A directory that becomes a collection is named by the directory | `deck_create` with `collection: "tbld1788953767880"` and `collection_path: "/tmp/opennotebook_build_decks"` registered a collection called `opennotebook_build_decks`, with one theme, `opennotebook_build_decks_default`. The `collection` field named nothing. Same shape as the retrieval service's `collection_import` in section 1, so it is a rule across two services rather than a quirk of one. This one is loud: `deck_create` refused the theme and listed what really existed. Give each collection its own directory named after it. |
| The response's content type is the audio discriminator, and a convenience wrapper can drop it | The speech client of the time base64 decoded the audio and returned bytes, discarding `content_type`. `response_format` accepts `"pcm"`, which has no header, so the two are reachable through one call and only that field tells them apart. Read the content type, or the RIFF header, before decoding; `opennotebook_speech` checks the WAV header and normalises every clip to 24 kHz mono 16-bit. Sixth instance of the silent-empty pattern. |
| `steps_total: 0` means "does not report", not "no phases" | The job row's field contract, kept in the SQLite `jobs` table. A prep job that leaves it at zero is indistinguishable from one that reports no progress at all, and a client can only render a spinner. Set it when the job is created. Reaching `steps_total` does not imply success either: read `status`. |
| `bg_file_upload` is collection-scoped | One collection per session, or sessions leak resources into each other |
| Paraphrasing extractor | Grounding store fed only by `convert2md`. Never by `bg_pdf_extract_async`. |
| Scanned PDF | Refuse at ingest with a clear message. No OCR into the grounding store. |
| An empty shell pipeline is not an empty answer | **Seventh instance, and the first one found in our own tooling rather than in a service.** A scan for absolute paths inside the migrated stores returned nothing, and the conclusion "no paths are baked in" was wrong: `strings` was not installed on the box, and the pipeline's exit status came from `grep`, which had simply read nothing. Redone with `grep -a` the paths were there. The rule is the same one this table already applies to RPC: read the discriminator, never the payload. For a pipeline the discriminator is the exit status of **every** stage (`set -o pipefail`, or check each), not the emptiness of stdout. It generalises past this stack — any tool that reports "not found" and "could not look" through the same empty channel needs the check. |
| A field that is always empty | `Session.deck_ref` was a `SlideRef` whose `slide` was permanently `""`, because a deck is addressed by collection and presentation and has no slide. An always-empty field teaches a reader to ignore it and gives a player an address that resolves to nothing — the same class of claim as a declared protocol with no route. §2 already described this field as "collection + presentation", so the fix was a two-field `DeckRef` and the three-field one was the implementation's shortcut. Either populate a field or remove it. |
| A stream's sentinel is a value the client will believe | The event stream opened each connection with an "impossible" previous playhead so the first tick would emit current truth. `line_id: "\0never"` is not impossible to a diff, and the opening frame a browser received over the public domain was a real `line.end {"line_id":"\u0000never"}` for a line that never started. Found by reading the live stream, not the tests. Start from the type's own default, which is a state the diff already handles, rather than from a value chosen to be unequal. |

## 8. Done means

A user drops in a PDF and a DOCX, waits through a progress screen, and watches a narrated deck play start to finish in one voice. Then flips `speaker_count` to 2, rebuilds, and watches the same material as a two-voice conversation, with no schema change and no player change.

That is the whole phase.

## Amendments

Newest first. Each entry says what changed and what forced it, so a correction is not re-derived from scratch.

### 2026-09-16, the create page could not take a link, so the conversation could not end

**The loop, as reproduced.** The greeting says "add anything you want me to read — paste a link, or type notes straight in". `POST /api/session/chat` did neither: it forwarded the message to a model and staged nothing. With nothing staged, `ready` — which the model was asked to decide — stayed false, the build button never appeared, the model asked another question, and the person answered it. Run against the live service on the paper this repo already cites, five turns produced five model calls, `ready:false` every time, and nothing on disk. The model filled the silence with work it was not doing: "I'll start reading the paper now", "I'll gather and analyze the content from both provided sources". It cannot read anything. Nothing had been submitted.

**The chat now stages what it is given.** Links in a message are fetched into the session's staging directory — the same directory `Add source` writes and `session_prepare` reads — and prose past 240 characters is kept as a note. Punctuation around a link is trimmed before the token is judged, because `(https://…)` did not start with `http` and was read as prose. The rows come back on the reply so the sources panel shows them.

**`ready` is read off the staging directory, not asked of the model.** It was only ever an offer of a button — a person decides whether a build happens — and a small model's reluctance to believe it had enough was the loop's engine. One readable source is enough.

**A message that is nothing but links never reaches a model at all.** The fetch either worked or it did not, and saying so is a report rather than a judgement. That is the commonest turn in this conversation and it now costs nothing. Measured on the transcript above: two turns and one model call, where it was five and five.

**The prompt says what the assistant cannot do.** It is told it is the two sentences beside the Start building button, that the work happens in a job it is not part of, that it cannot read, fetch, search, analyse or summarise anything here, and that a sentence beginning "I'll" is almost certainly a promise it cannot keep. It gets one question, counted off the transcript rather than trusted to its manners, and "everything", "go on" and "you decide" are recorded as complete answers.

**A model that is down no longer blocks a build.** An unreachable provider used to return its error as the whole reply with `ready` false — sources staged and no way to use them. The reply now carries what was staged and keeps the button.

**Section 7 gains a row: a format string is a thing a model will copy.** The first real prep after the fix failed at five minutes with `` line 1: `host_id` is not a speaker of this session ``. The session had one speaker, `host`, and the slide prompt said "Return only lines of the form `speaker_id: what they say`". `amazon/nova-micro-v1` blended the two. Both script prompts now show a real id from the session's own roster instead of the word `speaker_id`, and `parse_lines` resolves a tag that is a known id wearing capitals, punctuation or the word "id" — `**Host**`, `host id`, `host_id` are all `host`. A tag naming somebody who is not in the session is still an error, which is what the rule was for.

**Three more the first real prep found, once it got far enough to find them.**

*A prep submitted after an install could not start at all.* `dispatch::exe` takes the child's path from `current_exe`, which on Linux reads `/proc/self/exe` — and the kernel renders that as `/home/…/opennotebook_server (deleted)` once the file at that path has been replaced, which reinstalling the binary under a running server does every time. At the time the path went unquoted into a shell script, and bash answered `` syntax error near unexpected token `deleted' ``. The suffix is stripped now. The dispatched child is therefore the binary now at that path, which after an install is the new build rather than this process's own — the better of the two, since a prep should run the code that is installed.

*The bookend had no budget.* `script_one` spends the slide's 650 characters and then `generate_script` prepends up to two intro lines of up to `LINE` each, counted by nothing: a first slide could carry 1,290 characters, twice the thirty seconds of audio section 3 budgets. It went unseen because the bookend was being dropped for the `host_id` reason above — a limit nobody enforces is discovered by whatever stops hiding it. `budget.BOOKEND_NARRATION` is 320, enforced on the way out like every other budget here, and a bookend is now the only thing a first or last slide may carry beyond its own.

*A spent budget was emitting fragments.* With a few characters left, the loop fitted the line to what remained and kept it. A real run ended a slide on the spoken line "Moshi util" — not a shortened line, a destroyed one, synthesised and played. `budget.room` returns `None` below `MIN_LINE` and the line is dropped instead.

**The web bundle has its own build.** `web/` is a Vite project with its own `package.json`. `pnpm build` there builds it, and `make check-web` runs it after the lint and the tests.


### 2026-09-14, the stale basis under the audio engine lock

**Section 0's audio engine lock stands. The reason given for it does not.** The row said local Kokoro "synthesises at 0.20x to 0.24x realtime. Fast enough to pre-render a deck in minutes." That number came from the phase 0 verification run, whose record notes that it ran on `mahmoud-ashraf-devbox`. This project has since run entirely on `dev3omda`, a Xeon E5-2620 at 2.00 GHz. Re-measured there today against the same build, on an idle box:

| | `mahmoud-ashraf-devbox` | `dev3omda` |
|---|---|---|
| TTS, local Kokoro | 0.20x to 0.24x realtime | 1.09x to 1.22x realtime |
| STT, offline `stt_transcribe` | 0.060x to 0.100x realtime | 0.42x realtime |

Nothing was rebuilt between the two runs, and the 4x to 5x gap is consistent across two unrelated models on two different engines, so the explanation is the hardware and not a regression. This is the same staleness this project has now found in three other people's documents, and the rule it keeps proving is that a latency number is a property of a machine and belongs next to the machine's name.

**Two things the re-measurement found that the old number hid.** Kokoro's thread count was `clamp(1, 4)` in the TTS server of the time, hardcoded with no environment override, so 12 of this box's 16 threads are never used. And the engine sits behind one `Arc<Mutex<Option<OfflineTts>>>`, so synthesis is serialised process-wide: measured at 1, 2, 4 and 6 concurrent requests, the provider held 3.79 cores and returned 0.84x realtime of audio in every case, with each caller's wall time growing linearly. Concurrency buys nothing and a second caller queues rather than contends.

**What the decision now rests on.** Not speed, and not the absence of anything faster. There is something faster and it is reachable today. The lock now rests on a narrower claim: that nothing faster is also **local**, and that sovereignty is worth the wait for narration, which is a batch job nobody watches. Whether it is worth the wait for a phase 2 answer, which a person does watch, is a different question and section 0 does not settle it:

- **A cloud TTS route** through an AI broker could not be used: the only key available was `OPENROUTER_API_KEY`, and OpenRouter serves no `/audio/speech` model (`openai/tts-1` returns `Model openai/tts-1 does not exist`).
- **An audio-output chat model is reachable and fast.** OpenRouter carries `openai/gpt-audio` and `openai/gpt-audio-mini`, which take text in and return speech in the same streamed call. Measured: `gpt-audio-mini` puts the first audio byte on the wire at **846 to 916 ms** and delivers a 10 to 11 second answer in 1.8 s, which is 0.19x realtime and therefore never underruns; cost is **$0.0007 per question**, less than the text-only answer call it would replace. `gpt-audio` is 563 to 629 ms and $0.014. This is the same LLM path section 0 already locked for the script. What it costs is sovereignty for that one call, and the answer arriving in a provider voice rather than the session's own.
- **A GPU** would change the arithmetic and there was no path to one. The TTS server's `sherpa-onnx` v1.13.3 build bundled a CPU-only onnxruntime, and no GPU node was available. (Today the speech server is whatever `OPENNOTEBOOK_TTS_BASE_URL` points at, so a GPU-backed Speaches or Kokoro-FastAPI is a configuration change.)

So the lock is now held by sovereignty rather than by adequacy, which is a weaker and more honest thing to hold it by. It is still the right call for narration. It is an open question for phase 2's answer path, and the measurements that decide it are in the phase 2 investigation rather than here. What would reopen the narration half is a GPU node or the four-thread ceiling being lifted upstream.

### 2026-09-14, the debt slice

**Section 5 gains a decision the measurement forced.** Push to talk is phase 2's answer rather than a step towards live barge in, and barge in is blocked on a working realtime transcription path rather than scheduled. The realtime WebSocket accepted audio and emitted no transcription event; it also leaked connections and pinned four cores. Both were reported upstream. The phase 1 ordering came from the provider's guide, not from a measurement, and the measurement inverts what it implied.

**The png path works now and the letterbox branch has run against real png output for the first time.** `/api/session/slide` asked `slide_html_get` unconditionally, which on a png deck returns an empty string with a successful result, so the wrong door looked like a working one and only the liveness check made it visible. The route now reads the format from `slide_get`, which is the slide renderer's own discriminator, and serves the image door for png. Inferring the format from the stored aspect was rejected: 1920x1080 and 1376x768 happen to separate the two decks built so far, and a renderer is free to emit any size. The player branches on the response content type for the same reason, and a png deck renders through an `img` element at box 1128.75x630 where an html deck renders through the iframe at 1120x630.

### 2026-09-10, the player slice — phase 1 complete

**Section 8 passed in both halves, in a real browser.** One voice: four lines in order, the slide advancing at the boundary, `finished` at the end. Two voices: the same material, the same player binary, the same page, **no schema change and no player change** — eight lines alternating `host` and `expert`, each labelled with its speaker. The claim the whole phase was structured around, that two speakers is a field rather than a phase, held at the last place it could have failed. The voices are distinct by measurement, not by label: `af_bella` came back at 186 Hz and `am_adam` at 102 Hz.

**The change source is two answers, not one.** See section 4. Prep state is written by another process and is polled; playback state is written by the serving process and is pushed. The event route was added together with the stream it serves, which is the only condition under which it should ever have been declared.

**Progress covers all six phases.** `steps_total` no longer sits at 0 through ingest and script generation. See section 4.

**Three things a browser found that no probe had.** Each is in section 7:

- The player hard-coded `/api/session` while being served behind a router under a path prefix, so every call it made lost the routing prefix and 404ed. Every route answered when probed, because the probe supplied the prefix itself. The base is now derived from the page's own URL.
- The event stream opened each connection from an "impossible" sentinel playhead, and the first frame a browser received was a real `line.end` for a line that never started.
- `Session.deck_ref` was a `SlideRef` whose `slide` was permanently empty. It is now a two-field `DeckRef`, which is what section 2 described all along.

**The pause seam is built, and it is the one piece of section 5 that belongs in phase 1.** `Playhead.offset_ms` is a free millisecond inside the current line, never rounded to a boundary, and pause records whatever the browser reports. Proven over the public domain: paused at 4137 ms mid-line, resumed there. Retrofitting this would have been a rewrite; building it now was a parameter.

**Three decisions flagged, not buried.** The playhead lives in memory rather than the store, because section 0 fixes the audience at one listener and a playhead is worthless once the page is gone. The player is one HTML document served from a route rather than part of the app bundle, because section 4 asks for "a plain browser page" and the bundle needs a build step. The React app now draws it as one of its pages, `web/src/ui/player.tsx`. And `prep.progress` is a sixth event beyond section 4's five, because none of the five can carry a stage name.

### 2026-09-10, the seams slice

**Section 3's prep job is one row, created by the server and adopted by the child, never two.** The service slice had left two: one the studio made itself, and one the job supervisor actually executed, so a player reading `Session.prep_job_sid` for progress could read the row nobody was running. The supervisor of the time imposed several undocumented rules on how a job could be created and how per-run data reached the child; that plumbing is gone. Today the server writes one row to the Postgres `jobs` table with `jobs.create` and queues the `prep` task for it with `jobs.defer` (`server/opennotebook/jobs/__init__.py`), in the same transaction. The worker runs `prep` in `server/opennotebook/jobs/tasks.py` with that row's `job_id` in its arguments, and the task makes no row of its own.

**Section 4 gains "Where the bytes come from".** The survey's claim that bytes and streams need a socket of their own was read, never executed, and is wrong. Recorded in the spec rather than in a slice report because section 4 builds on it.

**Section 7 gains a row.** The seventh silent-empty instance, and the first found in our own tooling rather than in a service: a shell pipeline returned empty because `strings` was not installed and `grep`'s exit status masked it.

**SSE was not declared until there was a route to serve it.** A declared protocol nothing answers is the same class of claim as `ok: true` on a broken slide. It was not added with a minimal route because there was nothing yet to stream from: a session's state changes inside the prep child process, not the process serving the API, so the server could only learn of a change by polling the store. Choosing the interval, the per-client fan-out and whether to introduce an in-process bus is section 4's design, and emitting anything at all would mean naming events, which section 4 owns. Section 4 adds the declaration back together with the route.

**One thing the wired pipeline found immediately.** Nothing created the retrieval workspace, and the refusal arrived at step 5 of section 1, *after* the first store had been written, so ingest's rollback had to undo a write that need never have happened. In `opennotebook_memory` a workspace is only a namespace and nothing has to be created before it is written.

**Two decisions flagged, not buried.** The retrieval workspace is one shared `opennotebook` rather than one per session, because collections are already namespaced by a sid the spec requires to be unique per workspace; a per-session workspace becomes right the moment two users share an instance. And progress reporting still covers only `build_session`'s four phases, so `steps_total` stays 0 through ingest and script generation — section 3 asks progress to be honest about which stage is running, and for the first two stages it is not yet.

### 2026-09-09, the deck and synthesis slice

Section 3 gains three decisions it did not make, each taken here and named rather than buried. Partial failure is not rolled back: a session carries `state`, so an incomplete one can say so, and what it holds costs 20 to 37 seconds of rendering per slide and 4.4 to 5.7 seconds of synthesis per line. Section 1's rollback exists because a half-imported collection has no way to say it is incomplete; a session does. The narrower rule that replaces it is that `Ready` is written once, after validation, and on no other path. Resuming from a `Failed` session is deliberately not built, only made possible.

The render wait is 600 seconds rather than `deck_create`'s own 180. Five slides at the measured worst case of 37 seconds is 185, which the default does not cover, and a deck that comes back `running` is refused rather than stored.

The deck's theme is the caller's decision. The schema says a new collection ships `<collection>_default`; measured, it ships `<last segment of collection_path>_default`, so nothing here fills that name in silently.

Section 7 gains three rows: the directory naming rule, the synthesis content type discriminator a convenience layer drops, and the job row's `steps_total: 0`.

Section 3's render validation was a spec bug, not merely a loose wording. It read as a quality gate and it was written directly beneath the slide that motivated the whole `ok: true` trap: 131,541 characters of well-formed HTML with an overflowing title, an accent rule struck through a word, and the literal placeholder `SUBHEAD` still in it. A non-empty check passes that slide. So the check could not fail on its own motivating case, which is the definition of a check that reads as coverage and provides none.

It is now written as what it is, a liveness check, and quality is stated to rest entirely on the character budgets applied upstream when the script is generated. Those budgets exist as of the script slice, so the claim is now true rather than aspirational.

### 2026-09-09, the script generation slice

Section 3 gains the two numbers it never gave: five slides, matching `deck_create`'s own default so one number does not have two defaults, and 650 characters of narration per slide, derived from Kokoro's measured 3.6 words per second against this section's own 30 second budget.

Section 7 gains four rows. `qa_search` ranks but carries no content, the fifth silent-empty case. `finish_reason` is the truncation discriminator. Concurrent `qa_extract` then `qa_search` on fresh collections in one workspace can fail with a bare `-32000 "vector_search"`, proven by serialising two tests and watching the failure go away. And a naming note in section 1: `QaSearchReq` takes `collections`, its sibling `QaScopeReq` takes `collection`.

Section 2's `speaker_count` is recorded as derived from `speakers.len()` rather than stored, so nothing tries to set it.

### 2026-09-08, the narration store slice

Preamble no longer names a single commit: the spec rests on the phase 0 run plus these amendments. The two changelog sections were folded into this one list.

Section 6's teardown gap gains a requirement rather than a note. `IngestResult` persists the registry SmartID returned by `collection_import`, because it is the only key `collection_delete` accepts and it is returned nowhere else. Adding that field after sections 3 and 4 read `IngestResult` would be a migration.

Section 2 gains a storage decision the spec had never made. See it there.

### 2026-09-08, the ingest slice

**Section 1 had a bug, not a divergence.** Step 6 asserted `qa_list > 0` on a sequence whose five steps never produced a Q&A pair. Probed on 2026-09-08: `collection_import` on a fresh directory, then `qa_list`, returns zero pairs. The assertion could never have passed as written. This was an error in the spec rather than reality diverging from it, and it is the one correction here that was not discoverable by reading a schema. Section 1 gains **step 5b, `qa_extract`**, between `index_add` and the proof.

**Section 0 gains a Q&A dimensions row.** Adding step 5b forced a choice the spec had never made: which of the retrieval service's seventeen dimensions a session extracts on. Fixed set for phase 1, `architecture`, `technology`, `product`, `business`, with the reasoning, the cost, what it forecloses and two rejected alternatives written out under section 0. The parameter is required with no default, so the studio passes the set and it is not buried in the ingest function.

**Section 1 gains the naming rule.** `collection_import` accepts a `name` and ignores it, naming the collection after the imported directory's basename instead. Verified: importing `~/hct/probe` as `probe_qa_gap` produced a collection named `probe`. The session directory name is therefore the collection name, which is what stops the two writes disagreeing, and it turns the session sid into a uniqueness guarantee the spec relied on without stating.

**Section 7 gains two rows on rollback.** Converting every file before the first write covers only pre-write failures, which the draft treated as sufficient. It is not: with the scan refusal disabled, an image-only PDF reached `index_add` and left a collection with 1 indexed document and 0 Q&A pairs, exactly the half-populated state the rule forbids. Any failure after the first write now undoes both sides. The second row is a behaviour change rather than a record: **a rollback that fails must be reported**, carried as context on the original error, because a silent rollback failure recreates the very state the rule exists to prevent with nothing left to notice it.

**Section 3's prep table gains `qa_extract`.** Measured 8.6 s for two small fixtures and 23.8 s for three files, both at two dimensions, so roughly 4 s per file-dimension. At four dimensions and ten documents that is 2 to 3 minutes, which belongs in the prep budget beside `deck_create` and synthesis rather than arriving as a surprise.

**Section 6 gains a teardown gap.** Successful ingests leave a collection in both stores and a directory on disk, and nothing in the spec deletes either. Named, not solved.

**Section 1 gains one line on wire shapes.** Three param forms, a mechanical rule, and the point worth keeping: the generated SDK cannot get it wrong, so it is a raw-JSON-RPC hazard rather than something the studio code can hit.

### 2026-09-08, after the draft was grilled

**Three factual slips against the phase 0 run.**

The 20-slide `deck_create` total was "5 to 10 minutes" in both section 0 and section 3. At the measured 20 to 37 s per slide it is 400 to 740 s, so **7 to 12 minutes**. The draft contradicted its own measured rate.

Section 6 said `qa_extract` "produced 14 pairs". The first extraction produced **6 pairs from 3 files in 23.8 s**. `qa_list` reached 14 only after a second extraction pass over different dimensions. The verified claim is 6.

Section 1 gave an external file store a role holding the durable copy of the originals. **That store was never called in the phase 0 run**; its surface is schema-read only. It has moved to the section 6 exclusion list with the reason stated, and the rehydrate sentence has been kept.

**Two dependencies the draft needed and did not name.**

Section 3 steps 2 and 3 require LLM calls while section 0 excluded the AI broker service, and nothing said what makes them. Looked up: the slide renderer did not use the broker either. It linked an LLM client library directly and read `OPENROUTER_API_KEY` from the secret store; the voice service did the same. Now a locked row in section 0. (Today that client is `opennotebook_ai`, and the key comes from the environment or `settings.toml`.)

Section 2's `cues[]` said "derived per render" with no producer in the locked dependency set. The speech server's `stt_transcribe` returns `{text, language}` and no timestamps. The producer would be the voice service's `transcribe_cues`, which was not in the dependency list.

**One test run to settle it, and what it changed.**

The voice service was started and `transcribe_cues` was called on a 23.22 s Kokoro clip. It works and is lossless: 84 of 84 words, 0.00 percent word error rate, full duration coverage, 6.4 s latency, which is 0.28x realtime.

It returned **two cues of exactly 11.61 s**, splitting mid-sentence between "a start." and "offset,". That is a fixed halving, not speech boundaries, because synthesised speech has no pauses for VAD to find.

**The ingest assertion became a round trip.**

The draft asserted `index_count > 0` after ingest. That was queried again on 2026-09-08 and replaced, though not for the reason first proposed. `index_add` with the embedder down is not a silent failure: re-running it returned a JSON-RPC error naming the failed dependency and carrying start hints, wrote nothing, and left `index_count` at 0. `search` errored the same way. Nothing was written and nothing pretended otherwise.

The count is still the wrong assertion, for a structural reason. `search` embeds the query at read time, so the embedder is a live dependency of the read path, while a count only observes the write path that has already finished with it. Stopping and restarting the embedding service produced exactly that state: the retrieval service went on reporting the embedder Inactive for tens of seconds after it was back, and during that window `index_count` returned 3 and `qa_list` returned 14 while `search` returned an error. Both of the draft's assertions passed while retrieval was dead. A round trip on a known phrase is the only form that exercises the write, the read and the embedder together.

So `cues[]` is now empty in phase 1 by decision, the voice service is not a phase 1 dependency, and the spec deliberately does not name a future producer. Section 2 records that making the sentence the synthesis unit is the likely eventual answer, and that it is not being decided here. Section 3's audio estimate now carries its condition: about 2 minutes with cues empty, about 5 minutes if they are ever populated eagerly. Section 7 gained a row saying the granularity is a property of the input.
