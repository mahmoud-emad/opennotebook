//! The one ingest path for a opennotebook session.
//!
//! Nothing else writes a session's retrieval store. See `docs/phase1-spec.md`
//! section 1.

pub mod error;
pub mod ingest;
pub mod proof;

pub use error::IngestError;
pub use ingest::{IngestResult, SourceFile, ingest_resources};
pub use proof::{RetrievalProof, prove_retrievable};
