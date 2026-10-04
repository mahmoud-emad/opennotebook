use thiserror::Error;

#[derive(Debug, Error)]
pub enum MemoryError {
    #[error(transparent)]
    Store(#[from] opennotebook_session::StoreError),

    #[error("embedding failed: {0}")]
    Embed(String),

    #[error("Q&A extraction from `{doc}` on `{dimension}` failed: {reason}")]
    Extract {
        doc: String,
        dimension: String,
        reason: String,
    },
}
