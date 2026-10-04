use thiserror::Error;

#[derive(Debug, Error)]
pub enum ScriptError {
    #[error("retrieval store call `{call}` failed: {source}")]
    Memory {
        call: &'static str,
        #[source]
        source: opennotebook_memory::MemoryError,
    },

    #[error("the model call for {stage} failed: {reason}")]
    Model { stage: &'static str, reason: String },

    /// The model stopped because it ran out of room, so the text is a fragment
    /// that reads like a whole answer. Caught by reading `finish_reason` rather
    /// than by inspecting the text, which cannot show this.
    #[error(
        "the model truncated its answer for {stage} (finish_reason `{finish_reason}`), \
         so the script would be a fragment"
    )]
    Truncated {
        stage: &'static str,
        finish_reason: String,
        /// What the model managed to say before the cut.
        ///
        /// Carried so a caller that can use a partial answer does not have to
        /// ask again for the same truncated reply. The outline ignores it and
        /// refuses; the slide script keeps it — see `complete_partial`.
        text: String,
    },

    /// Nothing was retrieved to ground on. Generating from the model's own
    /// knowledge is the failure this service exists to avoid.
    #[error("no grounding found in workspace `{workspace}` collection `{collection}`")]
    NoGrounding {
        workspace: String,
        collection: String,
    },

    #[error("the model returned no usable {stage}")]
    Empty { stage: &'static str },

    #[error("line {line}: `{speaker}` is not a speaker of this session")]
    UnknownSpeaker { line: usize, speaker: String },
}
