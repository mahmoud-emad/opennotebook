//! The lines the narrator says around a question, without spending a model call.
//!
//! # Why this exists
//!
//! Every question used to cost TWO calls to `gpt-audio-mini`: one to answer it,
//! and one before that to generate "I see your hand, go ahead" fresh at
//! temperature 1.1. The second one buys variety and nothing else, and variety
//! is the one thing a written list gives away for nothing.
//!
//! So the courtesy lines are written down here, spoken by the session's own
//! Kokoro voice, and cached on disk. The first time a voice says a line it
//! costs local synthesis, about a second at the 1.1x realtime this box runs.
//! Every time after that it is a file read. Across a session of ten questions
//! that is ten audio-model calls removed, and across every session after the
//! first it is the synthesis removed too.
//!
//! # Why a rotation and not a random pick
//!
//! Random repeats. With 24 lines and a random draw, the same line comes up
//! twice inside six questions about half the time, and a listener notices a
//! repeat far more than they notice a list. The counter walks the bank in order
//! from a per-session offset, so nothing repeats until the bank is exhausted.

use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{Mutex, OnceLock},
};

/// What the narrator says when the hand goes up, before the listener speaks.
///
/// Rules these follow, so an addition does not break the feel: under ten words,
/// no greeting, no name, no question mark that expects an answer other than the
/// listener's own, and nothing that assumes what the question is about.
pub const INVITE: &[&str] = &[
    "Go ahead, I'd love to hear your question.",
    "Yes, go ahead.",
    "Please, ask away.",
    "Sure, what would you like to know?",
    "Of course, go ahead.",
    "I see a hand. Go ahead.",
    "Happy to take that. Go ahead.",
    "Let's hear it.",
    "Go on, I'm listening.",
    "Please, go ahead.",
    "Yes? What's on your mind?",
    "Good, let's pause there. Go ahead.",
    "Absolutely, ask away.",
    "I'm listening.",
    "Go ahead, take your time.",
    "Sure thing, what is it?",
    "Right, let's hear your question.",
    "Yes, please go ahead.",
    "Fire away.",
    "Of course. What would you like to ask?",
    "Let's take that now. Go ahead.",
    "Please do, I'm listening.",
    "Good moment to stop. Go ahead.",
    "Yes, what would you like to know?",
];

/// What the narrator says once the question has been sent, while the answer is
/// still being worked out. It fills the second or two before the reply starts
/// and it acknowledges the person rather than the machine.
pub const THANKS: &[&str] = &[
    "Thanks, let me think about that.",
    "Good question, one moment.",
    "Thank you, let me take that.",
    "Right, let me answer that.",
    "Good one. Let me explain.",
    "Thanks for asking, here goes.",
    "That's worth answering properly.",
    "Let me give you a proper answer.",
    "Thanks, here's how I'd put it.",
    "Good, let me address that.",
    "Nice question. One second.",
    "Thank you, I'll take that now.",
    "Let me think that through.",
    "Good point, let me answer.",
    "Thanks, that's a fair question.",
    "Let me come back on that.",
    "Right, here's the answer.",
    "Thanks, let me unpack that.",
    "Good, I can speak to that.",
    "Let me take that one.",
];

/// What the narrator says while the answer is still being written and voiced.
///
/// An answer used to be spoken clause by clause as the model wrote it, and every
/// time the writing or the synthesis fell behind the speaking the listener heard
/// the narrator stop mid-thought. Now the whole answer is prepared first and
/// these fill the wait, the way a person says "let me see" while they think,
/// so the answer itself is spoken in one go.
pub const HOLD: &[&str] = &[
    "Let me look into that.",
    "One moment, I'm putting it together.",
    "Let me check what we covered.",
    "Bear with me a second.",
    "Let me put that simply.",
    "Almost there.",
    "Let me find the right way to say this.",
    "Just a moment.",
    "Let me pull that together for you.",
    "Nearly ready.",
];

/// What the narrator says when the listener cut in only to say "keep going".
/// Said instead of an answer, then the session picks up its line again.
pub const RESUME: &[&str] = &[
    "Sure, let's keep going.",
    "Okay, carrying on.",
    "Right, back to it.",
    "Sure thing, picking up again.",
    "Okay, on we go.",
    "Got it, let's keep going.",
];

/// Which bank a request wants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Bank {
    Invite,
    Thanks,
    Hold,
    Resume,
}

impl Bank {
    pub fn lines(self) -> &'static [&'static str] {
        match self {
            Bank::Invite => INVITE,
            Bank::Thanks => THANKS,
            Bank::Hold => HOLD,
            Bank::Resume => RESUME,
        }
    }

    fn tag(self) -> &'static str {
        match self {
            Bank::Invite => "invite",
            Bank::Thanks => "thanks",
            Bank::Hold => "hold",
            Bank::Resume => "resume",
        }
    }
}

/// Per session, per bank: how many lines have been used.
///
/// In memory beside the playhead, for the same reason the playhead is: losing
/// it on restart costs one possible repeat, which is not worth a write.
fn counters() -> &'static Mutex<HashMap<(String, &'static str), usize>> {
    static C: OnceLock<Mutex<HashMap<(String, &'static str), usize>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(HashMap::new()))
}

/// The next line for this session, walking the bank in order.
///
/// The starting offset is derived from the sid, so two sessions running at once
/// do not open with the same line, and one session never repeats until it has
/// been all the way round.
pub fn next_line(sid: &str, bank: Bank) -> &'static str {
    let lines = bank.lines();
    let seed = sid
        .bytes()
        .fold(0usize, |a, b| a.wrapping_mul(31).wrapping_add(b as usize));
    let mut c = counters().lock().unwrap();
    let n = c.entry((sid.to_string(), bank.tag())).or_insert(0);
    let line = lines[(seed.wrapping_add(*n)) % lines.len()];
    *n = n.wrapping_add(1);
    line
}

/// Where a spoken line is cached.
///
/// Keyed by voice and by the text's own hash, not by session: the same voice
/// saying the same words is the same audio, and every session after the first
/// gets it for free.
fn cache_path(voice: &str, text: &str) -> Option<PathBuf> {
    let h = text.bytes().fold(1469598103934665603u64, |a, b| {
        (a ^ b as u64).wrapping_mul(1099511628211)
    });
    // The voice id is a Kokoro name like `af_bella`, so it is already a safe
    // path segment; the hash is hex. Nothing here comes from a URL.
    let safe: String = voice
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_')
        .collect();
    Some(
        opennotebook_session::paths::data_dir()
            .join("banter")
            .join(safe)
            .join(format!("{h:016x}.wav")),
    )
}

/// The line's audio as a WAV, synthesised once and then read from disk.
///
/// Returns `None` when there is no speech server and no cached copy, which the
/// caller turns into "say it on screen and play nothing" rather than a failure:
/// a courtesy that cannot be spoken is not a reason to refuse the question.
pub async fn spoken(voice: &str, text: &str) -> Option<Vec<u8>> {
    let path = cache_path(voice, text)?;
    if let Ok(bytes) = std::fs::read(&path) {
        return Some(bytes);
    }

    let wav = synthesise(voice, text).await?;

    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    // A write that fails costs a re-synthesis next time and nothing else.
    let _ = std::fs::write(&path, &wav);
    Some(wav)
}

/// One line, straight from the speech server, no cache either side.
///
/// `pub(crate)` for [`crate::ask`], which speaks the ANSWER through it clause by
/// clause. That path must not go through [`spoken`]: the cache is keyed by the
/// text's hash, and an answer is never said twice.
pub(crate) async fn synthesise(voice: &str, text: &str) -> Option<Vec<u8>> {
    match opennotebook_speech::from_settings()
        .await
        .synthesize(text, voice)
        .await
    {
        Ok(wav) => Some(wav),
        Err(e) => {
            eprintln!("banter: {e}");
            None
        }
    }
}

/// Synthesise every line of both banks for `voices`, skipping what is cached.
///
/// Spawned when the player page is served, because the cost profile is wrong
/// otherwise: the rotation hands out a NEW line each time, so the first pass
/// through a bank pays local synthesis on every question, measured at 3 to 4
/// seconds against the roughly 1 second the model call it replaced took. A
/// courtesy that arrives late is worse than one that costs money.
///
/// Warming moves that cost off the listener's path entirely. It runs once per
/// voice for the life of the box, it is idempotent, and a failure is silent by
/// design: the fallback is synthesising on demand, which is what used to happen.
pub fn warm(voices: Vec<String>) {
    tokio::spawn(async move {
        for voice in voices {
            for bank in [Bank::Invite, Bank::Thanks, Bank::Resume] {
                for line in bank.lines() {
                    // `spoken` writes through the same cache the request path
                    // reads, so this is the request path with nobody waiting.
                    let _ = spoken(&voice, line).await;
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The whole point of a written bank: it has to be big enough that a
    /// listener does not hear the loop. Twenty is the floor the owner set.
    #[test]
    fn both_banks_are_big_enough_to_not_repeat() {
        assert!(INVITE.len() >= 20, "INVITE has only {}", INVITE.len());
        assert!(THANKS.len() >= 20, "THANKS has only {}", THANKS.len());
        assert!(
            INVITE.len() <= 50 && THANKS.len() <= 50,
            "banks are meant to stay small"
        );
    }

    /// A duplicate line is a repeat that the rotation cannot protect against.
    #[test]
    fn no_line_appears_twice_in_a_bank() {
        for bank in [Bank::Invite, Bank::Thanks] {
            let mut seen: Vec<&str> = bank.lines().to_vec();
            seen.sort_unstable();
            let before = seen.len();
            seen.dedup();
            assert_eq!(before, seen.len(), "{:?} has a duplicate line", bank.tag());
        }
    }

    /// Nothing repeats until the bank is exhausted. This is what a random pick
    /// cannot promise and why the counter exists.
    #[test]
    fn a_session_walks_the_whole_bank_before_repeating() {
        let sid = "walk-the-bank";
        let n = INVITE.len();
        let mut seen = Vec::new();
        for _ in 0..n {
            seen.push(next_line(sid, Bank::Invite));
        }
        let mut sorted = seen.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(
            sorted.len(),
            n,
            "a line repeated inside one pass of the bank"
        );
    }

    /// Two sessions starting at once must not open with the same line.
    #[test]
    fn two_sessions_do_not_open_identically() {
        let a = next_line("session-alpha", Bank::Thanks);
        let b = next_line("session-beta", Bank::Thanks);
        assert_ne!(a, b, "two sessions opened with the same line");
    }

    /// Every line has to survive being spoken: no markup, no newline, and short
    /// enough that the courtesy does not become a speech.
    #[test]
    fn every_line_is_speakable() {
        for bank in [Bank::Invite, Bank::Thanks] {
            for l in bank.lines() {
                assert!(!l.is_empty(), "empty line in {:?}", bank.tag());
                assert!(!l.contains('\n'), "line has a newline: {l:?}");
                assert!(
                    !l.contains('<') && !l.contains('"'),
                    "line has markup: {l:?}"
                );
                assert!(
                    l.split_whitespace().count() <= 10,
                    "line is over ten words: {l:?}"
                );
            }
        }
    }
}
