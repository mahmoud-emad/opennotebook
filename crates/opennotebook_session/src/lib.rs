//! The narration store: the types a session is made of, and where they live.
//!
//! Nothing here produces a session. Section 3 fills it and section 4 reads it.

pub mod ai;
pub mod db;
pub mod error;
pub mod model;
pub mod paths;
pub mod store;

pub use db::Db;
pub use error::StoreError;
pub use model::{
    Aspect, AudioFormat, AudioLength, AudioSpec, Collection, CoverSpec, Cue, DeckRef, DurationMs,
    LineId, NarrationLine, Session, SessionSlide, SessionState, SlideRef, Speaker, SpeakerId,
};
pub mod settings;
pub mod spend;

pub use store::{CollectionStore, SessionStore};
