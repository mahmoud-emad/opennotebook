# OpenNotebook, phase 2 specification

Push to talk. The listener holds a key, asks a spoken question, and the session stops, answers in one of its own voices, and goes back to where it was.

## How to read this one

> **Written against an earlier service stack.** This spec dates from September 2026, when speech ran on a local Kokoro TTS and Whisper STT server, model calls went through an in-process LLM client with the key held by a job supervisor, the turn log was to go to a key-value store, and an external conversation agent service was a candidate owner for the turn. The decisions and the measurements stand. Today speech goes through `opennotebook_speech` to any OpenAI-compatible speech server (a local Speaches at `http://localhost:8000/v1` by default), model calls through `opennotebook_ai`, and storage is SQLite in the data directory. Below, a component of that time is named by its role. The phase 0 verification record and the stack survey this spec cites were not carried into this repository.

[The phase 1 spec](phase1-spec.md) could say "verified" almost everywhere, because almost everything it described already existed somewhere in the stack and the work was composing it. This phase is different. The turn state machine is the one piece of this project with no prior art in the survey, so most of what follows says **decided** and gives the reasoning, and a decision with reasoning is a weaker thing than a call that returned. Two labels are used and they are not interchangeable:

- **Measured.** A number produced on this box, today, by a call that ran. Every one of them is reproducible from the commands in [section 2](#2-the-latency-floor-measured).
- **Decided.** A judgement. The alternatives considered are named, and the reason for the pick is given, so that reopening it is cheap and so that a future reader can tell what would change the answer.

Nothing here is labelled "read". Where a claim rests on a schema nobody on this project has executed, it says so in those words.

## 0. Decisions locked

| Decision | Choice | Basis |
|---|---|---|
| Where the turn state machine lives | In the studio. The external agent service is not a phase 2 dependency. | Decided. The thing being interrupted is narration only the studio can see, and the studio already owns the playhead. See [section 3](#3-the-turn-state-machine). |
| The context window | The current slide's narration and the previous slide's, plus the heard prefix of the interrupted line. Not a count of lines. | Decided, sized by measurement. Two slides is 249 prompt tokens on a real pair. See [section 4](#4-the-context-window). |
| What resume means | The interrupted line replays from its start. The offset is kept, and it is context, not a seek target. | Decided against phase 1's own expectation. `cues[]` is empty and cue granularity is 11.6 s, so no millisecond maps to a word. See [section 5](#5-interruption-cancellation-and-resume). |
| Interruption mid-synthesis | A session has one active turn. A second press cancels the first. Never queue. The synthesiser keeps running regardless. | Decided. `tts_synthesize` is one blocking call with no stream and no cancel, verified in the schema the studio already calls. |
| Does the playhead outlive the page | The playhead outlives the page and not the process, unchanged from phase 1. The turn log outlives both. | Decided. A clock's state is cheap to re-find; a person's question is not. See [section 6](#6-what-is-durable). |
| Whose voice answers | The speaker whose line was interrupted. Before the first line, `speakers[0]`. | Decided. Phase 1 §5 named this seam and `line.start` already carries `speaker_id`. See [section 7](#7-whose-voice-answers). |
| Answer length | One sentence, 200 characters, enforced on the way out by `budget.fit` | Measured. A 309 character answer is 23.3 s of audio and 26 s of synthesis on this box. Length is the only lever with a large measured effect on time to first sound. |
| Answer generation | The in-process LLM client (today `opennotebook_ai`) at `openai/gpt-5.4`, the same client and model `opennotebook_script` already used | Measured. 1.5 to 2.2 s per answer, about $0.0016. Adding a second LLM path for one call buys nothing. |
| Speech to text | The offline path, `stt_transcribe` on the speech server | Measured, and the realtime path is blocked. See [section 1](#the-two-claims-that-did-not-hold). |
| Did the listener say anything | `opennotebook_vad::analyze`, on the uploaded clip, before the answer model is called. Never the emptiness of the transcript. | Built 2026-09-17. The studio's own detector rather than `/v1/audio/vad`, because the same crate has to run in the browser and sherpa-onnx does not cross to wasm32. See the amendment. |
| C and C++ dependencies in the audio path | Acceptable | Decided 2026-09-17. The stack already assumed this: Silero runs through `sherpa-onnx`, which is a C++ library with Rust bindings. |
| Voice enrollment, for separating one speaker from another | Declined | Decided 2026-09-17. It puts a recording step in front of a listener's first question and creates biometric data to store, to solve a case the on-screen transcript already makes visible. |

## 1. What phase 1 left ready

Phase 1 §5 listed four seams. Three were checked against the repo at `c2d5e7b` rather than taken from the phase 1 spec's own word, because the point of this section is that the spec can be wrong.

**A pause that survives mid-line. Confirmed.** The Rust server's playback module declared `offset_ms: i64` with the comment "Arbitrary: §5 needs to resume mid-line", and a test asserted 3,477 ms survives a round trip with the state and line id intact. The new server keeps both: `Playback.offset_ms` in `server/opennotebook/db/models.py`, and `test_an_offset_is_kept_exactly_and_per_output` in `server/tests/test_media.py`. The schema agrees: `Playhead.offset_ms` is documented as "arbitrary, inside line_id". Phase 2 uses this, though not for what phase 1 expected, and [section 5](#5-interruption-cancellation-and-resume) is about that difference.

**Which voice held the floor. Confirmed.** `NarrationLine.speaker_id` is `SpeakerId`, not an `Option`, in both the Rust model and the oschema, where it is annotated "REQUIRED, always, even in a one-speaker session". `StudioEvent::LineStart` carries it. `Session::dangling_speaker_ids` exists to prove the id resolves to a `Speaker`, which is what makes it safe to look up a `voice_id` from it.

**The last N lines window. Confirmed as data, absent as a query.** `Session::slides_in_order()` and `SessionSlide::lines_in_order()` both sort on the explicit `ordinal` rather than trusting vector position, so ordering is answerable. There is no function that assembles a window, which is exactly what phase 1 said: "just make them queryable as a unit". That is one function over data that is already correct, not a schema change.

**Nothing about the state machine. Confirmed, and it is the whole of this spec.**

Two more things phase 1 left that its §5 did not list, and phase 2 depends on both. `session.state` is a bare `str` on the wire carrying values from two different Rust enums, `SessionState` and `PlayState`, so adding a state costs a new string and not a schema change. And phase 1 §4 established that bytes ride `/api/` beside the JSON-RPC routes, which is why the question audio a browser uploads needs no new endpoint of its own kind.

### The two claims that did not hold

**The latency numbers from the phase 0 verification run are from a different machine.** That run recorded synthesis at 0.20x to 0.24x realtime and transcription at 0.060x to 0.100x, on `mahmoud-ashraf-devbox`. Everything since has run on `dev3omda`, a Xeon E5-2620 at 2.00 GHz with 16 threads and 15 GB. Re-measured here today, on a box with a load average of 1.29 and the voice provider idling at 0.002 cores:

| Operation | phase 0, `mahmoud-ashraf-devbox` | Measured today, `dev3omda` | Factor |
|---|---|---|---|
| TTS, local Kokoro | 0.20x to 0.24x realtime | 1.09x to 1.22x realtime | about 5x slower |
| STT, offline `stt_transcribe` | 0.060x to 0.100x realtime | 0.42x realtime | about 4x slower |

The old numbers are not wrong and they are not stale in the ordinary sense. They describe hardware this project no longer runs on. The consistent 4x to 5x across two unrelated models on two different engines is what makes "different box" the explanation rather than "regression": nothing was rebuilt between them.

This matters more to phase 2 than it did to phase 1, because phase 1 spent its synthesis in a background job where a factor of five turns three minutes into fifteen and nobody is watching. Phase 2 spends it in front of a person who has just asked a question.

**Transcription accuracy is 6.03%, not 0.00%, and the errors are the ones that hurt.** Phase 0 recorded 84 of 84 words correct on one clip. Run over eight of this project's own narration lines, 39.5 s of audio in total:

```
  s0l0:  3328ms ->  1523ms  WER   0.0%
  s0l1:  8871ms ->  3428ms  WER   0.0%
  s0l2:  6522ms ->  2647ms  WER   5.0%  'It also supports barge in. If someone interrupts, the runn'
  s0l3:  4364ms ->  1912ms  WER   0.0%
  s1l0:  3949ms ->  1783ms  WER   0.0%
  s1l1:  5023ms ->  2099ms  WER  18.8%  'If bajan happens, the running text-to-speech stream is can'
  s1l2:  3252ms ->  1502ms  WER   0.0%
  s1l3:  4232ms ->  1804ms  WER  20.0%  'Examples include off Bella and Amatom, which can be used f'

  AGGREGATE: 7 edits / 116 words = 6.03% WER
```

This is not a regression either: phase 0 transcribed one clip of general prose, and these are the studio's own sentences. Every single error is domain vocabulary. `barge-in` became `bajan`. `af_bella and am_adam` became `off Bella and Amatom`. Five of the eight lines are perfect and the three failures are all proper nouns and jargon.

The consequence is a design requirement and it is in [section 8](#8-failure-modes): a listener whose question was misheard gets a confident, fluent answer to a question they did not ask, and nothing on screen tells them why.

### The external agent service, checked rather than assumed

The stack had an external conversation agent service, with a message API, mid-turn steering, cancellation and an event stream. The survey had marked those methods "verified" from their schema, without calling any of them. Checked on 2026-09-17, the schema claims were correct, and two further facts bear on [section 3](#3-the-turn-state-machine): the service was not installed on this box, and a voice turn state machine (session start, turn commit, playback report, turn cancel, session end) had landed in it on 2026-09-08 and 2026-09-09, the days the survey ran and the day after. So "no implementation of a turn state machine anywhere" stopped being true within about 36 hours of being written.

That does not make it the right thing to depend on, and [section 3](#3-the-turn-state-machine) says why. It does make it the right thing to copy.

## 2. The latency floor, measured

Everything in this section ran today on `dev3omda` against the local Kokoro and Whisper speech server, and against `openai/gpt-5.4` through OpenRouter. Each figure is the median of three or more runs and the spread was under 5%.

**Transcription.** 0.42x realtime, from the eight-line run above. An 8.9 s question costs 3.4 s.

**Answer generation.** A two-slide context plus the question is 249 prompt tokens. Three runs returned in 2,147 ms, 1,478 ms and 1,621 ms, producing 63 to 73 completion tokens, at $0.0016 to $0.0017 each. `finish_reason` was `stop` every time.

**Synthesis.** Between 1.09x and 1.22x realtime across five different texts from 15 s to 45 s of output, so the factor is flat and there is no fixed cost worth modelling. It holds 3.84 cores for the duration, against 0.002 cores idle.

| Text | Characters | Audio | Wall | Factor |
|---|---|---|---|---|
| A real two sentence answer | 309 | 23.34 s | 26,180 ms | 1.12x |
| Its first sentence alone | 173 | 9.87 s | 11,249 ms | 1.14x |
| Its second sentence alone | 136 | 13.47 s | 15,309 ms | 1.14x |
| A three-sentence paragraph | 218 | 15.03 s | 18,213 ms | 1.21x |
| The same, tripled | 654 | 45.55 s | 53,583 ms | 1.18x |

**The floor, end to end.** For the measured question and the measured answer:

| Leg | Measured | Share |
|---|---|---|
| Transcribe a 8.9 s question | 3.4 s | 11% |
| Generate the answer | 1.6 s | 5% |
| Synthesise a 309 character answer | 26.2 s | 84% |
| **Key release to first sound** | **about 31 s** | |

**That is not a conversation, and the spec says so rather than hiding it in a table.** It is also the reason four of the six decisions below came out the way they did. Five sixths of the wait is the synthesiser, so every lever that matters is a lever on how many characters get synthesised.

Two levers were measured and only one survived.

*Shorten the answer.* Speech runs at 10 to 17 characters per second, and the spread is numerals: "1920 by 1080" is 12 characters and about two seconds. A 200 character cap is 12 to 20 s of audio and 13 to 22 s of synthesis. It is crude and it works, and it costs a line in a prompt plus the `budget.fit` call phase 1 already wrote.

*Synthesise per sentence and start on the first.* Measured both ways on the same answer. Whole: first sound at 26.2 s, no gaps. Per sentence: first sound at 11.2 s, then sentence one plays for 9.9 s while sentence two takes 15.3 s to render, leaving a **5.4 s silence in the middle of the answer**. At 1.1x realtime there is no margin to hide a gap in, which is the same arithmetic phase 0 used to reject streaming synthesis when the factor was 0.2x and the margin was fourfold.

**Decided: synthesise the whole answer, and put the text on screen while it renders.** A wait before an answer reads as thinking. A five second hole inside one reads as a fault. The transcript lands at about 3.5 s and the answer text at about 5 s, so the listener has something to read for the 26 s that follows, and [section 13](#13-open-questions-for-the-owner) asks whether they should simply be given a button rather than the audio.

## 3. The turn state machine

### Where it lives

**Decided: in the studio.** Three alternatives were considered.

*Delegate to the agent service's voice session.* It is the closest existing thing, it is tested, and it solves problems this spec would otherwise solve badly. It is still wrong here. Its turn owns a conversation; this turn owns a **playback position**. `voice_turn_commit` has nowhere to put a slide ordinal, a line id or an offset, and `voice_playback_report` reports the audible prefix of a reply rather than of a narration line that the agent service has never heard of. Putting the machine there splits one session's truth across two processes, and then no single place can answer "where is this session and who has the floor", which is the exact property phase 1 §4 built the studio-side playhead to get.

*Split it: shrimp owns the turn, the studio owns the position.* This is the worst of the three. Every question becomes two state machines that can disagree, and the failure mode is a session that is paused according to one and playing according to the other.

*The studio owns it, and the agent service is not a dependency.* Picked. The studio already holds the playhead, already holds the narration text, already holds the speakers, and already serves the browser the events. The turn machine is three fields next to state it owns.

Two costs, named so they are not discovered later. The studio gives up the agent service's tool calling, its projects, and its durable branching conversation history. And phase 2 answers from the session's own grounding store through the LLM client rather than from an agent, so it answers questions about the material and nothing else, which [section 9](#9-non-goals-explicitly-out) makes explicit.

The practical argument sits underneath both: the agent service is not on this box, has never been called by anything in this project, and moved a whole feature under the survey in 36 hours. Bringing it in is an install plus a phase 0 style verification run against a surface that is still moving, and phase 2 does not need what it would buy.

### What to copy from it anyway

Reading the agent service's implementation is cheap and it has already made three mistakes worth not repeating. This is composition of a design rather than of a service, and it is the strongest thing in this spec that is not a measurement.

**One owner per session, and `busy` is a typed answer rather than an error.** `VoiceTurnCommitResult` carries `busy: bool` described as "typed retry signal; true while an earlier accepted turn still owns the session". A caller that gets a refusal has to parse a message; a caller that gets a flag has to read a field.

**Identities are checked, not assumed.** `VoiceTurnCancelResult` returns `matched`, `cancelled` and `released` separately, with `matched: false` meaning "these identities are stale". A cancel that silently succeeded against the wrong turn is a bug nobody finds.

**The confirmed audible prefix is never a speculative estimate, and it is server-authored.** `VoicePlaybackReportParams.played_text` is documented "full confirmed audible prefix, never a speculative estimate", and its `prior_interruption_context` turns it into a fixed block of context that the browser cannot write:

```
[Voice playback continuity — server-authored]
The previous assistant reply was interrupted. The confirmed audible prefix was: "..." Treat the rest of
that reply as unheard: do not assume the user received it or rely on it without explaining it again.
[/Voice playback continuity]
```

The studio's version of that sentence is about narration rather than a reply, and it is built from `offset_ms` and the line's text rather than from a browser-supplied string, but it is the same idea and the same reason.

### The states

Phase 1's `session.state` is a wire string covering two enums. Phase 2 adds three values and no new event:

```
narrating    the playhead is running, phase 1's playing
listening    the key is down, audio is being captured, the playhead is paused at an exact offset
thinking     transcribing, then generating, then synthesising
answering    the answer's audio is playing
```

`paused` stays what it is: a listener who pressed pause. A turn passes `narrating -> listening -> thinking -> answering -> narrating`, and every edge out of `thinking` or `answering` other than the normal one is a cancel.

**Decided: one turn at a time, per session, and a turn is identified.** The studio holds `active_turn: Option<Turn>` beside the playhead, and a `Turn` carries a `turn_id`, the playhead as it was at the moment of interruption, the transcript once there is one, and the answer text once there is one. Nothing about this needs a new socket, a new domain or a new event type.

## 4. The context window

**Decided: the unit is the slide, not a count of lines.**

*N lines* was phase 1's phrasing and it is arbitrary at exactly the moment it matters. A listener who interrupts on the first line of slide 4 gets, under N=8, seven lines of slide 3 and one of the slide they are looking at. Under N=2 they get one line of context about a slide they have barely heard. The count is doing the work of a boundary that already exists in the data.

*The current slide plus the previous one* is picked, because slides are the unit the material was already organised into by [section 3 of phase 1](phase1-spec.md#3-session-build-a-job-not-a-request) and the unit the listener is looking at. The previous slide is included because a question asked early on a slide is usually about what just finished.

It is bounded by construction, which is why this can be stated as a size rather than hoped about. `budget.SLIDE_NARRATION` is 650 characters and `budget.LINE` is 320, both enforced on the way out of script generation. So the window is at most 650 plus 650 plus the heard prefix of the current line, under 1,620 characters. Measured on a real pair of slides plus a question: **249 prompt tokens**.

Alongside it, three things:

- **The slide's own text**, since the listener is looking at it and "what does that box mean" is about the slide and not the narration.
- **Retrieval from the session's grounding store**, using `qa_search` for ranking joined to `qa_list` for content on the id. Never `qa_search`'s own `text`: phase 1 §7 records that it is optional and came back absent on a live call, and `unwrap_or_default()` there grounds a model on empty strings while the output still reads fine. Fifth instance of the silent-empty pattern, and it is in the read path phase 2 lives on.
- **The continuity sentence**, when the previous turn was cancelled before its answer finished.

**Decided: turns accumulate, and only the previous one is in context.** A listener who asks "what about the other one" after an answer means the answer, so a window with no turn history in it cannot serve the second question in a pair. A full conversation history is a different feature and it belongs to something with the agent service's shape. One turn back is the cheapest thing that makes a follow-up work, it costs at most another 200 characters of answer plus a transcript, and going further is a constant.

## 5. Interruption, cancellation and resume

### What resume means

**Decided: the interrupted line replays from its start. The offset is kept and it is context, not a seek target.**

This goes against phase 1 §5's own expectation, which said "an arbitrary offset in milliseconds is all phase 2 needs to resume". That is true of the **state** and phase 2 does need it. It turned out not to be true of the **audio**.

A line is one WAV synthesised as a unit. There are no marks inside it: `cues[]` is empty by a phase 1 §0 decision, and phase 1 §7 records that cue granularity is 11.6 s and is a property of TTS input having no pauses to segment on, so it "will not improve with a better cue method". Nothing in this stack can turn 3,477 ms into a word boundary. A seek to the offset starts mid-syllable, and it does it on every single resume rather than occasionally.

Replaying costs at most one line. `budget.LINE` caps a line at 320 characters, which at the 10 to 17 characters per second measured above is 19 to 32 seconds. That is a real cost and it is smaller than it looks next to the 31 second answer that preceded it, and the bytes are already on disk so it costs no synthesis at all.

The offset stays load-bearing in two places. It is what `playback_pause` records so the browser and the studio agree where the session is, and it is what the continuity sentence is built from: the studio knows the line's text and the fraction of its duration that was heard, so it can say which part the listener got.

**A judgement inside a judgement.** Resuming at the start of the interrupted line, rather than the start of the sentence containing the offset, is the same decision made once at the only granularity available. Sentence-level resume is the open question phase 1 §2 already recorded, and it needs marks in the audio that nothing produces.

### Cancellation

**Decided: a second press cancels the active turn. Nothing is ever queued.**

A queued question is answered on average 31 seconds after it was asked, about a slide the listener has moved past. Answering it is worse than dropping it.

Cancelling is cheap in three of the four legs and free in none:

- **During capture.** Discard the buffer. Nothing has cost anything.
- **During transcription.** The call is in flight for a few seconds. Abandon the result.
- **During generation.** Same, and it has already been paid for. About $0.0016.
- **During synthesis.** The result is abandoned and **the work is not.** `tts_synthesize` is one blocking JSON-RPC call returning a whole WAV, with no stream and no cancel method anywhere on the surface the studio calls, and phase 1 §0 locked out the AI broker service, which was the one path in the stack with a mid-utterance cancel. So a turn cancelled at second 5 of a 26 second synthesis leaves the speech server holding 3.84 cores for 21 more seconds, and a listener who presses twice in frustration is now contending with themselves for the same 16 cores.

That last one is a real limit and there is no way around it in phase 2 without taking on the AI broker. It is named in [section 10](#10-known-traps-as-assertions) so nobody looks for a cancel that is not there.

**Decided: the playhead does not move during a turn, at all.** It is pinned at the moment of interruption from `listening` until the resume. A cancelled turn therefore costs the listener nothing but time: the deck is exactly where they left it, and the resume is the same resume a completed turn gets.

### The interruption point

**Decided: hold to talk.** The key going down is the start and the key coming up is the commit. The alternatives are a press to start and a second press to stop, which needs an explicit end and gives a stuck-microphone failure with no obvious exit, or silence detection through VAD, which is a second call and a tuning problem on top of a feature that is already 31 seconds slow.

The cost of hold to talk is that a long question means holding a key a long time, and browsers repeat keydown. The handler takes the first and ignores repeats.

## 6. What is durable

**Decided: the playhead stays in memory. The turn log goes to the store.**

Phase 1 flagged this as an open decision rather than making it, and the flag names the migration in the Rust server's playback module: "If phase 2 wants a session a user can walk away from and come back to, this map becomes a stored playhead per sid and nothing else changes". Phase 2 declines it, for a reason the flag does not contain.

The playhead already outlives the page. It lives in a process-wide map keyed by sid, so a reload recovers it, and phase 1 §0 fixes the audience at one listener and one browser page. What it does not outlive is a service restart, and that loses a position which costs a listener one line of scrubbing to re-find. Against that, the browser reports its clock through `playback_progress` continuously while playing, so persisting the playhead puts a store write on the wire every couple of seconds for the length of every session.

The turn log is the opposite trade on both axes. A question a person asked, transcribed and answered is not re-findable by scrubbing, and turns happen on a human timescale, so it is a handful of writes per session rather than hundreds. One document per session, keyed by the sid, appended when a turn reaches a terminal state, carrying the turn id, the playhead at interruption, the transcript, the answer and how the turn ended.

**The line being drawn is: state a person produced is durable, state a clock produced is not.** If that turns out to be wrong it is wrong in the direction phase 1 already costed, and the migration is the one that module described. The new server has since made it: the playhead is a row of the `playback` table (`Playback` in `server/opennotebook/db/models.py`).

The turn log is also what makes [section 4](#4-the-context-window)'s one-turn-back window survive a reload, and it is the only record that a question was ever misheard, which [section 8](#8-failure-modes) needs.

## 7. Whose voice answers

**Decided: the speaker whose line was interrupted.**

Phase 1 §5 named this seam and built it: `speaker_id` is required on every `NarrationLine` and rides on every `line.start`, and `Session::dangling_speaker_ids` proves the id resolves to a `Speaker` carrying a `voice_id`. The answer is synthesised with that `voice_id`. There is no new field and no new configuration anywhere.

*A third assistant voice* was considered and rejected. It needs a third `Speaker` in every session, a voice id somebody has to pick, and in the one-speaker case it doubles the cast in order to answer one question. It also makes the answer sound like it came from outside the material, when the whole point is that it came from inside it.

Two edges decided rather than discovered:

**Before the first line starts.** `Playhead.line_id` is `""` until the first line begins, asserted in `a_fresh_session_is_idle_at_the_start`, so there is no speaker holding the floor. Fall back to `speakers[0]`, which is session order and not a new field.

**A two-speaker session where the question is about what the other one said.** Phase 2 does not route by content. The voice that was speaking answers, always, and if the answer needs to refer to the other speaker it does so in words. Routing by content is a model call to pick a voice, on top of a feature that is already slow, to solve a problem nobody has reported.

## 8. Failure modes

Each of these is a way for the feature to produce a fluent, confident, wrong result. The measured ones say what was measured.

**The question was misheard.** Measured at 6.03% WER on this project's own sentences, with every error on domain vocabulary. The listener hears a good answer to a question they did not ask, and there is no signal anywhere that this happened.

*Decided: the transcript goes on screen before the answer does, always, as text and not as a confirmation step.* It is free, it arrives about 22 seconds before the audio does, and it turns an invisible failure into an obvious one. This is the one place a measurement forced a user-visible requirement into this spec. Phase 2 does not ask the listener to confirm the transcript, because a confirmation step on top of a 31 second answer is worse than a wrong answer they can see and redo.

**The listener said nothing, or said something the recogniser could not resolve.** Measured: `stt_transcribe` returns `{"text":""}`, byte for byte identical, for pure silence, for random noise, and for speech it cannot resolve into words. It is not an error and there is no confidence, no segment count and no flag on the result.

*Decided: gate on VAD, never on the transcript.* `/v1/audio/vad` returns `speech_ms` and `segment_count`, and it separates the two cases cleanly: 5 s of silence gives `segment_count: 0, speech_ms: 0`, and real speech gives `segment_count: 1, speech_ms: 8896`. It costs 239 ms on a 9 s clip on `dev3omda` against the 3.4 s the transcription costs. `speech_ms == 0` resumes silently, because a listener who pressed the key by accident should not be told off. `speech_ms > 0` with an empty transcript says so, because "I heard you and could not make it out" is a different thing and the listener should say it again.

**Built 2026-09-17, and not with `/v1/audio/vad`.** What follows describes the state at `aae4fb0`, kept because the reasoning still holds and the replacement is in the amendment below. Nothing in `opennotebook` called `/v1/audio/vad`. Searching every Rust and HTML file of the old server for `vad` or `speech_ms` returns one doc comment in its session model about TTS output, and no call site. Its ask handler sends the WAV to the answer model and calls `stt_transcribe` alongside it for the `heard` transcript. There is no discriminator anywhere in the built path, so an empty transcript still has three meanings and one representation, which is the thing this decision existed to prevent.

*What ships instead, and it does a different job.* The turn is ended in the browser by an energy threshold in the Rust server's `player.html`: `SILENCE_PEAK = 0.06`, read from an `AnalyserNode` with `fftSize = 512`, polled every 60 ms, with `HANG_MS = 2000` of quiet ending the turn. At 24 kHz that window is 21 ms, so the gate examines 21 ms out of every 60 ms and is blind to the other 39. `getByteTimeDomainData` quantises to 1/128, so the threshold is about 7.7 steps wide.

**A door slam above 0.06 counts as speech.** It starts the turn, the 2 s hang runs to completion, and a clip containing no speech is uploaded and answered. Nothing downstream disagrees, because nothing downstream is asking.

*The peak gate is not a replacement, and recording it as one would be wrong.* The VAD decision above is about what to tell the listener once the turn is over. The peak gate is about when the turn ends. Neither covers the other. The "listener said nothing" case is closed by accident, since a gate that never fires sends no turn, and the "said something I could not resolve" case is not closed at all.

*Why it went this way.* The turn design changed after this section was written, and the 2026-09-14 amendment records it: hold to talk gave an explicit end, and ending on silence needs a detector running live in the page, before anything is uploaded. A one-shot VAD on the server cannot end a turn that has not been sent yet. The speech server's VAD had `MIN_SILENCE_SECS` at 3.0 s, longer than the 2 s hang, so it would not see the pause that ended the turn even if it were called, and `POST /v1/audio/vad` exposes no parameter to change it. Closing this properly means a voice detector in the browser, which is a piece of work and not a line of wiring.

**The audio was malformed.** Measured: this one is loud. `{"error":{"message":"Transcription failed: WAV parse failed: Ill-formed WAVE file: no RIFF tag found"}}` for garbage bytes, and a similar message for an empty file. Surface it as a failure rather than as silence.

**The answer was truncated.** Phase 1 §7's `finish_reason` trap applies to the answer exactly as it applies to the script: a model that ran out of room returns prose that reads whole. Measured `stop` on three of three runs here, which proves the field is populated and not that it is always `stop`. Check it; never speak a completion without knowing why the model stopped.

**The synthesised answer is not a WAV.** Phase 1 §7's `content_type` trap. `response_format` accepts `"pcm"`, which has no header, and the SDK wrapper discards `content_type`. Drive `tts_synthesize` through the generated client and read `content_type` before decoding, the same way narration does: `synthesise_line` in `server/opennotebook/build/narrate.py` reads every clip's header with `wav.duration_of` before keeping it.

**A prep job is running on the same box.** Synthesis holds 3.84 cores, and a prep job synthesises one line after another for minutes. A question asked while another session is preparing contends for the same 16 cores, and the 31 second floor is a floor measured on an idle box. Phase 2 does not schedule around this. It is named so that a slow answer during a prep is recognised rather than investigated.

**The page closed mid-turn.** The turn log records a terminal state and the playhead stays where it was pinned. A reload lands on a paused session at the interruption point, which is correct: the question was asked and not answered, and pretending otherwise loses it.

**The store is unavailable when the turn ends.** The turn already happened and the listener already heard it. Log the write failure and do not fail the turn. The cost is a missing entry in the one-turn-back window, which degrades the next follow-up question and nothing else.

## 9. Non-goals, explicitly out

**Live barge in.** Not deferred, blocked. The realtime WebSocket emits no transcription event at all, across every configuration tried, and it leaks connections that pin 4.11 cores until the service is restarted. Both were reported upstream. Phase 2 carries no migration path towards it and shapes nothing around it. Push to talk is where this stops, not where it starts.

**Streaming synthesis.** Rejected on the measurement in [section 2](#2-the-latency-floor-measured), not on principle: at 1.1x realtime there is no margin, and it buys 15 seconds of time to first sound at the price of a 5.4 second hole in the middle of the answer.

**The external agent service, the AI broker, LiveKit, an external file store.** The first for the reasons in [section 3](#3-the-turn-state-machine), the rest carried forward from phase 1 §6 unchanged.

**A conversation.** One turn of history, not a thread. No branching, no titles, no search over past questions, no separate conversation store. That is the agent service's shape and it is a good shape, and it is not this.

**Questions about anything but the session.** The answer is grounded in the session's own collection and slides. No web search, no tool calls, no reaching into another session.

**Resuming at a sentence boundary.** [Section 5](#5-interruption-cancellation-and-resume) covers why. It needs marks in the audio that nothing in this stack produces.

**Populating `cues[]`.** Unchanged from phase 1 §6, and [section 5](#5-interruption-cancellation-and-resume) is the reason it stays unchanged.

**Any change to the slide renderer, and any change to the phase 1 schema.** Every field phase 2 needs already exists. The three new `session.state` values are strings in a field that is already a string.

## 10. Known traps, as assertions

Each gets a test that fails loudly, in the phase 1 §7 style. The first three are new; the rest are phase 1's, restated where phase 2 re-enters them.

| Trap | Assertion |
|---|---|
| `speech_ms` is the provider's discriminator, and the studio never calls it | **The first trap in this table that was specified and then not built.** Section 0 and [section 8](#8-failure-modes) both lock "gate on VAD, never on the transcript", and `opennotebook` calls `/v1/audio/vad` nowhere: one doc comment in the session model, no call site. What ends a turn is `SILENCE_PEAK = 0.06` in `player.html`, an energy threshold polled every 60 ms over a 21 ms window with a 2 s hang, so a door slam counts as speech and a clip with no speech in it is uploaded and answered. The assertion is about the built path, not the provider: assert that whatever decides "did the listener say anything" is actually called, before trusting either section. |
| An empty transcript has three meanings and one representation | **Eighth instance of the silent-empty pattern.** Silence, random noise, and unresolvable speech all return `{"text":""}` from `stt_transcribe`, identically, with no confidence and no segment count. The discriminator exists on a different endpoint: `/v1/audio/vad` returns `speech_ms` and `segment_count`, 0 and 0 for 5 s of silence against 8,896 and 1 for real speech, at 239 ms for a 9 s clip on `dev3omda`. Never branch on the transcript's emptiness. Malformed audio is the one case that is loud, and it errors by name. |
| Characters are not seconds, and numerals are why | The answer budget is enforced in characters because that is what `budget.fit` measures, and the thing that actually matters is synthesis time. Measured between 10 and 17 characters per second of speech across five texts, with numerals at the expensive end: "1920 by 1080" is 12 characters and about 2 seconds. A number-heavy answer blows the time budget while sitting inside the character budget. There is no cheap fix in phase 2; assert on the measured `duration_ms` of the synthesised answer and log when it exceeds the budget the character cap was chosen to buy. |
| A cancelled turn does not stop the synthesiser | `tts_synthesize` is one blocking call with no stream and no cancel. Cancelling during synthesis abandons the result and not the work, and the speech server keeps 3.84 cores for the remainder. Assert that cancel returns promptly and does **not** assert that the provider went idle. |
| Latency numbers belong to a box | Phase 0's numbers are `mahmoud-ashraf-devbox`; everything in this document is `dev3omda`, and the two differ by 4x to 5x on the same builds. Any number in either document is a property of a machine. Re-measure before designing against one. |
| `speech_started` is not a VAD onset event | Carried from phase 1 §5. It is queued only when the recogniser produces text, so it means a word was decoded rather than that a person started speaking. Nothing in phase 2 should read it, and anything written against OpenAI's realtime semantics will be wrong about it. |
| `finish_reason` is the truncation discriminator | Carried from phase 1 §7, and it now applies to a sentence a person is about to hear. |
| The response's content type is the audio discriminator, and a convenience wrapper can drop it | Carried from phase 1 §7. Sixth instance of the silent-empty pattern, and phase 2 calls the same method for the same reason. |
| `qa_search` ranks but does not carry content | Carried from phase 1 §7. Fifth instance, and phase 2's answer path is the read path it breaks. |
| `session.state` is one wire string over two enums | `SessionState` is `preparing`, `ready`, `failed`; `PlayState` is `idle`, `playing`, `paused`, `finished`; phase 2 adds `listening`, `thinking` and `answering`. Adding a value is free and colliding with one is silent. Assert the whole set in one place. |

## 11. Done means

A listener is watching a narrated deck. On slide 4, three seconds into a line, they hold the space bar and ask what letterboxing means. They release.

The deck stops where it was. Within about four seconds they see what was heard, in their own words, on screen. A moment later they see the answer in text. About half a minute after that they hear it spoken, in the same voice that had been narrating, in one sentence. Then slide 4's interrupted line starts again from its beginning, and the session carries on.

They do it again in a two-speaker session and are answered by whichever of the two was talking, with no schema change and no config change.

Then the negative half, which is the part that needs a test rather than a demonstration. They press again while the answer is still being made. The turn is cancelled, nothing is queued, no second answer arrives later, and the deck is still exactly where it was. They ask a question into a muted microphone and the session resumes without comment. They ask a question the recogniser cannot make out and are told so rather than answered.

## 12. Sequence, for building

Not a schedule. An order, so that each step ends somewhere testable.

1. **The turn record and its states.** `Turn` beside the playhead, the three new `session.state` values, the stored turn log, and a test that a turn cannot be started while one is active. No audio anywhere. This is the invented piece and it is worth having alone before anything is attached to it.
2. **Capture and transcription.** The key handler, the upload on the existing `/api/session/` surface, the VAD gate, the transcript on screen. Ends with a listener able to see their own question, correctly or otherwise, and nothing answering it.
3. **The context window and the answer as text.** One function assembling the window, the LLM call, `finish_reason` checked, the budget enforced on the way out, the answer on screen.
4. **The answer as audio.** Synthesis in the interrupted speaker's voice, `content_type` checked, playback, then resume from the start of the interrupted line.
5. **Cancellation.** Last, because it is only testable once there is something long enough to cancel.

## 13. Open questions for the owner

These are not judgement calls that were ducked. Each one needs something this project does not have.

**1. Which box does phase 2 run on?** Every number here is `dev3omda`, and phase 0's are from hardware four to five times faster. On that hardware the 31 second floor is about 7 seconds, and at 7 seconds two decisions in this spec are worth reopening: the one sentence answer cap, and synthesising the whole answer rather than the first sentence. I cannot pick the box, and the decisions above are the right ones for the box that exists.

**2. Is a spoken answer the product at all, on this hardware?** The listener sees the answer in text at about 5 seconds and hears it at about 31. The obvious alternative is to show the text and offer a button that speaks it, which makes the wait opt-in and costs one control. My recommendation is to ship the text-first version and keep the audio behind the button until question 1 is answered, because the voice is the point of the feature and 26 seconds of silence is not a good way to deliver it. This is a product call, not an engineering one.

**3. Does the external agent service become a dependency later?** It now has a tested voice turn machine, and this spec declines it for reasons that are about ownership of the playhead rather than about quality. If the studio ever wants tool calls, Projects, or a real conversation behind the question, that is the service and this decision should be revisited rather than reimplemented. Doing so costs an install on this box plus a phase 0 style run against a surface nobody here has executed.

**4. Should a listener be able to type a question instead of speaking it?** It skips the 3.4 s transcription and the whole 6.03% WER problem, and it is perhaps twenty lines. It also changes what the feature is, and it makes the case for the spoken path weaker rather than stronger. Out of scope until somebody decides it is wanted.

**5. What happens to a session nobody is watching?** Phase 1 §6 records that nothing deletes anything, and phase 2 adds a turn log to the pile. Teardown was tracked separately and this spec does not solve it (it has since been built: deleting an output or a collection removes its rows and files), but the turn log is a second document per session that whoever picks it up needs to know about.

## Amendments

Newest first. Each says what changed and what forced it, so a correction is not re-derived from scratch.

### 2026-09-17, the studio gets its own detector

Both questions in section 8 now have one answer, and it is code in this repo rather than a provider call. It was first a Rust crate, built for native and for wasm32 from one source. The new stack keeps its `Gate` and thresholds and runs Silero v5 in both places: `web/src/ui/playerGate.ts` in the page, through `@ricky0123/vad-web` and onnxruntime-web, and `server/opennotebook/speech/vad.py` on the server, through onnxruntime, with the same model file.

**Why not `/v1/audio/vad`.** The endpoint decision has to happen where the microphone is, in the page, before anything is uploaded. Silero there means sherpa-onnx, which is C++ and does not cross to wasm32. Running Silero on the server and something else in the browser would be two detectors and two sets of thresholds, which is precisely the drift that made the peak gate and the VAD gate answer different questions in the first place. So one crate answers both, from one `Config`.

**The detector is `earshot` 1.2**, MIT/Apache-2.0, a small neural model with 39,940 bytes of weights compiled in, zero runtime dependencies and nothing to fetch at start up. Rejected: Silero through `tract`, the pure Rust ONNX engine that does reach wasm32, on payload, because tract plus a 643 KB model is megabytes of wasm to load into an audio thread against 88,160 bytes for this; `voice_activity_detector` and `silero-vad-rust`, which read as pure Rust ports and both depend on `ort`; `webrtc-vad`, a C binding last released in 2019. Two things the research had wrong and the source corrected: earshot is not a WebRTC GMM port, that was 0.x, and the published 1.2.2 is behind its own README, which documents a segmenter the release does not ship.

**Measured in a real browser engine**, headless Chromium, the wasm fetched from the server and driven through its C ABI at the browser's native 48 kHz: 3 s of digital silence gives `speech_ms` 0, 3 s of a 0.8 amplitude 440 Hz tone gives `speech_ms` 0, and 9,325 ms of this project's own narration gives 8,864. The tone is the point. It clears the old 0.06 threshold on every poll, and it is not speech.

**Measured on the server**, against the running service: 6 s of digital silence and 6 s of gaussian hum at about 0.09 amplitude both return `event: silent` with `speech_ms: 0`, real narration passes the gate through to the session load, and a malformed body still fails loudly by name as section 8 requires. The hum is the bug this slice exists for: replayed through the shipped gate it trips 100% of polls, so the turn never falls quiet and never sends, which is the listener waiting while nothing happens.

**Capture moved to an `AudioWorklet`.** `ScriptProcessorNode` is deprecated, ran on the main thread, and had to be connected onward to the destination to keep firing, which routed the microphone into the output stream Chrome's echo canceller takes its reference from. That line is gone. The worklet is pulled because it has an input, and it has zero outputs. Whether the old wiring was hurting AEC is still unmeasured and is now moot.

**A box with no wasm built still works.** `/api/session/vad.wasm` answers 404 and the worklet falls back to the 0.06 threshold, so the page degrades to exactly its previous behaviour rather than losing the microphone.

**What is not measured is the threshold.** `hang_ms` stays at 2000 so this change moves the detector and not the feel of the turn. Picking a number needs recorded clips, and no box here has a microphone: the Rust crate's corpus and replay test did not carry over. `server/tests/test_vad.py` checks the gate on made clips: silence, a click, steady noise and a voice.

### 2026-09-17, corrected against the build

Documentation only, no code changed. These are places where the spec described something the build does not do, or carried a number that does not reproduce. All of it comes from reading the built path and from measurements on `dev3omda`.

**The VAD gate is not built, and the gate that ships is not a substitute for it.** Recorded in section 0 and in [section 8](#8-failure-modes), with a trap in [section 10](#10-known-traps-as-assertions). `opennotebook` calls `/v1/audio/vad` nowhere. The turn ends on `SILENCE_PEAK = 0.06` in `player.html`, which is an energy threshold, so a door slam above it counts as speech. The two mechanisms answer different questions and the document should stop implying that one covers the other.

**The VAD cost was wrong by 1.6x.** Sections 8 and 10 said 145 ms for a 9 s clip. Measured against the running speech server on `dev3omda`, box idle at load 0.15, best of three: 239 ms. It scales linearly with almost no fixed overhead, 0.0260x realtime on a 3.33 s clip and 0.0313x on a 30 s one, which is 26 to 31 ms of wall per second of audio on one thread. Both figures are corrected in place. Section 10's own trap applies to this correction as much as to the original: a latency number is a property of a machine, and this one is `dev3omda`.

**Echo cancellation now has a bar, and it is about 20 dB.** The 2026-09-14 amendment said echo cancellation "is not reliable" against audio the page plays through Web Audio, without saying what reliable would be, which leaves a future reader no way to tell a fix from a hope. Replicating `player.html`'s gate exactly, 8 bit quantisation included, and passing this project's own Kokoro narration through it: the narration trips the gate on 67.9% to 82.4% of polls at full level, and needs 15.5 to 20.0 dB of attenuation before it stops firing at all. At -18 dB the worst clip is down to 4.1% of polls, and at -24 dB every clip is at zero.

That number reframes the question rather than answering it. 20 dB is inside published AEC3 performance, so what is in doubt is whether AEC3 **engages** against Web Audio playback, not whether it is strong enough once it does. `chrome://webrtc-internals` reports the audio processing module's own ERLE while a session is live, and reading it on a real laptop settles the matter in one sitting. Nothing on this box can: `dev3omda` has no sound card, `/dev/snd` holds only `seq` and `timer`, and there is no display.

**One known oddity, unmeasured.** `player.html` connects the capture node to the output: `micNode.connect(micCtx.destination)`. A `ScriptProcessorNode` needs that connection to keep its `onaudioprocess` callback firing, so it is deliberate, but it also routes the microphone into the output stream AEC3 takes its reference from. Whether that degrades cancellation is unmeasured, and it is the first thing to check when somebody has a microphone in front of them. Moving capture to an `AudioWorklet` would remove the deprecated node and the need for the connection together.

**Two questions are answered and are now in section 0.** C and C++ dependencies are acceptable in this path, which the stack already assumed, since Silero runs through `sherpa-onnx`. Voice enrollment, the only route to separating a second speaker in the room, is declined: it puts a recording step in front of a listener's first question and creates biometric data to store, to solve a case the on-screen transcript already makes visible.

What stays open is `sonora`, the pure Rust WebRTC audio processing port that would be the candidate if the page has to cancel echo itself. It is two months old with one minor release, and its supported platform table does not include `wasm32`. That does not need deciding until the client measurement says the page has to do the work.

### 2026-09-14, built

Phase 2 was specified and then built in the same week, and building it contradicted the spec in four places.

**The pipeline is gone, and with it the 31 second floor.** Sections 2 and 5 designed the obvious thing: transcribe the question, ask a text model, synthesise the answer. Measured end to end that was about 31 seconds, 84% of it local synthesis. [Moshi](https://arxiv.org/abs/2410.00037) argues the pipeline itself is the latency rather than any stage of it, and the measurement agrees: sending the question's audio to a model that answers in audio puts the first sound on the wire in **746 ms to 1.3 s**, whole reply in about 2 s, at $0.0006 a question through `openai/gpt-audio-mini`. There is no transcribe step and no synthesise step in the answer path.

What it costs is sovereignty for that one call. Narration stays local, where 1.1x realtime is fine because nobody is waiting.

**Section 7 is not honoured literally, and cannot be.** It decided the answer is spoken by whichever narrator was interrupted, and the seam for it has been there since phase 1. What is not available is that narrator's *voice*: no audio-out model accepts a Kokoro voice id, and re-synthesising the answer locally in the real voice costs 1.1x realtime, which turns one second to first sound into roughly nine.

So the decision is honoured at the level it can be. A Kokoro id encodes the one thing needed: `af_bella` is American female, `am_adam` American male. Each speaker keeps one provider voice of the right gender for the life of the session, indexed by position so two speakers never collide. Measured by fundamental frequency, the same way phase 1 proved its two narrators were two people: interrupting the Host is answered at 172.7 Hz, the Expert at 117.6 Hz. The same person answers every time that person is interrupted, in the right register, named on screen. **The timbre differs from the narration**, and a reader should not be surprised by that.

**Section 8's transcript requirement is met, and it is met twice.** The requirement was that a listener sees what was heard, because 6.03% WER on this project's own vocabulary means a misheard question otherwise produces a fluent answer to something nobody asked. There are two transcripts and they disagree on purpose: a live one from the browser's own recogniser, which fills in while the listener is still speaking and is a convenience rather than a record, and `heard`, produced server-side from the exact bytes the model received, which replaces it when it lands. The authoritative one runs **in parallel with the answer**, never in front of it. Local STT is 0.42x realtime, so a five second question costs two seconds, and as a step that would double time to first sound.

**Section 5's hold to talk did not survive contact.** Three things about the turn were wrong in practice and are now built differently:

- **The narrator acknowledges the hand.** A live presenter does not go silent when someone raises a hand. A short line is generated fresh each time at high temperature, given the line it interrupted: "I see your hand, what would you like to ask?", never twice the same. A fixed set of phrases is worse than none by the fourth question.
- **The microphone opens immediately, and is armed for nothing.** Making the listener wait for a courtesy is worse than not having the courtesy. But the acknowledgement comes back down the microphone loudly enough to clear any speech threshold, measured as the studio cutting itself off and then answering its own line. So nothing counts until the narrator has stopped. Echo cancellation is not reliable against audio the page plays through Web Audio, so the fix is timing rather than a filter.
- **There is no send button.** The turn ends on two seconds of silence, the way a phone call does. Pressing a second button to say "I have finished speaking" is a thing to remember while trying to think.

**Section 6 stands, with one addition.** The playhead still lives in memory. A question asked after the deck has finished now answers and stays ended, rather than resuming: replaying a line the listener already sat through reads as the player losing its place.

**Sessions now open and close.** Not in the original spec at all. The script carries an opening that says what the session covers and why, and a closing that recaps it, written from the outline rather than the source material, they describe the shape of the session, and a bookend that invents a fact is worse than no bookend. They are lines on the first and last slides rather than slides of their own, because the deck is built from the outline titles and a title card saying "Introduction" is a slide nobody needs to look at.

**What section 13's question 1 turned out to be.** The box question is still open and still the largest lever, but it no longer gates the answer path, because the answer no longer runs on this box. It still gates narration: prep for a 3 slide, 2 voice session measured **222 seconds** end to end here.

**And question 2 answered itself.** "Is a spoken answer the product at all on this hardware" was asked when an answer took 31 seconds. At one second it is not a question.

### Still true, and worth not losing

Everything in section 10's traps table survived the build, including the two that were new. `stt_transcribe` still returns `{"text":""}` identically for silence, for noise and for unresolvable speech, and the gate is still VAD's `speech_ms`. Characters are still not seconds. And one trap should be added to that table from this build: **an SSE field consumes a single space after the colon**, so a transcript delta of `" and"` arrives as `"and"` and a reply reconstructs with every word run together. Measured on the first run of the answer route, which returned `Letterboxingisatechniquethatkeeps...`. The payload is JSON now, so the framing carries whitespace instead of eating it.

**One claim in section 5 is wrong about the service, though right about the method.** It says `tts_synthesize` is one blocking call with no stream and no cancel, and that a cancelled turn leaves the synthesiser running at 3.84 cores. That is true of `tts_synthesize`. It is not true of the speech server of the time, which had `synthesize_streaming` behind `/v1/audio/speech?response_format=pcm`, and a disconnect there genuinely stops the work, measured, CPU falls to zero within 4 to 8 seconds, against a blocking WAV call that keeps 3.8 cores for its full remaining duration. The answer path no longer uses either, so this is recorded rather than acted on.

