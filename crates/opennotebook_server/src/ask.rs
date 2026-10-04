//! The voice turn: a spoken question in, a spoken answer out.
//!
//! # Why this is one call and not a pipeline
//!
//! The phase 2 spec designed the obvious thing: transcribe the question, ask a
//! text model, synthesise the answer. Measured end to end on this box that is
//! about **31 seconds**, and 84% of it is local synthesis at 1.1x realtime.
//!
//! [Moshi](https://arxiv.org/abs/2410.00037) argues the pipeline itself is the
//! problem rather than any stage of it: "their complexity induces a latency of
//! several seconds between interactions", and text as the intermediate modality
//! throws away everything about the speech that was not words.
//!
//! So this module does not have stages. It sends the question's audio to a model
//! that takes audio in and gives audio out, and streams the reply back as it
//! arrives. Measured on this box against `openai/gpt-audio`, with a real
//! synthesised question rather than text: **746 ms to the first audio byte**,
//! 2.3 s for the whole reply, $0.012 a question. `gpt-audio-mini` is 1,251 ms
//! and $0.0006.
//!
//! # Audio in, text out, the narrator's own voice
//!
//! This route used to ask that model for audio OUT as well, and it is worth
//! writing down why it stopped. No audio-out model takes a Kokoro voice id, so
//! the answer came back in a provider voice: the narrator said "go ahead" in
//! their voice, said "let me think about that" in their voice, and then a
//! stranger answered the question. One turn, two people. That is the most
//! audible seam this product had.
//!
//! So the model is asked for text and the answer is synthesised HERE, by the
//! same local Kokoro voice the narration and the courtesy lines use. The
//! question still goes up as audio — it is never transcribed in front of the
//! model, which is the stage Moshi's argument is actually about — and the
//! synthesis is streamed clause by clause as the words arrive, so the first
//! sound does not wait for the last word. Measured on a one-sentence answer:
//! first sound in roughly two seconds against 0.75, under the "thanks" line
//! that is playing anyway, and in the right voice.
//!
//! # The call goes through the shared client
//!
//! The question's audio goes to the model through `opennotebook_ai` like every
//! other model call, and `send_stream` yields the deltas already normalized.
//!
//! One behaviour worth knowing, because this route depends on it. An audio model
//! answers with `content: null` and puts the words in `audio.transcript`.
//! `opennotebook_ai` maps that transcript onto ordinary `TextDelta`s, so the
//! `said` events below come from `TextDelta` and not from a field named
//! transcript.

use std::convert::Infallible;

use axum::body::Bytes;
use axum::extract::Query;
use axum::response::sse::Event;
use axum::response::{IntoResponse, Response};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as B64;
use futures_util::{FutureExt, Stream, StreamExt};
use serde::Deserialize;
use serde_json::json;

use opennotebook_session::{NarrationLine, Session, Speaker};

use opennotebook_ai::{CompletionEvent, CompletionStream, ContentPart, Message};

/// The model that answers, from the operator setting. See
/// [`opennotebook_session::settings`] for what the default is and why.
async fn answer_model() -> String {
    opennotebook_session::settings::answer_model().await
}

/// The model endpoint every call goes through; see `opennotebook_session::ai`.
async fn provider() -> Result<opennotebook_ai::Provider, String> {
    opennotebook_session::ai::provider().await
}

/// Reply audio comes back as raw 16-bit little-endian PCM at this rate, which
/// is what the browser's AudioContext is handed directly.
#[cfg_attr(not(test), allow(dead_code))]
pub const REPLY_RATE: u32 = 24_000;

#[cfg(test)]
mod tests {
    /// The rate is a contract with the player: the reply arrives as headerless
    /// PCM and the browser builds an AudioBuffer at exactly this rate. Wrong by
    /// a factor and the answer plays chipmunked or slurred, which is a bug that
    /// sounds like a model problem.
    #[test]
    fn reply_audio_is_pcm16_at_24k() {
        assert_eq!(super::REPLY_RATE, 24_000);
    }

    /// An answer is voiced in whole sentences: a comma never splits one, a
    /// decimal point is not a sentence end, and an unpunctuated tail still
    /// gets said.
    /// The three parts come out in speaking order, whatever the deltas look
    /// like: the thinking line whole as soon as it closes, the answer and the
    /// handback by sentence, and a tag split across deltas still found.
    #[test]
    fn a_streamed_answer_comes_out_as_typed_parts_in_order() {
        let mut p = super::AnswerStream::default();
        let reply = "<think>Hmm, so you want to know why attention helps.</think>\n\
                     <answer>It lets every word look at every other word. That is why it handles long \
                     sentences well.</answer>\n<back>Anyway, I was saying the model is simple.</back>";
        let mut got = Vec::new();
        // Deliberately awkward chunks: tags cut in half.
        for chunk in reply.as_bytes().chunks(7) {
            got.extend(p.push(std::str::from_utf8(chunk).unwrap()));
        }
        got.extend(p.finish());
        assert_eq!(
            got,
            vec![
                "Hmm, so you want to know why attention helps.",
                "It lets every word look at every other word.",
                "That is why it handles long sentences well.",
                "Anyway, I was saying the model is simple.",
            ]
        );
        let a = p.into_answer();
        assert_eq!(
            a.think.trim(),
            "Hmm, so you want to know why attention helps."
        );
        assert!(a.answer.contains("every other word"));
        assert!(a.back.starts_with("Anyway"));
    }

    /// A model that ignores the format still gets its answer said.
    #[test]
    fn an_untagged_reply_is_all_answer() {
        let mut p = super::AnswerStream::default();
        let mut got = p.push("It is 2.5 times faster. Mostly because ");
        got.extend(p.push("of caching"));
        got.extend(p.finish());
        assert_eq!(
            got,
            vec!["It is 2.5 times faster.", "Mostly because of caching"]
        );
    }

    #[test]
    fn an_answer_is_voiced_in_whole_sentences() {
        assert_eq!(
            super::speakable_parts("It is 2.5 times faster, roughly. Why? Attention"),
            vec!["It is 2.5 times faster, roughly.", "Why?", "Attention"]
        );
        assert!(super::speakable_parts("  ").is_empty());
    }

    /// A clause leaves as soon as there is one, or the answer starts late.
    #[test]
    fn a_finished_sentence_is_taken_and_the_rest_is_kept() {
        let mut buf = "Letterboxing keeps the frame. It pads the sides.".to_string();
        assert_eq!(
            super::take_speakable(&mut buf).as_deref(),
            Some("Letterboxing keeps the frame.")
        );
        assert_eq!(buf, " It pads the sides.");
        // The tail has no boundary yet: it waits for the next delta rather than
        // going out as a fragment.
        assert_eq!(super::take_speakable(&mut buf), None);
    }

    /// The bug this cost rule exists for: a mid-token dot is not a sentence.
    /// `gpt-4.1` spoken as two clauses is heard as a stutter.
    #[test]
    fn a_dot_inside_a_token_is_not_a_boundary() {
        let mut buf = "The model is gpt-4.1 and it".to_string();
        assert_eq!(super::take_speakable(&mut buf), None);
        assert_eq!(buf, "The model is gpt-4.1 and it");
    }

    /// A comma cuts only once the clause is worth the synthesis call.
    #[test]
    fn a_comma_cuts_only_a_long_enough_clause() {
        // Two rules hold here at once. The comma is in the second word, well
        // under MIN_CLAUSE, so it does not cut; and the closing dot is the last
        // character in the buffer, so it is not a boundary either — mid-stream
        // that dot is indistinguishable from `gpt-4.` still waiting for its
        // `1`. The whole short answer therefore stays put and goes out as the
        // tail, in one piece, which is what it should sound like.
        let mut buf = "Yes, it does.".to_string();
        assert_eq!(super::take_speakable(&mut buf), None);
        assert_eq!(buf, "Yes, it does.");

        let mut long =
            "Letterboxing preserves the original aspect ratio of the frame, which matters."
                .to_string();
        assert_eq!(
            super::take_speakable(&mut long).as_deref(),
            Some("Letterboxing preserves the original aspect ratio of the frame,")
        );
    }

    /// Nothing is lost when the model stops without punctuation: the caller
    /// speaks whatever is left, so an un-cut buffer must come back whole.
    #[test]
    fn an_unpunctuated_answer_is_left_in_the_buffer() {
        let mut buf = "about two thirds".to_string();
        assert_eq!(super::take_speakable(&mut buf), None);
        assert_eq!(buf, "about two thirds");
    }

    use opennotebook_session::{
        LineId, NarrationLine, Session, SessionSlide, SessionState, Speaker, SpeakerId,
    };

    fn line(id: &str, who: &str, ordinal: u32, text: &str) -> NarrationLine {
        NarrationLine {
            line_id: LineId(id.into()),
            speaker_id: SpeakerId(who.into()),
            ordinal,
            text: text.into(),
            audio_path: None,
            duration_ms: None,
            cues: Vec::new(),
        }
    }

    /// Host then expert on one slide, the expert jokey so the sample is
    /// recognisable in the prompt.
    fn two_speakers() -> Session {
        let sp = |id: &str, voice: &str, name: &str, role: &str| Speaker {
            speaker_id: SpeakerId(id.into()),
            voice_id: voice.into(),
            display_name: name.into(),
            role: role.into(),
        };
        Session {
            sid: "s".into(),
            title: "t".into(),
            collection_name: "s".into(),
            collection_sid: None,
            deck_ref: None,
            speakers: vec![
                sp("host", "af_bella", "Bella", "asks the questions"),
                sp("expert", "am_adam", "Adam", "explains, with a joke or two"),
            ],
            // Through serde rather than a struct literal, so fields the slide
            // grows with `#[serde(default)]` do not break this fixture.
            slides: vec![
                serde_json::from_value::<SessionSlide>(serde_json::json!({
                    "slide_ref": { "collection": "c", "presentation": "p", "slide": "one" },
                    "ordinal": 0,
                    "aspect": { "width": 1376, "height": 768 },
                    "lines": [
                        line("l1", "host", 0, "So what is a vector index?"),
                        line("l2", "expert", 1, "Think of it as a very nosy librarian, honestly."),
                        line("l3", "host", 2, "And how fast is it?"),
                    ],
                }))
                .expect("a slide fixture"),
            ],
            state: SessionState::Ready,
            prep_job_sid: None,
            failure: None,
            pinned: false,
            audio: None,
            collection: None,
            spent_usd: None,
            style: None,
        }
    }

    fn at(line: &str, offset_ms: i64) -> super::AskQuery {
        super::AskQuery {
            session: "s".into(),
            slide: 0,
            line: line.into(),
            offset_ms,
        }
    }

    /// Mid-line, the answer comes from whoever is talking.
    #[test]
    fn the_speaker_mid_line_answers() {
        let s = two_speakers();
        let (voice, name) = super::narrating_speaker(&s, &at("l2", 1500));
        assert_eq!((voice.as_str(), name.as_str()), ("am_adam", "Adam"));
    }

    /// Cut in between lines: the next line is queued at zero but nobody has
    /// heard its speaker yet, so the one who just finished answers.
    #[test]
    fn an_unheard_line_hands_the_floor_back() {
        let s = two_speakers();
        let (voice, name) = super::narrating_speaker(&s, &at("l3", 0));
        assert_eq!((voice.as_str(), name.as_str()), ("am_adam", "Adam"));
    }

    /// Before anything has played there is nobody to hand back to.
    #[test]
    fn the_first_line_keeps_its_speaker() {
        let s = two_speakers();
        let (_, name) = super::narrating_speaker(&s, &at("l1", 0));
        assert_eq!(name, "Bella");
    }

    /// Only a request to carry on skips the model; a question that happens to
    /// contain "go" or "continue" does not.
    #[test]
    fn keep_going_is_not_a_question() {
        for t in [
            "Keep going, please.",
            "keep going",
            "Go on.",
            "Okay, continue.",
            "Please continue",
            "Carry on",
            "Never mind.",
            "No, nothing, sorry.",
            "Go ahead.",
        ] {
            assert!(super::is_resume(t), "{t}");
        }
        for t in [
            "Why?",
            "So, what's Mochi at all",
            "Keep going with the latency part",
            "How does it continue speaking while I talk?",
            "",
        ] {
            assert!(!super::is_resume(t), "{t}");
        }
    }

    /// The prompt is written as that speaker: their name, their role, the
    /// co-host, and their own words as the sample of how they talk.
    #[test]
    fn the_prompt_is_in_the_speakers_persona() {
        let s = two_speakers();
        let p = super::context_for(&s, &at("l3", 0), "normal", "English");
        assert!(p.starts_with("You are Adam,"), "{p}");
        assert!(p.contains("explains, with a joke or two"), "{p}");
        assert!(p.contains("Bella (asks the questions)"), "{p}");
        assert!(p.contains("- Think of it as a very nosy librarian"), "{p}");
        // The host's line is not offered as Adam's voice.
        assert!(!p.contains("- So what is a vector index?"), "{p}");
        // l3 was not heard, so the cut mark sits in front of it.
        assert!(p.contains("before this next line"), "{p}");
        assert!(!p.contains("And how fast is it?"), "{p}");
        assert!(p.contains("plain spoken English"), "{p}");
        assert!(p.contains("<answer>ONE or TWO short sentences"), "{p}");
        // The three typed parts are asked for.
        assert!(p.contains("<think>") && p.contains("<back>"), "{p}");
    }

    /// An audio overview's listener is listening, and its parts are chapters.
    #[test]
    fn the_prompt_of_an_audio_overview_says_listening_and_chapters() {
        let mut s = two_speakers();
        s.audio = Some(opennotebook_session::AudioSpec {
            format: opennotebook_session::AudioFormat::DeepDive,
            length: opennotebook_session::AudioLength::Default,
            focus: String::new(),
        });
        let p = super::context_for(&s, &at("l2", 900), "normal", "English");
        assert!(
            p.contains("an audio overview the listener is listening to"),
            "{p}"
        );
        assert!(
            p.contains("THE EPISODE'S CHAPTERS") && p.contains("CURRENT CHAPTER"),
            "{p}"
        );
        assert!(!p.to_lowercase().contains("slide"), "{p}");
    }

    /// The faults of a real turn, each answered in the prompt: talking about
    /// the listener instead of to them, "keep going" taken as a question, a
    /// misheard name, and a handback that repeats the line replayed after it.
    #[test]
    fn the_prompt_speaks_to_the_listener_and_knows_keep_going() {
        let s = two_speakers();
        let p = super::context_for(&s, &at("l2", 900), "normal", "English");
        assert!(p.contains("as \"you\""), "{p}");
        assert!(p.contains("never call them \"they\""), "{p}");
        assert!(p.contains("\"keep going\""), "{p}");
        assert!(p.contains("\"Mochi\" for \"Moshi\""), "{p}");
        assert!(p.contains("do not repeat or paraphrase it"), "{p}");
        assert!(
            !p.contains("I was saying that"),
            "the example the model copied is gone"
        );
        assert!(p.contains("THE SESSION'S SLIDES"), "{p}");
        assert!(p.contains("1. one  <- they are here"), "{p}");
    }

    /// Answer length and language come from settings and land in the prompt.
    #[test]
    fn the_prompt_follows_length_and_language() {
        let s = two_speakers();
        let p = super::context_for(&s, &at("l2", 900), "detailed", "French");
        assert!(p.contains("plain spoken French"), "{p}");
        assert!(p.contains("<answer>THREE or FOUR sentences"), "{p}");
        let p = super::context_for(&s, &at("l2", 900), "short", "");
        assert!(p.contains("plain spoken English"), "{p}");
        assert!(p.contains("<answer>ONE short sentence"), "{p}");
    }
}

#[derive(Deserialize)]
pub struct AskQuery {
    pub session: String,
    /// Where the playhead was when the listener cut in. Carried so the answer
    /// can be grounded in what they had actually heard, not in the whole deck.
    #[serde(default)]
    pub slide: i64,
    #[serde(default)]
    pub line: String,
    #[serde(default)]
    pub offset_ms: i64,
}

/// `GET /api/session/ack` — the narrator noticing you want to speak.
///
/// A live presenter does not go silent the instant someone raises a hand; they
/// say "oh, looks like we have a question". Without it the session stops dead
/// and the listener is talking into a hole, which is the single thing that made
/// the turn feel like a script rather than a conversation.
///
/// Picked from a written list, not generated. This used to be a model call at
/// a high temperature, on the theory that a fixed set of three phrases is worse
/// than none by the fourth question. The theory was right about three phrases
/// and wrong about the fix: [`crate::banter`] holds twenty-four and walks them
/// in rotation, so the seam the model call was buying stays shut for nothing.
/// A raised hand costs no tokens at all now — the only model call left in the
/// turn is the one that answers the actual question.
pub async fn serve_ack(Query(q): Query<AskQuery>) -> Response {
    ack_stream(&q, crate::banter::Bank::Invite).await
}

/// `GET /api/session/thanks` — what the narrator says once the question is in.
///
/// Same shape and same bank machinery as the invite, different words. It fills
/// the second or two between the send and the first sound of the answer, and it
/// is the "collaborate with the listener" half of the turn.
pub async fn serve_thanks(Query(q): Query<AskQuery>) -> Response {
    ack_stream(&q, crate::banter::Bank::Thanks).await
}

/// One courtesy line, written rather than generated.
///
/// This used to be a `gpt-audio-mini` call at temperature 1.1, per question,
/// purely for variety. A written bank gives the same variety for nothing, and
/// [`crate::banter`] walks it so a line never repeats until the bank is spent.
/// The audio is the session's own Kokoro voice, synthesised once per voice per
/// line and then read from a cache, so a returning session pays nothing at all.
///
/// The events are byte for byte what the generated version emitted, so the
/// page's `speak()` needed no change: `speaker`, then `said`, then `audio`.
async fn ack_stream(q: &AskQuery, bank: crate::banter::Bank) -> Response {
    // Off in settings, or a session in another language: the bank is English,
    // and an English "go ahead" before a French answer is two languages in one
    // turn. An empty turn is a `done` with nothing said, which the page
    // already treats as "nothing to show".
    let courtesy = opennotebook_session::settings::is_on(
        &opennotebook_session::settings::get(opennotebook_session::settings::COURTESY_KEY).await,
    );
    let language = opennotebook_session::settings::language().await;
    if !courtesy || (!language.is_empty() && language != "English") {
        let s = async_stream::stream! {
            yield Ok::<_, Infallible>(Event::default().event("done").data(json!({ "t": "" }).to_string()));
        };
        return axum::response::Sse::new(s).into_response();
    }
    let session = match crate::session_impl::load_session(&q.session).await {
        Ok(Some(s)) => s,
        _ => return err_stream("no session").into_response(),
    };
    let (voice, speaker_name) = narrating_speaker(&session, q);
    // A hand is up: the hold lines will be wanted in a few seconds.
    if bank == crate::banter::Bank::Invite {
        warm_holds(voice.clone());
    }
    let line = crate::banter::next_line(&q.session, bank);
    let spoken = crate::banter::spoken(&voice, line).await;

    let speaker_name = speaker_name.to_string();
    let line = line.to_string();
    let s = async_stream::stream! {
        yield Ok::<_, Infallible>(
            Event::default().event("speaker").data(json!({ "t": speaker_name }).to_string()),
        );
        yield Ok(Event::default().event("said").data(json!({ "t": line }).to_string()));

        // A courtesy that cannot be spoken is still a courtesy. The text is
        // already on screen, so a missing voice provider costs the sound and
        // not the turn.
        if let Some(wav) = spoken {
            for chunk in wav_to_pcm16_chunks(&wav) {
                yield Ok(Event::default().event("audio").data(B64.encode(&chunk)));
            }
        }
        yield Ok(Event::default().event("done").data(json!({ "t": line }).to_string()));
    };
    axum::response::Sse::new(s).into_response()
}

/// A Kokoro WAV as the little-endian PCM16 frames the page's sink plays.
///
/// Chunked so a long line starts playing before it has all arrived, the same
/// way the model's own stream did.
fn wav_to_pcm16_chunks(wav: &[u8]) -> Vec<Vec<u8>> {
    let Ok(pcm) = opennotebook_vad::wav::decode(wav) else {
        return Vec::new();
    };
    // The page builds its buffers at REPLY_RATE and nothing resamples on the
    // way. A voice provider that changed rate would play back at the wrong
    // speed, which sounds like a bug in the voice rather than in the wiring.
    if (pcm.rate - REPLY_RATE as f32).abs() > 1.0 {
        eprintln!(
            "banter: {} Hz from tts_synthesize, the page plays {REPLY_RATE} Hz",
            pcm.rate
        );
        return Vec::new();
    }
    pcm.samples
        .chunks(4800) // 200 ms
        .map(|c| {
            let mut out = Vec::with_capacity(c.len() * 2);
            for s in c {
                out.extend_from_slice(&((s.clamp(-1.0, 1.0) * 32767.0) as i16).to_le_bytes());
            }
            out
        })
        .collect()
}

/// `POST /api/session/ask` — body is the question as a WAV.
///
/// Replies with an event stream rather than a bare audio body so the transcript
/// of what was *heard* can arrive beside the audio. Phase 2 §8: a listener whose
/// question was misheard otherwise gets a confident answer to a question they
/// did not ask, with nothing on screen to show it. Measured word error rate on
/// this project's own vocabulary was 6%, and every error was domain jargon.
pub async fn serve_ask(Query(q): Query<AskQuery>, body: Bytes) -> Response {
    if body.is_empty() {
        return err_stream("no audio was recorded").into_response();
    }

    // The gate phase 2 §8 decided and nothing implemented, now in front of the
    // model instead of nowhere.
    //
    // `stt_transcribe` returns `{"text":""}` byte for byte for silence, for
    // noise and for speech it could not resolve, so the emptiness of a
    // transcript cannot separate them — and it arrives in parallel with the
    // answer, which means by the time it could tell us, the answer model has
    // already been called and billed. This runs on the same bytes the model
    // would have received, before it receives them.
    //
    // Malformed audio stays loud, per §8: `analyze_wav` fails by name and that
    // becomes a `failed` frame, which is a different thing from silence.
    let speech_ms =
        match opennotebook_vad::wav::analyze_wav(&body, &opennotebook_vad::Config::DEFAULT) {
            Err(e) => return err_stream(&e).into_response(),
            Ok(a) if a.is_silent() => return silent_stream(a),
            Ok(a) => a.speech_ms,
        };
    let session = match crate::session_impl::load_session(&q.session).await {
        Ok(Some(s)) => s,
        Ok(None) => return err_stream(&format!("no session `{}`", q.session)).into_response(),
        Err(e) => return err_stream(&e).into_response(),
    };

    let provider = match provider().await {
        Ok(p) => p,
        Err(e) => return err_stream(&e).into_response(),
    };

    let system = context_for(
        &session,
        &q,
        &opennotebook_session::settings::get(opennotebook_session::settings::ANSWER_LENGTH_KEY)
            .await,
        &opennotebook_session::settings::language().await,
    );
    let wav = B64.encode(&body);
    // The narrator who was interrupted, in their OWN voice. Same lookup the
    // courtesy lines use, so one turn is one person from "go ahead" to the last
    // word of the answer.
    let (voice, speaker_name) = narrating_speaker(&session, &q);

    // The question itself, as a content part rather than a transcript of it.
    // `mime_type` is the provider's `format` field, which wants a container
    // name and not a MIME type.
    let mut question = Message::user("");
    question.content = vec![ContentPart::Audio {
        mime_type: "wav".to_string(),
        data: wav,
    }];

    let stream = provider
        .completions()
        .model(answer_model().await)
        // No `.audio()`. That sets the audio modality AND a provider voice, and
        // a provider voice is the bug. Without it the same model reads the
        // question's audio and answers in text, which is the only form this box
        // can speak in the narrator's voice.
        .message(Message::system(system))
        .message(question)
        .send_stream()
        .await;
    let stream = match stream {
        Ok(s) => s,
        Err(e) => return err_stream(&format!("the model did not answer: {e}")).into_response(),
    };

    // The question is transcribed LOCALLY and in parallel, never in front of the
    // answer. Phase 2 §8 made showing it a requirement: measured word error rate
    // on this project's own vocabulary is 6% and every error was domain jargon,
    // so a misheard question otherwise produces a fluent answer to something
    // nobody asked with nothing on screen to show it. Offline STT runs at 0.42x
    // realtime here, so a five second question costs about two seconds — which
    // would double time-to-first-sound if it were a step. It is not a step.
    // One transcription, read twice: by the `heard` event, and — for a clip
    // short enough to be "keep going" — by the relay before it speaks.
    let transcript: Transcript = {
        let wav = body.to_vec();
        async move { transcribe(&wav).await }.boxed().shared()
    };
    let heard = heard_stream(transcript.clone());
    let gate = (speech_ms <= RESUME_MAX_SPEECH_MS).then_some(transcript);
    // Hold lines are English, like the rest of the courtesy bank, and follow
    // the same switch: off, or a session in another language, and the wait is
    // silent rather than bilingual.
    let hold = opennotebook_session::settings::is_on(
        &opennotebook_session::settings::get(opennotebook_session::settings::COURTESY_KEY).await,
    ) && {
        let language = opennotebook_session::settings::language().await;
        language.is_empty() || language == "English"
    };
    let merged = futures_util::stream::select(
        relay(stream, speaker_name, voice, q.session.clone(), hold, gate),
        heard,
    );
    axum::response::Sse::new(merged).into_response()
}

/// How much of a line has to have played before its speaker counts as the one
/// talking. Below this the listener has heard nothing of it: the page cut in
/// between lines, after the next one was queued but before it made a sound.
const HEARD_MS: i64 = 400;

/// Every line of the session, in the order it is played.
fn lines_in_play_order(session: &Session) -> Vec<&NarrationLine> {
    session
        .slides_in_order()
        .into_iter()
        .flat_map(|s| s.lines_in_order())
        .collect()
}

/// The line whose speaker last actually spoke, as an index into
/// [`lines_in_play_order`].
///
/// `q.line` is the line under the playhead, which is not always the one the
/// listener last heard: a hand raised in the gap between two lines arrives with
/// the NEXT line queued at offset zero. Answering from that line's speaker puts
/// the question to someone who has not said a word yet, which is exactly what a
/// real conversation never does. So an unheard line hands the floor back to the
/// one before it.
fn floor_index(lines: &[&NarrationLine], q: &AskQuery) -> Option<usize> {
    let at = lines.iter().position(|l| l.line_id.0 == q.line)?;
    if q.offset_ms < HEARD_MS && at > 0 {
        Some(at - 1)
    } else {
        Some(at)
    }
}

/// Who answers: the speaker who last held the floor.
///
/// Before the first line starts nobody holds it, so §7's decided fallback
/// applies: `speakers[0]`, session order, not a new field.
///
/// One lookup for the whole turn: the invite, the thanks and the answer are all
/// synthesised locally by Kokoro from this speaker's voice id, and the answer's
/// prompt is written in this speaker's persona. There used to be a second
/// function mapping the speaker onto the answer model's voice pool, and it was
/// the reason a turn had two people in it.
fn floor_speaker<'a>(session: &'a Session, q: &AskQuery) -> Option<&'a Speaker> {
    let lines = lines_in_play_order(session);
    floor_index(&lines, q)
        .and_then(|i| {
            session
                .speakers
                .iter()
                .find(|s| s.speaker_id == lines[i].speaker_id)
        })
        .or_else(|| session.speakers.first())
}

fn display_name(sp: &Speaker) -> String {
    // Sessions built before speakers had names still say "Host"; the answer
    // names them the way the player does. See `opennotebook_sdk::voices`.
    let name = opennotebook_sdk::voices::display_name(&sp.display_name, &sp.voice_id);
    if name.is_empty() {
        sp.speaker_id.0.clone()
    } else {
        name
    }
}

/// The floor speaker's OWN Kokoro voice and display name.
fn narrating_speaker(session: &Session, q: &AskQuery) -> (String, String) {
    match floor_speaker(session, q) {
        Some(sp) => (sp.voice_id.clone(), display_name(sp)),
        None => (String::new(), "Studio".to_string()),
    }
}

/// The local transcription of the question, shared by everything that reads it.
type Transcript =
    futures_util::future::Shared<futures_util::future::BoxFuture<'static, Result<String, String>>>;

/// One event, when the local transcription lands.
fn heard_stream(transcript: Transcript) -> impl Stream<Item = Result<Event, Infallible>> {
    async_stream::stream! {
        match transcript.await {
            Ok(text) if !text.trim().is_empty() => {
                yield Ok(Event::default().event("heard").data(json!({ "t": text }).to_string()));
            }
            // An empty transcript is NOT the discriminator for "said nothing":
            // measured, silence, noise and unresolvable speech all return
            // `{"text":""}` byte for byte. The browser already gates on having
            // recorded something, so nothing is claimed here rather than
            // guessing which of the three it was.
            Ok(_) => {}
            Err(e) => {
                yield Ok(Event::default().event("heard_failed").data(json!({ "t": e }).to_string()));
            }
        }
    }
}

async fn transcribe(wav: &[u8]) -> Result<String, String> {
    opennotebook_speech::from_settings()
        .await
        .transcribe(wav)
        .await
        .map_err(|e| e.to_string())
}

/// Turn the model's text stream into ours, speaking each part as it is ready.
///
/// # The shape of an answer
///
/// The model answers in three typed parts ([`SpokenAnswer`]): a line of
/// thinking aloud, the answer and a word of discussion, and a handback to the
/// session. That is how a speaker who was interrupted actually talks — "hmm,
/// so you're asking whether…", the answer, "anyway, I was saying…" — and it is
/// what makes a fast start possible without the answer stuttering.
///
/// # Why this order of work
///
/// Speaking clause by clause as words arrived stopped mid-answer whenever the
/// writing or the voicing fell behind the speaking. Preparing the whole answer
/// first fixed that and made the listener wait twenty-five seconds, filled with
/// canned "one moment" lines. This keeps the best of both: the thinking line is
/// short, so it is voiced and playing within a couple of seconds, and while it
/// plays the answer's sentences are voiced several at a time, in order, as the
/// model writes them. By the time the thinking line ends the answer's first
/// sentence is ready behind it.
///
/// One ordered queue carries everything, so nothing can overlap. A canned hold
/// line survives only as the fallback for a model that has said nothing at all
/// after a few seconds.
fn relay(
    mut stream: CompletionStream,
    speaker: String,
    voice: String,
    sid: String,
    hold: bool,
    gate: Option<Transcript>,
) -> impl Stream<Item = Result<Event, Infallible>> {
    async_stream::stream! {
        yield Ok(Event::default().event("speaker").data(json!({ "t": speaker }).to_string()));

        // "Keep going" is not a question. Told so in its prompt, the answer
        // model still answered it — with a "third issue" the material never
        // mentions. A short clip waits for its local transcript (about a
        // second, under the "thanks" line the page is playing anyway); when
        // all it says is carry on, the narrator says so and the session
        // resumes, and the model's reply is dropped unread.
        if let Some(gate) = gate {
            let heard = tokio::time::timeout(std::time::Duration::from_millis(RESUME_WAIT_MS), gate).await;
            if let Ok(Ok(text)) = heard && is_resume(&text) {
                    let line = crate::banter::next_line(&sid, crate::banter::Bank::Resume);
                    yield Ok(Event::default().event("said").data(json!({ "t": format!("{line} ") }).to_string()));
                    if let Some(wav) = crate::banter::spoken(&voice, line).await {
                        for chunk in wav_to_pcm16_chunks(&wav) {
                            yield Ok(Event::default().event("audio").data(B64.encode(&chunk)));
                        }
                    }
                    yield Ok(Event::default().event("done").data(json!({
                        "t": line, "think": line, "answer": "", "back": "",
                    }).to_string()));
                    return;
                }
        }

        // Parts to voice go in here in speaking order; voiced parts come out of
        // `voiced` in the same order, several synthesised at once.
        let (parts_tx, parts_rx) = tokio::sync::mpsc::unbounded_channel::<String>();
        let (voiced_tx, mut voiced) = tokio::sync::mpsc::unbounded_channel::<(String, Vec<Vec<u8>>)>();
        {
            let voice = voice.clone();
            tokio::spawn(async move {
                let parts = futures_util::stream::unfold(parts_rx, |mut rx| async move {
                    rx.recv().await.map(|t| (t, rx))
                });
                let out = parts
                    .map(|text| {
                        let voice = voice.clone();
                        async move {
                            let audio = speak_chunk(&voice, &text).await;
                            (text, audio)
                        }
                    })
                    .buffered(VOICING_PARALLEL);
                let mut out = std::pin::pin!(out);
                while let Some(done) = out.next().await {
                    if voiced_tx.send(done).is_err() {
                        break;
                    }
                }
            });
        }

        let mut parser = AnswerStream::default();
        let mut parts_tx = Some(parts_tx);
        let mut spoken_any = false;
        let mut model_done = false;
        let hold_at = tokio::time::Instant::now() + std::time::Duration::from_millis(HOLD_FIRST_MS);
        let mut held = false;

        loop {
            tokio::select! {
                event = stream.next(), if !model_done => match event {
                    Some(CompletionEvent::TextDelta(t)) => {
                        for part in parser.push(&t) {
                            if let Some(tx) = &parts_tx { let _ = tx.send(part); }
                        }
                    }
                    Some(CompletionEvent::Error(e)) => {
                        yield Ok(Event::default().event("failed").data(json!({ "t": e.to_string() }).to_string()));
                        return;
                    }
                    Some(_) => {}
                    None => {
                        model_done = true;
                        for part in parser.finish() {
                            if let Some(tx) = &parts_tx { let _ = tx.send(part); }
                        }
                        // Closing the queue lets the voicer finish and end.
                        parts_tx = None;
                    }
                },
                got = voiced.recv() => match got {
                    Some((text, audio)) => {
                        spoken_any = true;
                        // The words go out with their sound, so the bubble
                        // shows what is being said, not what is coming.
                        yield Ok(Event::default().event("said").data(json!({ "t": format!("{text} ") }).to_string()));
                        for chunk in audio {
                            yield Ok(Event::default().event("audio").data(B64.encode(&chunk)));
                        }
                    }
                    None => break,
                },
                _ = tokio::time::sleep_until(hold_at), if hold && !held && !spoken_any => {
                    held = true;
                    for ev in hold_events(&sid, &voice).await {
                        yield Ok(ev);
                    }
                }
            }
        }
        let answer = parser.into_answer();
        yield Ok(Event::default().event("done").data(json!({
            "t": answer.spoken(),
            "think": answer.think,
            "answer": answer.answer,
            "back": answer.back,
        }).to_string()));
    }
}

/// A spoken answer, typed: what the model wrote, split into its three parts.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct SpokenAnswer {
    /// Thinking aloud before answering.
    pub think: String,
    /// The answer and a word of discussion.
    pub answer: String,
    /// The handback to the session.
    pub back: String,
}

impl SpokenAnswer {
    /// Everything said, in order.
    pub fn spoken(&self) -> String {
        [&self.think, &self.answer, &self.back]
            .iter()
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" ")
    }
}

/// Which part the stream is in.
#[derive(Debug, Default, Clone, Copy, PartialEq)]
enum Part {
    #[default]
    Before,
    Think,
    Answer,
    Back,
    /// No tags at all: the whole reply is the answer.
    Untagged,
}

/// Reads the model's reply as it streams and hands out speakable pieces in
/// order: the thinking line whole as soon as it closes, the answer and the
/// handback sentence by sentence as each sentence ends.
///
/// Tags are matched on the accumulated text, so a tag split across two deltas
/// ("<ans" + "wer>") is still found. A model that ignores the format is not a
/// failure: after a few characters with no opening tag, everything is treated
/// as the answer.
#[derive(Debug, Default)]
struct AnswerStream {
    text: String,
    /// How much of `text` has been consumed.
    at: usize,
    part: Part,
    answer: SpokenAnswer,
}

impl AnswerStream {
    fn push(&mut self, delta: &str) -> Vec<String> {
        self.text.push_str(delta);
        self.drain(false)
    }

    fn finish(&mut self) -> Vec<String> {
        self.drain(true)
    }

    fn into_answer(self) -> SpokenAnswer {
        self.answer
    }

    fn drain(&mut self, end: bool) -> Vec<String> {
        let mut out = Vec::new();
        loop {
            let rest = &self.text[self.at..];
            match self.part {
                Part::Before => {
                    let trimmed = rest.trim_start();
                    let skip = rest.len() - trimmed.len();
                    if let Some((tag, part)) = [
                        ("<think>", Part::Think),
                        ("<answer>", Part::Answer),
                        ("<back>", Part::Back),
                    ]
                    .iter()
                    .find(|(t, _)| trimmed.starts_with(t))
                    {
                        self.at += skip + tag.len();
                        self.part = *part;
                        continue;
                    }
                    // Not a tag, and cannot become one.
                    if !trimmed.is_empty()
                        && !"<think>".starts_with(trimmed)
                        && !"<answer>".starts_with(trimmed)
                        && !"<back>".starts_with(trimmed)
                    {
                        self.part = Part::Untagged;
                        continue;
                    }
                    return out;
                }
                Part::Think | Part::Answer | Part::Back => {
                    let close = match self.part {
                        Part::Think => "</think>",
                        Part::Answer => "</answer>",
                        _ => "</back>",
                    };
                    if let Some(i) = rest.find(close) {
                        let body = rest[..i].to_string();
                        self.emit_part(&body, &mut out);
                        self.at += i + close.len();
                        self.part = Part::Before;
                        continue;
                    }
                    if end {
                        let body = rest.to_string();
                        self.emit_part(&body, &mut out);
                        self.at = self.text.len();
                        return out;
                    }
                    // The thinking line is spoken whole; the others by sentence,
                    // holding back anything that could be the closing tag.
                    if self.part != Part::Think {
                        let safe = rest.rfind('<').unwrap_or(rest.len());
                        if let Some(cut) = last_sentence_end(&rest[..safe]) {
                            let piece = rest[..cut].to_string();
                            self.record(&piece);
                            out.extend(speakable_parts(&piece));
                            self.at += cut;
                        }
                    }
                    return out;
                }
                Part::Untagged => {
                    let rest = rest.to_string();
                    if end {
                        self.part = Part::Answer;
                        self.emit_part(&rest, &mut out);
                        self.at = self.text.len();
                        return out;
                    }
                    if let Some(cut) = last_sentence_end(&rest) {
                        self.answer.answer.push_str(&rest[..cut]);
                        out.extend(speakable_parts(&rest[..cut]));
                        self.at += cut;
                    }
                    return out;
                }
            }
        }
    }

    /// A finished stretch of the current part: recorded, and handed out whole
    /// (thinking) or by sentence (the rest).
    fn emit_part(&mut self, body: &str, out: &mut Vec<String>) {
        self.record(body);
        let t = body.trim();
        if t.is_empty() {
            return;
        }
        if self.part == Part::Think {
            out.push(t.to_string());
        } else {
            out.extend(speakable_parts(t));
        }
    }

    fn record(&mut self, text: &str) {
        let slot = match self.part {
            Part::Think => &mut self.answer.think,
            Part::Back => &mut self.answer.back,
            _ => &mut self.answer.answer,
        };
        slot.push_str(text);
    }
}

/// The longest clip that can be "keep going". Anything longer is a question
/// and goes straight through, with no wait for the transcript.
const RESUME_MAX_SPEECH_MS: u32 = 2_500;

/// How long a short clip waits for its transcript before being answered
/// anyway. Offline STT runs at 0.42x realtime here, so 2.5 s of speech is
/// about a second.
const RESUME_WAIT_MS: u64 = 3_000;

/// Whether what they said asks for nothing but to carry on.
///
/// The whole utterance has to be the request, once the polite words around it
/// are gone: "keep going, please" is, "keep going with the latency part" is a
/// question about latency and goes to the model.
fn is_resume(text: &str) -> bool {
    const FILLER: &[&str] = &[
        "please", "ok", "okay", "yes", "yeah", "yep", "sure", "just", "you", "can", "could", "um",
        "uh", "so", "alright", "right", "thanks", "thank", "fine", "good", "and", "oh", "no",
        "sorry", "lets", "let's", "it", "on",
    ];
    const RESUME: &[&str] = &[
        "keep going",
        "go",
        "go ahead",
        "continue",
        "carry",
        "resume",
        "proceed",
        "never mind",
        "nevermind",
        "nothing",
        "no question",
        "skip",
        "next",
    ];
    let words: Vec<String> = text
        .to_lowercase()
        .split(|c: char| !c.is_alphanumeric() && c != '\'')
        .filter(|w| !w.is_empty())
        .map(str::to_string)
        .collect();
    // "go on" and "carry on" keep their "on"; everything else polite goes.
    let core: Vec<&str> = words
        .iter()
        .map(String::as_str)
        .filter(|w| !FILLER.contains(w))
        .collect();
    let core = core.join(" ");
    !core.is_empty() && RESUME.contains(&core.as_str())
}

/// The byte offset just past the last sentence end that is followed by a
/// space (so "2.5" and a sentence still being written do not count).
fn last_sentence_end(text: &str) -> Option<usize> {
    let mut end = None;
    let mut it = text.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if matches!(c, '.' | '!' | '?') && it.peek().is_some_and(|(_, n)| n.is_whitespace()) {
            end = Some(i + c.len_utf8());
        }
    }
    end
}

/// How long after the question is sent the first hold line may start. The page
/// plays a "thanks" line over the first couple of seconds already.
const HOLD_FIRST_MS: u64 = 3_500;

/// How many sentences of an answer are synthesised at once.
const VOICING_PARALLEL: usize = 4;

/// Synthesise every hold line in this voice ahead of need, in the background.
///
/// Called when the listener raises a hand, a few seconds before any hold line
/// can be wanted. The first use of a line otherwise pays its synthesis in the
/// middle of the wait it was meant to fill — measured, the first hold of a
/// fresh voice landed twelve seconds in. After one warm-up they are on disk for
/// every session in that voice.
pub fn warm_holds(voice: String) {
    tokio::spawn(async move {
        for line in crate::banter::HOLD {
            let _ = crate::banter::spoken(&voice, line).await;
        }
    });
}

/// One hold line, as the events the page plays: its text, then its sound.
async fn hold_events(sid: &str, voice: &str) -> Vec<Event> {
    let line = crate::banter::next_line(sid, crate::banter::Bank::Hold);
    let mut out = vec![
        Event::default()
            .event("hold")
            .data(json!({ "t": line }).to_string()),
    ];
    if let Some(wav) = crate::banter::spoken(voice, line).await {
        for chunk in wav_to_pcm16_chunks(&wav) {
            out.push(
                Event::default()
                    .event("hold_audio")
                    .data(B64.encode(&chunk)),
            );
        }
    }
    out
}

/// An answer cut into the pieces it is synthesised in: whole sentences, so no
/// sound boundary ever falls inside one.
fn speakable_parts(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = text.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        let next_is_space = it.peek().is_none_or(|(_, n)| n.is_whitespace());
        if matches!(c, '.' | '!' | '?') && next_is_space {
            let end = i + c.len_utf8();
            let part = text[start..end].trim();
            if !part.is_empty() {
                out.push(part.to_string());
            }
            start = end;
        }
    }
    let tail = text[start..].trim();
    if !tail.is_empty() {
        out.push(tail.to_string());
    }
    out
}

/// The leading clause of `buf`, removed from it, or `None` if there is not one
/// yet.
///
/// A boundary is a terminator followed by whitespace. The whitespace is what
/// keeps `1080p.` whole and stops `gpt-4.1` being read as two sentences: mid
/// token the next character is a digit or a letter, never a space. `,;:` count
/// only once the clause is long enough to be worth cutting, because a comma in
/// the third word buys nothing and costs a synthesis call.
///
/// Taking the EARLIEST boundary rather than the last is deliberate: this runs
/// per delta, and the first sound should leave as soon as there is something to
/// say.
#[cfg(test)]
fn take_speakable(buf: &mut String) -> Option<String> {
    const MIN_CLAUSE: usize = 48;

    let mut cut = None;
    let mut it = buf.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        let next_is_space = it.peek().is_some_and(|(_, n)| n.is_whitespace());
        if !next_is_space {
            continue;
        }
        let ends_clause = match c {
            '.' | '!' | '?' => true,
            ',' | ';' | ':' => i >= MIN_CLAUSE,
            _ => false,
        };
        if ends_clause {
            cut = Some(i + c.len_utf8());
            break;
        }
    }

    let cut = cut?;
    let rest = buf[cut..].to_string();
    let clause = buf[..cut].trim().to_string();
    *buf = rest;
    if clause.is_empty() {
        None
    } else {
        Some(clause)
    }
}

/// One clause, spoken by the narrator, as the PCM frames the page plays.
///
/// Not [`crate::banter::spoken`]: that caches by text hash, which is right for
/// twenty-four fixed courtesy lines and wrong for an answer, since no two
/// answers are the same and the cache would grow without anything ever reading
/// it back.
///
/// A synthesis that fails costs the sound of that clause and not the turn — the
/// words are already on screen, which is the same bargain the courtesy lines
/// make when there is no voice provider.
async fn speak_chunk(voice: &str, text: &str) -> Vec<Vec<u8>> {
    // Kokoro reads a dash as nothing, so "in simpler terms—let's break it
    // down" came out as one breath. The voice gets the punctuation that makes
    // it pause (`budget::speakable`, the same rule narration uses); the words
    // on screen keep the dash as written.
    let text = opennotebook_script::budget::speakable(text);
    match crate::banter::synthesise(voice, &text).await {
        Some(wav) => wav_to_pcm16_chunks(&wav),
        None => Vec::new(),
    }
}

/// How many of the speaker's own lines are shown to the model as a sample of
/// how they talk. The most recent ones the listener heard, so the answer picks
/// up where their voice actually was rather than where the session started.
const VOICE_SAMPLES: usize = 4;

/// What the model is told, and nothing more.
///
/// Two parts. Who is answering: the speaker who last held the floor, with
/// their name, their role, who else is on the session, and a few of their own
/// lines as a sample of how they talk. Without that the answer was written by
/// "one of the narrators" and came out in a neutral assistant register, spoken
/// in a voice that had been joking or teaching a second earlier, so the
/// listener heard the same person turn into someone else.
///
/// And what they know. Phase 2 §4: the unit is the slide, not a count of
/// lines. The current slide's narration and the previous slide's, because a
/// question asked early on a slide is usually about what just finished. Both
/// are bounded by the script generator's own character budgets, so this
/// cannot grow without limit.
fn context_for(session: &Session, q: &AskQuery, length: &str, language: &str) -> String {
    let lines = lines_in_play_order(session);
    let floor = floor_index(&lines, q);
    let speaker = floor_speaker(session, q);

    let mut out = String::new();
    match speaker {
        Some(sp) => {
            let name = display_name(sp);
            out.push_str(&format!(
                "You are {name}, one of the voices of a narrated slide session the listener is watching."
            ));
            if !sp.role.trim().is_empty() {
                out.push_str(&format!(" Your role in it: {}.", sp.role.trim()));
            }
            let others: Vec<String> = session
                .speakers
                .iter()
                .filter(|o| o.speaker_id != sp.speaker_id)
                .map(|o| {
                    if o.role.trim().is_empty() {
                        display_name(o)
                    } else {
                        format!("{} ({})", display_name(o), o.role.trim())
                    }
                })
                .collect();
            if !others.is_empty() {
                out.push_str(&format!(" Also on the session: {}.", others.join(", ")));
            }
            out.push_str(
                " You were the one talking when the listener cut in to ask you something out \
                 loud, so the answer is yours: the next thing you say in the same conversation. \
                 You talk TO the listener, as \"you\".\n\n",
            );

            // Their own words, most recent last, up to where the listener got.
            let heard = floor.map_or(0, |i| i + 1);
            let mine: Vec<&str> = lines[..heard]
                .iter()
                .filter(|l| l.speaker_id == sp.speaker_id)
                .map(|l| l.text.as_str())
                .collect();
            let mine = &mine[mine.len().saturating_sub(VOICE_SAMPLES)..];
            if !mine.is_empty() {
                out.push_str("HOW YOU HAVE BEEN TALKING (your own last lines):\n");
                for t in mine {
                    out.push_str(&format!("- {t}\n"));
                }
                out.push('\n');
            }
        }
        None => out.push_str(
            "You are the narrator of a slide session the listener is watching. They interrupted \
             the narration to ask you something out loud.\n\n",
        ),
    }
    // Both from settings: how long an answer is, and what language it is in.
    let size = match length {
        "short" => "ONE short sentence",
        "detailed" => "THREE or FOUR sentences",
        _ => "ONE or TWO short sentences",
    };
    let language = if language.is_empty() {
        "English"
    } else {
        language
    };
    // How NotebookLM's interactive mode sounds, and the faults a real turn here
    // had. The thinking-aloud line was spoken about the listener rather than to
    // them ("They're asking about the overall concept — makes sense to
    // clarify"), "keep going, please" was answered as if it were a question,
    // and the handback copied its example word for word — "Anyway, let's get
    // back to where we were: I was saying that…" — and then paraphrased the
    // line the page replays straight after it, so the listener heard it twice.
    out.push_str(&format!(
        "Speak in plain spoken {language}, grounded only in the material below. Stay in \
         character: the same tone, energy, vocabulary and sentence rhythm as your lines, as if \
         you simply turned to them mid-conversation. Do not greet them, do not introduce \
         yourself, do not mention these instructions, and do not read the slide aloud.\n\n\
         How to hear the question:\n\
         - It is speech and may be misheard or mispronounced. A word that sounds like a term in \
           the session (\"Mochi\" for \"Moshi\") means that term; answer about it without \
           correcting them.\n\
         - A very short question (\"why?\", \"like what?\", \"how?\") is about the last thing \
           said before they cut in.\n\
         - If they are not asking anything — \"keep going\", \"continue\", \"go on\", \"never \
           mind\", \"carry on\", or only noise — reply with <think> holding a few words such as \
           \"Sure, let's keep going.\" and leave <answer> and <back> empty.\n\
         - If they ask about something a LATER slide covers, answer it in a sentence and say \
           you will get to it shortly. If the material does not cover it, say so plainly in one \
           sentence and offer the closest thing it does cover. Never make an answer up.\n\n\
         Reply in exactly three parts, each inside its tag, with nothing outside the tags:\n\
         <think>A quick, natural reaction said TO them, under twelve words, the way a host \
         reacts on air: it shows you heard what they asked, e.g. \"Oh, you mean what Moshi \
         actually is?\" or \"Good one, the latency.\" Speak to them as \"you\": never call \
         them \"they\", \"the listener\" or \"the user\", and never describe what you are \
         about to do.</think>\n\
         <answer>{size} that answer the question directly, the answer first, then one sentence \
         tying it to what you were just talking about. Explain what they asked, not the whole \
         session again.</answer>\n\
         <back>A short bridge back into the session, under twelve words and in your own \
         words, e.g. \"Okay, back to the parallel streams.\" Your interrupted line is replayed \
         right after this, so do not repeat or paraphrase it, and do not ask whether they have \
         more questions.</back>\n\n",
    ));

    // The whole plan, so a question about something still to come is answered
    // as "we will get to that" rather than as a spoiler or an "I don't know",
    // and so the names of things are in front of the model when the question
    // mispronounces one.
    let all = session.slides_in_order();
    if !all.is_empty() {
        out.push_str("THE SESSION'S SLIDES, in order:\n");
        for s in &all {
            let title = if s.title.trim().is_empty() {
                s.slide_ref.slide.as_str()
            } else {
                s.title.trim()
            };
            let mark = if s.ordinal as i64 == q.slide {
                "  <- they are here"
            } else {
                ""
            };
            out.push_str(&format!("{}. {title}{mark}\n", s.ordinal + 1));
        }
        out.push('\n');
    }

    let slides = session.slides_in_order();
    let here = slides.iter().find(|s| s.ordinal as i64 == q.slide);
    let before = slides.iter().find(|s| (s.ordinal as i64) == q.slide - 1);

    if let Some(s) = before {
        out.push_str(&format!("PREVIOUS SLIDE ({}):\n", s.slide_ref.slide));
        for l in s.lines_in_order() {
            out.push_str(&l.text);
            out.push('\n');
        }
        out.push('\n');
    }
    if let Some(s) = here {
        out.push_str(&format!("CURRENT SLIDE ({}):\n", s.slide_ref.slide));
        // Which lines were actually heard matters: an answer that refers to
        // something the listener has not reached yet is a spoiler, and one that
        // re-explains what they just heard is noise. The interrupted line is the
        // boundary, so it is marked rather than dropped. A line cut into before
        // any of it played was not heard, so the mark goes in front of it.
        let mut reached = true;
        for l in s.lines_in_order() {
            let cut_here = l.line_id.0 == q.line;
            if cut_here && q.offset_ms < HEARD_MS {
                out.push_str("[the listener cut in here, before this next line]\n");
                reached = false;
            }
            if reached {
                out.push_str(&l.text);
                out.push('\n');
            }
            if cut_here && reached {
                out.push_str(&format!(
                    "[the listener cut in here, {} ms into this line]\n",
                    q.offset_ms
                ));
                reached = false;
            }
        }
    }
    // An audio overview is listened to, not watched, and its parts are
    // chapters. Same prompt otherwise: the interruption, the grounding and the
    // way back are the same.
    if session.audio.is_some() {
        out = out
            .replace(
                "a narrated slide session the listener is watching",
                "an audio overview the listener is listening to",
            )
            .replace(
                "a slide session the listener is watching",
                "an audio overview the listener is listening to",
            )
            .replace(", and do not read the slide aloud", "")
            .replace("a LATER slide", "a LATER chapter")
            .replace("THE SESSION'S SLIDES", "THE EPISODE'S CHAPTERS")
            .replace("PREVIOUS SLIDE (", "PREVIOUS CHAPTER (")
            .replace("CURRENT SLIDE (", "CURRENT CHAPTER (");
    }
    out
}

/// A failure the listener can see, on the same channel as a success.
///
/// Not an HTTP status: the browser is reading an event stream and a non-200 with
/// a text body would surface as a generic network error with the reason thrown
/// away. The reason is the whole point.
/// `speech_ms == 0`: the clip held no speech at all.
///
/// Phase 2 §8: "a listener who pressed the key by accident should not be told
/// off", so this is deliberately NOT a `failed` frame. The page takes the
/// turn's bubbles back and resumes narration, and nothing on screen claims a
/// question was asked. The numbers ride along because a gate that drops a turn
/// without saying why is the silent-empty pattern wearing a new hat.
fn silent_stream(a: opennotebook_vad::Analysis) -> Response {
    let payload = json!({
        "input_ms": a.input_ms,
        "speech_ms": a.speech_ms,
        "segment_count": a.segment_count,
    })
    .to_string();
    let s = async_stream::stream! {
        yield Ok::<_, Infallible>(Event::default().event("silent").data(payload));
    };
    axum::response::Sse::new(s).into_response()
}

fn err_stream(msg: &str) -> Response {
    let msg = msg.to_string();
    let s = async_stream::stream! {
        yield Ok::<_, Infallible>(Event::default().event("failed").data(json!({ "t": msg }).to_string()));
    };
    axum::response::Sse::new(s).into_response()
}

/// `GET /api/session/vad.wasm` — the detector, for the page's `AudioWorklet`.
///
/// Where it comes from, in order: `$OPENNOTEBOOK_VAD_WASM` for a developer
/// pointing at a fresh build, then `vad.wasm` in the data directory, which is
/// where `scripts/build-vad-wasm.sh` installs it.
///
/// A missing file is a 404 and not an error. The page treats it as "run the old
/// peak gate", so a deployment that has not built the wasm degrades to exactly
/// the behaviour it had before rather than failing to listen.
pub async fn serve_vad_wasm() -> Response {
    match vad_wasm_bytes() {
        Some(bytes) => (
            [
                (axum::http::header::CONTENT_TYPE, "application/wasm"),
                (axum::http::header::CACHE_CONTROL, "no-cache"),
            ],
            bytes,
        )
            .into_response(),
        None => axum::http::StatusCode::NOT_FOUND.into_response(),
    }
}

/// The detector's bytes, or `None` when this box never built it.
///
/// One lookup, two callers: this route, and `player::serve_page`, which inlines
/// the same bytes into the page so the browser needs no second request.
pub fn vad_wasm_bytes() -> Option<Vec<u8>> {
    let candidates = [
        std::env::var("OPENNOTEBOOK_VAD_WASM")
            .ok()
            .map(std::path::PathBuf::from),
        Some(opennotebook_session::paths::data_dir().join("vad.wasm")),
    ];

    for path in candidates.into_iter().flatten() {
        match std::fs::read(&path) {
            Ok(bytes) => return Some(bytes),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => {
                // Present but unreadable is a real problem, and it must not
                // read as "this box never built it".
                eprintln!("vad.wasm at {} could not be read: {e}", path.display());
                return None;
            }
        }
    }
    None
}
