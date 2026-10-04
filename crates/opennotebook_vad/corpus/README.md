# corpus

Twenty recordings, and the detector can be tuned instead of guessed at. Without them `tests/replay.rs` runs and says it has nothing to measure, which is the honest answer but not a useful one.

This is the one step nothing on the box can do. `dev3omda` has no sound card: `/dev/snd` holds `seq` and `timer` and nothing else. Synthetic noise mixed into clean WAVs would produce numbers about a signal no microphone ever made, so the harness does not offer that option.

## What to record

On a normal laptop, with its built-in microphone, in the room you would actually use the studio in. Not a headset, not a treated room: the point is the ordinary case.

| How many | Condition | What to do |
|---|---|---|
| 5 | Quiet | Ask a question the way you would ask it. Normal room, nothing playing. |
| 4 | Street | Window open, traffic audible, or a recording of traffic playing from another device across the room. |
| 4 | Music | Music with lyrics playing in the background, loud enough to hear clearly while you talk. |
| 3 | Another person | Someone else talking nearby while you ask your question. |
| 4 | Thinking pause | Ask a question, stop for two to four seconds mid sentence as if working out how to phrase it, then finish. |

That is 20. Then 6 more with no question in them at all, which is what the FALSE SEND rate needs:

| How many | Condition | What to do |
|---|---|---|
| 2 | Room tone | Press record, say nothing, sit still for ten seconds. |
| 2 | Bangs | Say nothing. Shut a door, put a mug down hard, drop a book. |
| 2 | Background only | Say nothing. Leave the street noise or the music running. |

The thinking-pause clips and the no-question clips are the ones that matter most. Everything else is confirmation; those two sets are where a threshold gets decided.

## How to record them

Any recorder that writes WAV. Browser, phone, `arecord`, Audacity, it makes no difference: the harness resamples whatever it is handed and reads 16 or 24 or 32 bit, mono or stereo.

Two things to keep:

- **Do not trim the ends.** Leave three or four seconds of whatever the room sounds like after you stop talking. NEVER SENDS and CUT OFF EARLY are both measured in that tail, so a clip trimmed to the words has thrown away the only part under test.
- **Do not apply noise reduction, normalisation or a gate.** Most recorders offer one and it defeats the purpose. If the recorder has an "enhance voice" setting, turn it off.

Length: ten to twenty seconds each. Twenty clips at that length is about five minutes of recording.

## How to label them

Drop the WAVs in this directory and add one row per clip to `index.tsv`. Tab separated, `#` for comments.

```text
file            kind      speech_end_ms   note
quiet-01.wav    question  4200            plain question
think-03.wav    question  9100            pauses twice mid sentence
street-02.wav   noise     -               traffic, no speaker
```

`speech_end_ms` is the only label that needs care: the millisecond you stopped speaking, read off a waveform by eye. It is what CUT OFF EARLY is measured against, and the harness allows 250 ms of slack, so close is good enough and guessing is not. For `kind = noise` write `-`.

`kind` is `question` when you asked one and `noise` when you did not. There is no third value.

## Then

```sh
cargo test -p opennotebook_vad --test replay -- --nocapture
```

It prints a line per clip and then both detectors side by side: this crate against the 0.06 peak threshold `player.html` ships. The three rates are reported separately and never averaged, because the whole decision is the trade between them.

## Are these committed

Yes, if they are yours to commit. They are a few megabytes of your own voice asking questions about slides, and a test that needs a corpus nobody has is a test that never runs again. If any clip has someone else's voice in it, get their agreement first: the "another person" rows are the ones to check.

If a clip cannot be committed, leave it out of `index.tsv` rather than out of the directory, and say so in a comment there.
