use thiserror::Error;

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("database `{path}` could not be opened: {reason}")]
    Open { path: String, reason: String },

    #[error("database call `{call}` failed: {reason}")]
    Db { call: &'static str, reason: String },

    #[error("document `{key}`: could not be encoded: {source}")]
    Encode {
        key: String,
        #[source]
        source: serde_json::Error,
    },

    #[error("document `{key}`: stored document did not decode: {source}")]
    Decode {
        key: String,
        #[source]
        source: serde_json::Error,
    },
}
