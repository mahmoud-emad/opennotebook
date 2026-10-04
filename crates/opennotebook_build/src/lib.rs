//! Section 3's second half: the script becomes slides and narration.
//!
//! The model writes each slide's HTML ([`slides`]), the style's kit takes the
//! look back from it ([`clean`], [`kits`]), the narration is synthesised at the
//! same time ([`narrate`]), and a line's duration is read from the WAV header
//! of the audio that will actually play ([`wav`]).

pub mod clean;
pub mod error;
pub mod job;
pub mod kits;
pub mod narrate;
pub mod prep;
pub mod slides;
pub mod validate;
pub mod wav;

pub use error::BuildError;
pub use prep::{PrepOutcome, build_session, phases};
