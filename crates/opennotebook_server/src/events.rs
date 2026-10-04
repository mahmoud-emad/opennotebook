//! The SSE event stream, and the change-source decision behind it.
//!
//! # There is no single change source, because there are two kinds of state
//!
//! The previous slice dropped `sse` from the manifest rather than declare a
//! protocol nothing served, and left the change source as an open decision. It
//! turns out not to have one answer, and that is why it looked hard:
//!
//! **Prep state is written by another process.** `preparing -> ready | failed`,
//! and the phase a prep is on, are written by the prep child process. This
//! process cannot observe that except by asking, so those events are
//! **polled** — the session row plus the prep job row, both in SQLite.
//!
//! **Playback state is written by this process.** `playing`, `paused`,
//! `finished`, the slide and line boundaries and the playhead are all created
//! here, by the browser calling this service. Polling our own memory would be
//! absurd, so those events are **pushed** the moment they are made.
//!
//! So the stream is a merge: a poll loop for what another process owns, and a
//! direct read of the playhead for what this one owns. Neither half is a
//! fallback for the other and neither could replace the other.
//!
//! **Flagged as a decision the spec did not make.** §4 lists the events and says
//! "over SSE"; it never says how the studio learns of a change. The poll
//! interval below is a guess constrained by measurement rather than a
//! requirement.
//!
//! # Why polling is honest here rather than merely easy
//!
//! The polled half is cheap *because of what it is polling for*. A prep phase is
//! 20 to 37 seconds of rendering or 4.4 to 5.7 seconds of synthesis per line, so
//! a one-second poll can only ever be a few percent of a phase late — well below
//! the resolution a progress bar can show. Two reads of a local SQLite file per
//! second per watching browser is not a cost worth a design to avoid.
//!
//! The alternative was considered and is worse *today*: a notification channel
//! where the prep child publishes and the server subscribes is the right shape
//! for many listeners and strictly more moving parts for one. It becomes
//! correct when a session has more than one watcher.
//!
//! # Terminal states stop the poll
//!
//! Once a session is `ready` or `failed` the polled half has nothing left to
//! learn, so it stops and the stream continues on playback events alone. A poll
//! that ran forever against a finished session would be the cost with none of
//! the reason.

use std::convert::Infallible;
use std::time::Duration;

use axum::response::sse::{Event, KeepAlive};
use serde::Serialize;

use crate::playback::Playhead;

/// How often the polled half reads the session and job rows for a change.
///
/// One second. See the module note: a prep phase is tens of seconds, so this
/// cannot be visibly late, and it is two local database reads.
pub const POLL: Duration = Duration::from_secs(1);

/// How often a comment frame goes out when nothing has changed.
///
/// Proxies and load balancers close a silent connection; the gateway in front of
/// this box is one. 15 s is comfortably inside the usual 30–60 s idle timeouts.
pub const KEEPALIVE: Duration = Duration::from_secs(15);

/// §4's event vocabulary, plus one addition that is called out below.
///
/// The names are the spec's, verbatim, because a player written against §4
/// should not have to translate.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "event", content = "data")]
pub enum StudioEvent {
    /// `preparing | ready | failed | playing | paused | finished`
    ///
    /// §4 lists `preparing | ready | playing | paused | finished`. `failed` is
    /// added because §2's `SessionState` has it and a prep screen that cannot
    /// say "this failed" would spin forever on a session that is never coming.
    #[serde(rename = "session.state")]
    SessionState { state: String },

    #[serde(rename = "slide.enter")]
    SlideEnter {
        slide_ordinal: i64,
        collection: String,
        presentation: String,
        slide: String,
    },

    #[serde(rename = "line.start")]
    LineStart {
        line_id: String,
        /// §4: carried "so the UI can show who is talking with one speaker or
        /// two". The phase 2 seam that costs nothing now.
        speaker_id: String,
        duration_ms: i64,
        audio_url: String,
    },

    #[serde(rename = "line.end")]
    LineEnd { line_id: String },

    #[serde(rename = "playhead")]
    Playhead {
        slide_ordinal: i64,
        line_id: String,
        offset_ms: i64,
    },

    /// **Not in §4's list.** Flagged rather than slipped in.
    ///
    /// §3 requires progress to "be honest about which stage is running", and
    /// §4's five events carry no way to say it: `session.state` is `preparing`
    /// for the entire multi-minute prep. A prep screen without this can draw a
    /// spinner and nothing else, which is exactly the `steps_total: 0` state
    /// a job row uses for "does not report".
    ///
    /// It is a strict addition — no existing event changed — so a player written
    /// against §4 alone still works and simply shows less.
    #[serde(rename = "prep.progress")]
    PrepProgress {
        step: String,
        steps_done: i64,
        steps_total: i64,
    },
}

impl StudioEvent {
    /// The SSE frame. The event name is the `event:` field so a browser can use
    /// `addEventListener("line.start", …)` rather than parsing a discriminator
    /// out of the payload.
    pub fn frame(&self) -> Result<Event, Infallible> {
        let name = match self {
            StudioEvent::SessionState { .. } => "session.state",
            StudioEvent::SlideEnter { .. } => "slide.enter",
            StudioEvent::LineStart { .. } => "line.start",
            StudioEvent::LineEnd { .. } => "line.end",
            StudioEvent::Playhead { .. } => "playhead",
            StudioEvent::PrepProgress { .. } => "prep.progress",
        };
        // `data` carries only the payload; the name is already the event field.
        let data = match serde_json::to_value(self) {
            Ok(serde_json::Value::Object(mut m)) => m
                .remove("data")
                .unwrap_or(serde_json::Value::Object(Default::default())),
            _ => serde_json::Value::Object(Default::default()),
        };
        Ok(Event::default().event(name).data(data.to_string()))
    }
}

/// The playback half of the stream, derived from the playhead this process owns.
///
/// Returns the events that describe a transition from `was` to `now`, in the
/// order a player should see them. Pure, so the ordering is testable without a
/// browser.
pub fn playback_events(
    was: &Playhead,
    now: &Playhead,
    session: Option<&SlideLookup>,
) -> Vec<StudioEvent> {
    let mut out = Vec::new();

    if was.state != now.state {
        out.push(StudioEvent::SessionState {
            state: now.state.as_str().to_string(),
        });
    }

    // Order matters and is part of what a player can rely on:
    //
    //   line.end  ->  slide.enter  ->  line.start
    //
    // End the line that was speaking before changing what is on screen, then
    // enter the new slide, then start the line spoken over it. The other order
    // was tried and observed live: `slide.enter` arrived first, so the new slide
    // was on screen for one frame with the previous slide's line still
    // highlighted.
    let line_changed = was.line_id != now.line_id;

    if line_changed && !was.line_id.is_empty() {
        out.push(StudioEvent::LineEnd {
            line_id: was.line_id.clone(),
        });
    }

    if (was.slide_ordinal != now.slide_ordinal
        || (was.line_id.is_empty() && !now.line_id.is_empty()))
        && let Some(s) = session.and_then(|l| l.slide(now.slide_ordinal))
    {
        out.push(StudioEvent::SlideEnter {
            slide_ordinal: now.slide_ordinal,
            collection: s.0,
            presentation: s.1,
            slide: s.2,
        });
    }

    if line_changed
        && !now.line_id.is_empty()
        && let Some((speaker, dur, url)) = session.and_then(|l| l.line(&now.line_id))
    {
        out.push(StudioEvent::LineStart {
            line_id: now.line_id.clone(),
            speaker_id: speaker,
            duration_ms: dur,
            audio_url: url,
        });
    }

    if was.offset_ms != now.offset_ms || was.line_id != now.line_id {
        out.push(StudioEvent::Playhead {
            slide_ordinal: now.slide_ordinal,
            line_id: now.line_id.clone(),
            offset_ms: now.offset_ms,
        });
    }

    out
}

/// What `playback_events` needs to look up about a session, kept as a trait
/// object's worth of closures so the event logic stays testable without a store.
pub struct SlideLookup {
    #[allow(clippy::type_complexity)]
    pub slide: Box<dyn Fn(i64) -> Option<(String, String, String)> + Send + Sync>,
    #[allow(clippy::type_complexity)]
    pub line: Box<dyn Fn(&str) -> Option<(String, i64, String)> + Send + Sync>,
}

impl SlideLookup {
    fn slide(&self, ordinal: i64) -> Option<(String, String, String)> {
        (self.slide)(ordinal)
    }
    fn line(&self, id: &str) -> Option<(String, i64, String)> {
        (self.line)(id)
    }
}

/// The keepalive the gateway in front of this box needs.
///
/// A silent SSE connection is closed by proxies; the interval is comfortably
/// inside the usual 30-60 s idle timeouts. Applied by the caller so the stream
/// type stays the caller's.
pub fn keepalive() -> KeepAlive {
    KeepAlive::new()
        .interval(KEEPALIVE)
        .text("opennotebook keepalive")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::playback::PlayState;

    fn lookup() -> SlideLookup {
        SlideLookup {
            slide: Box::new(|o| Some(("c".into(), "p".into(), format!("slide{o}")))),
            line: Box::new(|id| Some(("s1".into(), 1234, format!("/audio?line={id}")))),
        }
    }

    fn head(state: PlayState, slide: i64, line: &str, offset: i64) -> Playhead {
        Playhead {
            slide_ordinal: slide,
            line_id: line.to_string(),
            offset_ms: offset,
            state,
        }
    }

    #[test]
    fn starting_playback_enters_a_slide_then_starts_a_line() {
        let was = head(PlayState::Idle, 0, "", 0);
        let now = head(PlayState::Playing, 0, "s0l0", 0);
        let ev = playback_events(&was, &now, Some(&lookup()));
        let names: Vec<_> = ev
            .iter()
            .map(|e| match e {
                StudioEvent::SessionState { .. } => "session.state",
                StudioEvent::SlideEnter { .. } => "slide.enter",
                StudioEvent::LineStart { .. } => "line.start",
                StudioEvent::LineEnd { .. } => "line.end",
                StudioEvent::Playhead { .. } => "playhead",
                StudioEvent::PrepProgress { .. } => "prep.progress",
            })
            .collect();
        // Order matters to a player: it must know which slide before it is told
        // to play a line over it.
        assert_eq!(
            names,
            ["session.state", "slide.enter", "line.start", "playhead"]
        );
    }

    #[test]
    fn a_line_boundary_ends_the_old_line_before_starting_the_new_one() {
        let was = head(PlayState::Playing, 0, "s0l0", 3000);
        let now = head(PlayState::Playing, 0, "s0l1", 0);
        let ev = playback_events(&was, &now, Some(&lookup()));
        let end_at = ev
            .iter()
            .position(|e| matches!(e, StudioEvent::LineEnd { .. }));
        let start_at = ev
            .iter()
            .position(|e| matches!(e, StudioEvent::LineStart { .. }));
        assert!(end_at.is_some() && start_at.is_some());
        assert!(
            end_at < start_at,
            "line.end must precede the next line.start"
        );
        // No slide change, so no slide.enter.
        assert!(
            !ev.iter()
                .any(|e| matches!(e, StudioEvent::SlideEnter { .. }))
        );
    }

    #[test]
    fn crossing_a_slide_boundary_ends_the_line_before_entering_the_slide() {
        // Observed live before it was fixed: `slide.enter` arrived first, so the
        // new slide was painted while the previous slide's line was still the
        // highlighted one. The contract is line.end -> slide.enter -> line.start.
        let ev = playback_events(
            &head(PlayState::Playing, 0, "s0l2", 4000),
            &head(PlayState::Playing, 1, "s1l0", 0),
            Some(&lookup()),
        );
        let pos = |f: fn(&StudioEvent) -> bool| ev.iter().position(f);
        let end = pos(|e| matches!(e, StudioEvent::LineEnd { .. })).expect("line.end");
        let enter = pos(|e| matches!(e, StudioEvent::SlideEnter { .. })).expect("slide.enter");
        let start = pos(|e| matches!(e, StudioEvent::LineStart { .. })).expect("line.start");
        assert!(end < enter, "line.end must precede slide.enter");
        assert!(enter < start, "slide.enter must precede line.start");
    }

    #[test]
    fn line_start_carries_the_speaker() {
        // §4's phase 2 seam. A UI cannot show who is talking without it, and
        // adding it later would change an event a player already parses.
        let ev = playback_events(
            &head(PlayState::Playing, 0, "a", 0),
            &head(PlayState::Playing, 0, "b", 0),
            Some(&lookup()),
        );
        let start = ev
            .iter()
            .find_map(|e| match e {
                StudioEvent::LineStart { speaker_id, .. } => Some(speaker_id.clone()),
                _ => None,
            })
            .expect("line.start present");
        assert_eq!(start, "s1");
    }

    #[test]
    fn a_pause_mid_line_emits_state_and_the_exact_offset() {
        // The §5 seam, as an event: the offset is whatever was reported, not a
        // boundary, and it reaches the page so a UI can show it.
        let was = head(PlayState::Playing, 1, "s1l0", 4000);
        let now = head(PlayState::Paused, 1, "s1l0", 4137);
        let ev = playback_events(&was, &now, Some(&lookup()));
        assert!(ev.iter().any(|e| matches!(
            e,
            StudioEvent::SessionState { state } if state == "paused"
        )));
        let off = ev
            .iter()
            .find_map(|e| match e {
                StudioEvent::Playhead { offset_ms, .. } => Some(*offset_ms),
                _ => None,
            })
            .expect("playhead present");
        assert_eq!(off, 4137, "the exact millisecond survives to the page");
    }

    #[test]
    fn joining_a_fresh_session_does_not_invent_a_line_that_ended() {
        // Regression. The stream used to open `last_head` at a sentinel
        // (`line_id: "\0never"`), and the very first frame a browser received
        // over the public domain was `line.end` for that sentinel. A `line.end`
        // for a line that never started is a lie a player will act on.
        let ev = playback_events(
            &Playhead::default(),
            &head(PlayState::Playing, 0, "s0l0", 0),
            Some(&lookup()),
        );
        assert!(
            !ev.iter().any(|e| matches!(e, StudioEvent::LineEnd { .. })),
            "no line.end when there was no previous line: {ev:?}"
        );
        // …and it still says everything a page joining mid-session needs.
        assert!(
            ev.iter()
                .any(|e| matches!(e, StudioEvent::SlideEnter { .. }))
        );
        assert!(
            ev.iter()
                .any(|e| matches!(e, StudioEvent::LineStart { .. }))
        );
    }

    #[test]
    fn nothing_changed_emits_nothing() {
        // A stream that repeats itself teaches a client to ignore it.
        let h = head(PlayState::Playing, 0, "s0l0", 500);
        assert!(playback_events(&h, &h, Some(&lookup())).is_empty());
    }

    #[test]
    fn the_frame_names_the_event_and_carries_only_the_payload() {
        let e = StudioEvent::LineEnd {
            line_id: "x".into(),
        };
        let f = format!("{:?}", e.frame().unwrap());
        // The browser keys on the event name, so it has to be the SSE field
        // rather than something to dig out of the JSON.
        assert!(f.contains("line.end"), "{f}");
    }
}
