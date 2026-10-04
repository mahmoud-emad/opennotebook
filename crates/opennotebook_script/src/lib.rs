//! Section 3's script generation: an ingested collection becomes a
//! speaker-tagged narration script, grounded in the source.
//!
//! Deliberately not in this crate: the slides, synthesis, `duration_ms`,
//! render validation, the prep job wrapper. This fills the store; it produces
//! no media.
//!
//! One speaker and two speakers share one code path. There is no branch on
//! speaker count anywhere below: the speaker set is data, the turn assignment
//! is a rotation over it, and a one-speaker session rotates over one. That is
//! the claim section 8 rests on.

pub mod budget;
pub mod cite;
pub mod error;
pub mod generate;
pub mod grounding;
pub mod mindmap;
pub mod notes;
pub mod parse;

pub use error::ScriptError;
pub use generate::{DEFAULT_SLIDE_COUNT, ScriptSpec, generate_script};
pub use grounding::Grounding;
pub use parse::ScriptedLine;
