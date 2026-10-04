//! The proof that an ingest actually landed.
//!
//! A collection is two kinds of material: passages written by `index_add` and
//! Q&A pairs written by `qa_extract`. Both answer an unwritten collection with
//! an empty result rather than an error, so nothing below this module can tell
//! a working session from a broken one.
//!
//! The search half is a round trip on purpose. A row count only observes the
//! write path, while `search` embeds the query at read time when an embedder
//! is configured, so that endpoint is a live dependency of the read path that
//! no count can see.

use std::time::Duration;

use opennotebook_memory::Memory;

use crate::error::IngestError;

/// How often to re-probe while waiting for a dependency to come up.
const RETRY_INTERVAL: Duration = Duration::from_secs(1);

/// What the two doors returned when asked. Carried rather than discarded so a
/// caller can log the numbers that made the ingest pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetrievalProof {
    pub search_hits: usize,
    pub qa_pairs: usize,
}

/// Prove both kinds of material are served for this collection.
///
/// `probe` must be a phrase known to be in the ingested material; the caller
/// takes it from what it just wrote, never from a constant.
///
/// `wait` covers dependency startup, not emptiness. A transport or server error
/// is retried until `wait` is spent, because an embedding endpoint can be
/// briefly unavailable. An empty door is never retried: an
/// empty hit list is a complete answer meaning nothing is there to find, and
/// waiting cannot change it. Pass `Duration::ZERO` to fail on the first error.
pub async fn prove_retrievable(
    client: &Memory,
    workspace: &str,
    collection: &str,
    probe: &str,
    wait: Duration,
) -> Result<RetrievalProof, IngestError> {
    let deadline = std::time::Instant::now() + wait;
    loop {
        match probe_once(client, workspace, collection, probe).await {
            // An empty door is a verdict, not a hiccup: return it immediately.
            Err(e @ (IngestError::SearchDoorEmpty { .. } | IngestError::QaDoorEmpty { .. })) => {
                return Err(e);
            }
            // Any other failure may be a dependency still coming up. Retrying a
            // genuinely malformed call just fails later, which is acceptable
            // for a check that runs once per ingest.
            Err(e) if std::time::Instant::now() < deadline => {
                tracing_note(&e);
                tokio::time::sleep(RETRY_INTERVAL).await;
            }
            other => return other,
        }
    }
}

fn tracing_note(_e: &IngestError) {
    // Deliberately silent: the caller owns logging, and the error is returned
    // in full if the deadline passes.
}

async fn probe_once(
    memory: &Memory,
    workspace: &str,
    collection: &str,
    probe: &str,
) -> Result<RetrievalProof, IngestError> {
    let found = memory
        .search(workspace, collection, probe, 3)
        .await
        .map_err(|source| IngestError::Memory {
            call: "search",
            source,
        })?;

    if found.is_empty() {
        return Err(IngestError::SearchDoorEmpty {
            workspace: workspace.to_string(),
            collection: collection.to_string(),
            probe: probe.to_string(),
        });
    }

    let pairs = memory
        .qa_list(workspace, collection)
        .await
        .map_err(|source| IngestError::Memory {
            call: "qa_list",
            source,
        })?;

    if pairs.is_empty() {
        return Err(IngestError::QaDoorEmpty {
            workspace: workspace.to_string(),
            collection: collection.to_string(),
        });
    }

    Ok(RetrievalProof {
        search_hits: found.len(),
        qa_pairs: pairs.len(),
    })
}
