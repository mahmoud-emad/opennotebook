//! The playhead, held by the studio.
//!
//! §4: "The studio holds the playhead. It plays `NarrationLine` audio in order,
//! advances the iframe at slide boundaries, and emits events."
//!
//! Taken literally that is impossible — the audio element is in the browser and
//! nothing on this box can make a sound. What the studio holds is the
//! **authoritative position**: which slide, which line, how far into it, and
//! whether it is running. The browser is a rendering surface that obeys the
//! events and reports its clock back. So the split is:
//!
//! * the studio decides what plays next and emits `slide.enter` / `line.start` /
//!   `line.end`;
//! * the browser plays what it is told and posts its offset back;
//! * the studio records that offset and is the only thing that can answer
//!   "where is this session".
//!
//! The alternative — the browser owning the position and the studio only serving
//! bytes — is simpler and is what a media player normally does, but it fails
//! §5's first seam: a phase 2 question arrives at the *studio*, and a studio
//! that does not know the offset cannot resume from it or say what was being
//! said when the user cut in.
//!
//! # Pause at an arbitrary offset
//!
//! §5: "Make pause work at an arbitrary offset and record the offset. Note that
//! an arbitrary offset in milliseconds is all phase 2 needs to resume."
//!
//! So [`Playhead::offset_ms`] is a free millisecond value inside the current
//! line, not a line index, and pause records whatever the browser reports rather
//! than rounding to a boundary. This is the one piece of §5 built now, because
//! a player that can only stop between lines is a player phase 2 rewrites.
//!
//! # Why this is in-process and prep progress is not
//!
//! These are two different kinds of state and they need two different change
//! sources — see [`crate::events`].
//!
//! Playback state is *created* by this process: the browser calls this service,
//! this service mutates the playhead, this service emits the event. Nothing else
//! writes it, so an in-process broadcast is correct and a poll would be a poll of
//! our own memory.
//!
//! Session state during prep is the opposite: it is written by the prep child,
//! a different process, so this one can only find out by reading the database.

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use serde::{Deserialize, Serialize};

/// Where a session is, and whether it is moving.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Playhead {
    pub slide_ordinal: i64,
    /// Empty before the first line starts.
    pub line_id: String,
    /// Milliseconds into `line_id`. Arbitrary: §5 needs to resume mid-line.
    pub offset_ms: i64,
    pub state: PlayState,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlayState {
    /// Never started, or reset.
    Idle,
    Playing,
    Paused,
    /// The last line of the last slide ended.
    Finished,
}

impl PlayState {
    pub fn as_str(self) -> &'static str {
        match self {
            PlayState::Idle => "idle",
            PlayState::Playing => "playing",
            PlayState::Paused => "paused",
            PlayState::Finished => "finished",
        }
    }
}

impl Default for Playhead {
    fn default() -> Self {
        Self {
            slide_ordinal: 0,
            line_id: String::new(),
            offset_ms: 0,
            state: PlayState::Idle,
        }
    }
}

/// Every session's playhead, for as long as this process lives.
///
/// **Flagged as an unspecified decision.** The spec never says whether a
/// playhead outlives a restart. In memory is the simplest defensible thing for
/// phase 1: §0 fixes the audience at "one listener, one browser page", a
/// playhead is worthless once that page is gone, and persisting it would put a
/// database write for every `timeupdate` the browser fires. If phase 2 wants a
/// session a user can walk away from and come back to, this map becomes a
/// `playhead` kind in the `docs` table and nothing else changes — the shape is
/// already one small document per session.
fn playheads() -> &'static Mutex<HashMap<String, Playhead>> {
    static HEADS: OnceLock<Mutex<HashMap<String, Playhead>>> = OnceLock::new();
    HEADS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// The current playhead, or a fresh `Idle` one.
pub fn get(sid: &str) -> Playhead {
    playheads()
        .lock()
        .expect("playhead lock")
        .get(sid)
        .cloned()
        .unwrap_or_default()
}

/// Replace a session's playhead and return what was stored.
pub fn put(sid: &str, head: Playhead) -> Playhead {
    playheads()
        .lock()
        .expect("playhead lock")
        .insert(sid.to_string(), head.clone());
    head
}

/// Forget a session's playhead entirely, as deleting the session does.
pub fn clear(sid: &str) {
    playheads().lock().expect("playhead lock").remove(sid);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_fresh_session_is_idle_at_the_start() {
        let h = get("never-seen");
        assert_eq!(h.state, PlayState::Idle);
        assert_eq!(h.offset_ms, 0);
        assert_eq!(h.slide_ordinal, 0);
        // Empty, not "the first line": the studio has not chosen one yet, and
        // guessing would make a caller believe playback had begun.
        assert!(h.line_id.is_empty());
    }

    #[test]
    fn an_offset_is_kept_exactly_not_rounded_to_a_line() {
        // §5's whole requirement in one assertion. A player that stores a line
        // index and calls it a position is a player phase 2 has to rewrite.
        let sid = "offset-test";
        put(
            sid,
            Playhead {
                slide_ordinal: 2,
                line_id: "s2l1".into(),
                offset_ms: 3_477,
                state: PlayState::Paused,
            },
        );
        let back = get(sid);
        assert_eq!(
            back.offset_ms, 3_477,
            "the exact millisecond, not a boundary"
        );
        assert_eq!(back.state, PlayState::Paused);
        assert_eq!(back.line_id, "s2l1");
        clear(sid);
        assert_eq!(get(sid).state, PlayState::Idle, "clear really forgets");
    }

    #[test]
    fn two_sessions_do_not_share_a_playhead() {
        put(
            "a",
            Playhead {
                slide_ordinal: 1,
                line_id: "x".into(),
                offset_ms: 10,
                state: PlayState::Playing,
            },
        );
        put(
            "b",
            Playhead {
                slide_ordinal: 5,
                line_id: "y".into(),
                offset_ms: 20,
                state: PlayState::Paused,
            },
        );
        assert_eq!(get("a").offset_ms, 10);
        assert_eq!(get("b").offset_ms, 20);
        clear("a");
        clear("b");
    }
}
