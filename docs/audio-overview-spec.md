# OpenNotebook, audio overview specification

An audio overview of a collection's sources: a spoken conversation about them, to listen to anywhere and to join with a spoken question at any moment. On the collection page the person adds sources, picks the Audio overview tile, a format, a length and an optional focus, and presses Generate. A collection holds as many audio overviews as are made from it, each its own session.

It is modelled on NotebookLM's Audio Overview. Where this spec copies NotebookLM or an open implementation it says so; where it departs, it says why. The labels are the ones the [mind map spec](mindmap-spec.md) uses: Measured, Decided, Observed.

## 1. What the others do

- **NotebookLM's formats (observed).** Deep Dive is the default: two unnamed hosts in "a lively conversation". Brief is under two minutes and, by Google's own announcement, one host. Critique is two hosts giving "an expert review, offering constructive feedback". Debate is two hosts with "different perspectives". A Lecture format, one host up to about 30 minutes, was in testing in December 2025.
- **NotebookLM's lengths (observed).** Shorter, Default and Longer for Deep Dive; Shorter and Default for Critique and Debate; none for Brief. A default Deep Dive runs about 10 to 12 minutes.
- **NotebookLM's Customize box (observed).** Free text: a focus, the audience, the expertise level.
- **NotebookLM's player (observed).** Play, pause, scrub, speed, download as WAV, share. Interactive mode: press Join, a host calls on you, answers from the sources, and the episode resumes.
- **NotebookLM's pipeline (observed, from its team).** Outline, revised outline, script, critique, rewrite, then a pass that adds banter and pauses. The audio model adds the "uh"s and backchannels, which a text-to-speech voice like Kokoro cannot.
- **What listeners complain about (observed).** Stock phrases ("deep dive", "let's unpack this"), hosts agreeing all the time, longer episodes that repeat rather than go deeper, and inaccuracy.
- **Open implementations.** [Podcastfy](https://github.com/souzatharsis/podcastfy) writes long episodes part by part with the transcript so far as context and bans "picking up where we left off". [podcast-creator](https://github.com/lfnovo/podcast-creator) plans segments first and says "segments are just markers, no need to reintroduce speakers". [NotebookLlama](https://github.com/meta-llama/llama-cookbook/tree/main/end-to-end-use-cases/NotebookLlama) tailors disfluencies to what each voice can render. None of them leaves any pause between turns; they concatenate raw clips.
- **ElevenLabs GenFM (observed).** Conversation or bulletin mode, short, default or long duration, an instructions prompt, and an editable script before the audio.

## 2. Decisions

| Decision | Choice | Basis |
|---|---|---|
| What it is | A session with an `audio` spec: the same sources, plan, script, edit pass, voices, player and interactive Q&A, with no deck. Its parts are chapters. | Decided. Everything that makes the script good, and the whole of interactive mode, already exists for sessions. A separate store would rebuild all of it. |
| Formats | Deep Dive, Brief, Critique, Debate | Observed, NotebookLM. |
| Voices | Brief one host, the rest two | Observed, NotebookLM's Brief. |
| Lengths | Deep Dive 5, 10 or 16 minutes; Critique and Debate 5 or 8; Brief 2. A length the format does not offer becomes Default. | Observed lengths, rounded. |
| Chapters | Brief 2; Shorter 3; Default 5 for Deep Dive, 4 otherwise; Longer 6 | Decided. A chapter is a part of the plan with its own points, so length adds depth rather than repetition. |
| Focus | Free text, carried into every prompt | Observed, NotebookLM's Customize. |
| Format rules | Each format has its own instructions, and Debate and Critique their own host roles (two sides; reviewer and author) | Decided after measuring: a Debate written with the newcomer and explainer roles had both hosts explaining the same side. |
| Stock phrases | "deep dive", "let's dive in", "unpack", "buckle up", "stay tuned" banned by name, and agreement limited to one line in six | Observed complaints. |
| Prompts | The slide session's prompts, adapted: slide copy instructions removed, "slide" said as "chapter", the format and focus added | Decided. One set of prompts to keep good, tested in both shapes. |
| Closing | Per format: Deep Dive a takeaway, a question to think about and a sign-off; Debate what each side concedes and the open question; Critique the revisions in order; Brief one sentence. Written apart and kept out of the edit pass. | Decided. |
| Pauses in the download | 220 ms when the speaker changes, 320 ms when the same one goes on, 650 ms at a chapter | Decided from conversation research (about 200 ms between turns). |
| Download | One WAV, joined on the server | Observed, NotebookLM downloads WAV. No MP3 encoder is on this box. |
| Player | The session player with an audio stage in place of the slide: the title, the format, the chapter, the hosts with the speaking one lit, a level meter. Plus a speed control and the download. | Decided. The chapters scrubber, transcript and Ask are the player's own. |
| Cost | The estimate leaves out slide design, which is most of a session's price | Decided. |

## 3. Where it lives

- `crates/opennotebook_session/src/model.rs`: `AudioSpec`, `AudioFormat`, `AudioLength`, with each format's voices, lengths, minutes and chapters.
- `crates/opennotebook_api/oschema/session/session.oschema`: `audio_format`, `audio_length` and `focus` on `SessionBuildReq` and `SessionPrepareReq`; `audio` on `Session`; `kind`, `audio_format` and `duration_ms` on `SessionSummary`; a chapter's `title` on `SessionSlide`.
- `crates/opennotebook_script/src/generate.rs`: `ScriptSpec::audio`, `adapt`, `format_rules`, `outline_shape`, the format-specific host roles in `dialogue`, and the per-format closing.
- `crates/opennotebook_build/src/prep.rs`: `write_slides: false` narrates and validates without a deck.
- `crates/opennotebook_build/src/wav.rs`: `join`, the episode as one WAV.
- `crates/opennotebook_server/src/main.rs`: `GET /api/session/episode?session=`.
- `crates/opennotebook_server/src/player.html`: the audio stage, speed and download.
- `crates/opennotebook_server/src/ask.rs`: the answer prompt says "listening" and "chapter" for an audio overview.
- `crates/opennotebook_ui/src/main.rs`: Audio Overview as an output at `/ui/new-audio-overview`, the format cards, length and focus, the Audio overviews filter and the waveform card.

## 4. Measured

On 2026-10-03, on this box.

| Build | Script model | Chapters | Lines | Audio | Estimate |
|---|---|---|---|---|---|
| Deep Dive, Shorter, Moshi paper | nova-micro (default) | 3 | 26 | 3.1 min | $0.033 |
| Debate, Shorter, Moshi paper, with a focus | Claude Haiku 4.5 | 3 | 31 | 4.1 min | not recorded |
| Brief, a 137 word note, through the browser, before the name fix below | nova-micro | 2 | 3 | 0.6 min | $0.0013 to $0.0063 |
| Brief, the same note, after it | nova-micro | 2 | 11 | 1.9 min | $0.0013 to $0.0063 |

- **Download.** The Debate as one WAV: 24 kHz mono, 4.16 minutes, 12 MB, joined in 0.75 s.
- **Interactive mode.** "Which side do you actually think is right?", asked as audio during the Debate, was answered in Adam's voice from the paper and bridged back to the chapter.
- **Browser.** Checked in a real browser: the format cards, Brief hiding the lengths, the price, Generate, the player's stage, speaker highlight and 1.25x speed, the Audio overviews filter and cards, with no console errors.

The script model decides the quality. On nova-micro the structure holds, but the content repeats a fact three times, uses banned phrases and names a planet the source never named. On Haiku the same prompts gave a real debate, with sides kept and concessions made. Haiku is the recommended script model for audio overviews.

## 5. Known limits

- **No overlap or backchannel audio.** Kokoro speaks one line at a time, so the hosts never talk over each other. Short reaction lines in the script stand in for it.
- **WAV only.** An MP3 download needs an encoder this box does not have.
- **Lecture is not offered.** It was still in testing at NotebookLM.
- **A Brief's opening and closing still depend on the model writing whole lines.** When it does not, the episode runs short of its two minutes.

## Amendments

### 2026-10-03, built and measured

The first Brief through the browser ran 0.64 minutes of its 2. The model had labelled lines with the speaker's name ("Bella:") where the prompt asked for the id, and the parser, which knows ids and positions but not names, rejected the whole-episode reply and both bookends. Names are now read as their speaker before parsing. The same Brief then ran 1.93 minutes.

The first Debate had its chapter titles shifted by two, because the plan began with a heading ("Debate: Moshi") and a label ("PART ONE"). A part with no points before the first real part, and a numbering label anywhere, are no longer read as parts. Its last line was cut mid-sentence, because the closing was still trimmed by the old cut; closings now keep whole sentences, with room for a debate's concessions.
