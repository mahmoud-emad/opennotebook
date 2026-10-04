//! Render validation, which is a liveness check and only that.
//!
//! It answers "is there something to look at", nothing more. The slide that
//! motivated the whole `ok: true` trap returned 131,541 characters of
//! well-formed HTML in which the title overflowed its box, the accent rule
//! struck through a word, and the literal placeholder `SUBHEAD` was still
//! there. A non-empty check passes that slide, and would pass it every time.
//! Quality rests on the character budgets applied when the script is generated.
//!
//! Reading the render is also where the aspect comes from, so both happen in
//! one call per slide rather than two.

use std::path::Path;

use opennotebook_session::Session;

use crate::error::BuildError;
use crate::slides;

/// Every slide was written to disk as a complete HTML document.
///
/// The writer never leaves a slide out — one the model did not deliver is drawn
/// plain — so this guards the one thing that could still go wrong after it: a
/// file that is missing or cut short, which the player would show as a blank.
pub fn slides_are_written(session: &Session, deck_dir: &Path) -> Result<(), BuildError> {
    for slide in session.slides_in_order() {
        let path = slides::slide_path(deck_dir, &slide.slide_ref.slide);
        let html = std::fs::read_to_string(&path).unwrap_or_default();
        if !html.to_ascii_lowercase().contains("</html>") {
            return Err(BuildError::SlideMissing {
                slide: slide.slide_ref.slide.clone(),
                path: path.to_string_lossy().into_owned(),
            });
        }
    }
    Ok(())
}

/// Every line has audio, and that audio lasts a non-zero time.
///
/// A zero duration means synthesis returned a header and no samples, which
/// plays as nothing and would otherwise reach the player as a slide that
/// advances instantly.
pub fn narration_is_playable(session: &Session) -> Result<(), BuildError> {
    for slide in session.slides_in_order() {
        for line in slide.lines_in_order() {
            match (&line.audio_path, line.duration_ms) {
                (Some(_), Some(d)) if d.millis() > 0 => {}
                _ => {
                    return Err(BuildError::EmptyAudio {
                        line: line.line_id.0.clone(),
                    });
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use opennotebook_session::{
        Aspect, DurationMs, LineId, NarrationLine, SessionSlide, SessionState, SlideRef, Speaker,
        SpeakerId,
    };

    use super::*;

    fn session(audio: Option<&str>, millis: u64) -> Session {
        Session {
            sid: "s".into(),
            title: "t".into(),
            collection_name: "c".into(),
            collection_sid: None,
            deck_ref: None,
            speakers: vec![Speaker {
                speaker_id: SpeakerId("host".into()),
                voice_id: "af_bella".into(),
                display_name: "Host".into(),
                role: "asks".into(),
            }],
            slides: vec![SessionSlide {
                slide_ref: SlideRef {
                    collection: "c".into(),
                    presentation: "p".into(),
                    slide: "one".into(),
                },
                title: String::new(),
                on_slide: Vec::new(),
                ordinal: 0,
                aspect: Aspect {
                    width: 1920,
                    height: 1080,
                },
                lines: vec![NarrationLine {
                    line_id: LineId("l0".into()),
                    speaker_id: SpeakerId("host".into()),
                    ordinal: 0,
                    text: "Spoken.".into(),
                    audio_path: audio.map(str::to_string),
                    duration_ms: Some(DurationMs::measured_from_wav(millis)),
                    cues: Vec::new(),
                }],
            }],
            state: SessionState::Preparing,
            prep_job_sid: None,
            failure: None,
            pinned: false,
            audio: None,
            collection: None,
            spent_usd: None,
            style: None,
        }
    }

    #[test]
    fn a_line_with_audio_and_a_real_duration_passes() {
        assert!(narration_is_playable(&session(Some("/tmp/l0.wav"), 1200)).is_ok());
    }

    /// Both halves of the check fail on their own, which is the point: a path
    /// with no duration and a duration of zero are different bugs.
    #[test]
    fn a_missing_path_or_a_zero_duration_fails() {
        assert!(matches!(
            narration_is_playable(&session(None, 1200)),
            Err(BuildError::EmptyAudio { .. })
        ));
        assert!(matches!(
            narration_is_playable(&session(Some("/tmp/l0.wav"), 0)),
            Err(BuildError::EmptyAudio { .. })
        ));
    }
}
