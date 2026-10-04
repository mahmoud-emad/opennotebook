use thiserror::Error;

/// Why a session did not get built. Every variant names the call or the slide
/// it came from, because a prep that fails halfway is diagnosed from the stored
/// session and this string.
#[derive(Debug, Error)]
pub enum BuildError {
    /// Progress was written for a job row that does not exist. The prep was
    /// started without [`crate::job::submit`], or its row was removed, and the
    /// progress it reports would reach nobody.
    #[error("prep job `{job}` has no row to report progress to")]
    NoJob { job: String },

    #[error("speech call `{call}` failed: {source}")]
    Voice {
        call: &'static str,
        #[source]
        source: opennotebook_speech::SpeechError,
    },

    #[error("the session store refused the write: {source}")]
    Store {
        #[source]
        source: opennotebook_session::StoreError,
    },

    #[error("`{name}`: not a WAV: {why}")]
    NotWav { name: String, why: String },

    #[error("line `{line}`: synthesis returned no audio")]
    EmptyAudio { line: String },

    #[error("audio for line `{line}` at {path}: {source}")]
    AudioWrite {
        line: String,
        path: String,
        #[source]
        source: std::io::Error,
    },

    /// A line naming a speaker the session does not declare has no voice to be
    /// spoken in. `Session::dangling_speaker_ids` finds these; this is what
    /// happens when synthesis meets one anyway.
    #[error("could not write the slide file {path}: {source}")]
    SlideWrite {
        path: String,
        #[source]
        source: std::io::Error,
    },

    #[error("slide `{slide}` was not written: {path} is missing or incomplete")]
    SlideMissing { slide: String, path: String },

    #[error("line `{line}` names speaker `{speaker}`, which the session does not declare")]
    UnknownSpeaker { line: String, speaker: String },

    /// Nobody wants this session any more: what it was being made for was
    /// deleted while it was being made. Not a failure of the build, and the
    /// caller treats it as a clean stop, because a `Failed` row written now
    /// would bring the deleted thing back to every list that reads rows.
    #[error("session `{sid}` was abandoned while it was being prepared; nothing was written")]
    Abandoned { sid: String },
}
