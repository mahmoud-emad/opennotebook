//! Where OpenNotebook keeps everything on disk.
//!
//! One root, so a person can find, back up or delete all of it at once:
//! `$OPENNOTEBOOK_HOME` when set, otherwise the platform's data directory
//! (`~/.local/share/opennotebook` on Linux, `~/Library/Application
//! Support/opennotebook` on macOS).
//!
//! ```text
//! <data_dir>/
//!   opennotebook.db     sessions, collections, jobs
//!   settings.toml       operator settings, mode 0600
//!   staging/<cid>/      a collection's sources, one Markdown file each
//!   sessions/ audio/ decks/
//!   mindmaps/<cid>/ notes/<cid>/
//!   prep/               the spec file a prep child is started with
//!   banter/             cached spoken interjections
//!   vad.wasm            the voice detector, from scripts/build-vad-wasm.sh
//! ```

use std::path::PathBuf;

/// Overrides the data directory.
pub const HOME_ENV: &str = "OPENNOTEBOOK_HOME";

/// The root everything else is under.
pub fn data_dir() -> PathBuf {
    if let Some(home) = std::env::var_os(HOME_ENV).filter(|v| !v.is_empty()) {
        return PathBuf::from(home);
    }
    dirs::data_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("opennotebook")
}

/// The SQLite database.
pub fn db_file() -> PathBuf {
    data_dir().join("opennotebook.db")
}

/// The settings file.
pub fn settings_file() -> PathBuf {
    data_dir().join("settings.toml")
}
