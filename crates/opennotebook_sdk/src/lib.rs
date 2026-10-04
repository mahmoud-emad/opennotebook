//! SDK for OpenNotebook: the generated clients, and the tables the server and
//! the web app must read from one place.
//!
//! The clients are `opennotebook_api`'s, generated at build time from its
//! oschema/. The point of them is the phase 1 rule: use generated clients,
//! never hand-written JSON-RPC. The player page predates this crate and posts
//! JSON by hand because it is plain HTML with no build step; the Dioxus app has
//! a build step, so it has no excuse.
//!
//! One client per domain: `mindmap::MindMapServiceClient`,
//! `notes::NotesServiceClient`, `session::SessionServiceClient`,
//! `settings::SettingsServiceClient`, `sources::SourcesServiceClient`.

pub mod icons;
pub mod mindmap_layout;
pub mod styles;
pub mod theme;
pub mod voices;

/// The kinds of file a source can be, as a person is told when theirs is not
/// one. The page and the server's refusal say the same.
pub const UPLOAD_KINDS: &str = "PDF, Word, PowerPoint, Excel, Markdown, text or CSV";

pub use opennotebook_api::ClientError as OpenNotebookRpcApiError;

/// The five domains: types and a typed client each (`notes::NotesServiceClient`).
pub use opennotebook_api::{mindmap, notes, session, settings, sources};
