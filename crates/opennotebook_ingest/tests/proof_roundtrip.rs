//! The proof on its own, against collections that were never written.
//!
//! The populated case lives in `ingest.rs`, where the material is seeded by
//! `ingest_resources` rather than assumed to exist.

mod common;

use std::time::Duration;

use common::{WORKSPACE, client, session};
use opennotebook_ingest::{IngestError, prove_retrievable};

const PROBE: &str = "who is speaking and when to interrupt";

/// The failure this whole module exists for. A collection that was never
/// written answers with an empty list and no error, so the proof has to be what
/// turns that into a failure.
#[tokio::test]
async fn an_unwritten_collection_fails_the_proof() {
    let (sid, _) = session("unwritten");
    let err = prove_retrievable(&client().await, WORKSPACE, &sid, PROBE, Duration::ZERO)
        .await
        .expect_err("an unwritten collection must not pass the proof");

    assert!(
        matches!(err, IngestError::SearchDoorEmpty { .. }),
        "expected SearchDoorEmpty, got {err}"
    );
}

/// Emptiness must not be retried. If it were, this would take the full wait
/// before failing instead of returning at once.
#[tokio::test]
async fn emptiness_is_not_retried() {
    let (sid, _) = session("noretry");
    let started = std::time::Instant::now();
    let _ = prove_retrievable(
        &client().await,
        WORKSPACE,
        &sid,
        PROBE,
        Duration::from_secs(20),
    )
    .await
    .expect_err("an unwritten collection must not pass the proof");

    assert!(
        started.elapsed() < Duration::from_secs(5),
        "empty door was retried; it took {:?}",
        started.elapsed()
    );
}
