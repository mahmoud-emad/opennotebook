use std::path::PathBuf;

use thiserror::Error;

/// Why an ingest, or the proof that follows it, did not succeed.
#[derive(Debug, Error)]
pub enum IngestError {
    #[error("retrieval store call `{call}` failed: {source}")]
    Memory {
        call: &'static str,
        #[source]
        source: opennotebook_memory::MemoryError,
    },

    #[error("{name}: `{extension}` is not a format this ingest can read verbatim")]
    UnsupportedFormat { name: String, extension: String },

    #[error("{name}: could not be parsed: {reason}")]
    Convert { name: String, reason: String },

    /// A document with no text layer, which in practice means a scan. Refused
    /// rather than sent to OCR: a transcription in the grounding store gets
    /// quoted back as if it were the document.
    #[error(
        "{name}: no extractable text ({bytes} bytes), so it is a scan or an image-only export. \
         Supply a copy with a text layer; this ingest will not OCR into the grounding store."
    )]
    NoExtractableText { name: String, bytes: usize },

    #[error("session directory {path}: {source}")]
    SessionDir {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    /// The search door answered, and answered with nothing. A search returns an
    /// empty hit list rather than an error when a collection holds nothing it
    /// can match, so this is the only place that failure becomes loud.
    #[error(
        "search door is empty: `{probe}` returned no hits in workspace `{workspace}` collection `{collection}`"
    )]
    SearchDoorEmpty {
        workspace: String,
        collection: String,
        probe: String,
    },

    /// The Q&A door answered, and answered with nothing.
    #[error(
        "q&a door is empty: no extracted pairs in workspace `{workspace}` collection `{collection}`"
    )]
    QaDoorEmpty {
        workspace: String,
        collection: String,
    },
}
