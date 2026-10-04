//! Runs against a real (in-memory) retrieval store. Nothing here calls a model.
//!
//! # Why the two script-generation tests are gone
//!
//! They called `generate_script`, which is one billed outline call plus one per
//! slide, so `cargo test` spent money on every run — including runs checking
//! something else. They were deleted rather than gated behind `--ignored`: a
//! test that only runs behind a flag is a test nobody runs, and it reads as
//! coverage while providing none.
//!
//! What they asserted — that a script parses, that both speakers get lines,
//! that the budgets hold — is checked without a model by the unit tests in
//! `opennotebook_script`, which exercise `parse_lines` and `budget` against
//! fixed input. What is no longer checked is whether a real model's output
//! survives that parsing, and that is a real gap, found by preparing a session
//! rather than by this file.
//!
//! # What stays, and why it costs nothing
//!
//! `an_empty_collection_refuses_rather_than_inventing` is the guard that stops
//! a script being written from the model's own knowledge instead of the
//! session's sources. `generate_script` retrieves grounding from the store
//! first and returns `NoGrounding` before any model call, so this asserts a
//! refusal that happens on the free side of the call. The cheapest test here is
//! also the one most worth keeping green.

use std::path::PathBuf;

use opennotebook_memory::Memory;
use opennotebook_script::{ScriptError, ScriptSpec, generate_script};
use opennotebook_session::{Db, Speaker, SpeakerId};

const WORKSPACE: &str = "opennotebook_tests";

async fn client() -> Memory {
    Memory::open(Db::open_in_memory().unwrap(), None)
        .await
        .expect("the retrieval store opens")
}

fn ids(tag: &str) -> (String, PathBuf) {
    let sid = format!(
        "t{tag}{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    );
    (sid, std::env::temp_dir().join("opennotebook_script_tests"))
}

fn narrator() -> Vec<Speaker> {
    vec![Speaker {
        speaker_id: SpeakerId("narrator".into()),
        voice_id: "af_bella".into(),
        display_name: "Narrator".into(),
        role: "explains the material directly".into(),
    }]
}

/// The guard that matters: generating against a collection with nothing in it
/// must refuse rather than let the model write from its own knowledge.
#[tokio::test]
async fn an_empty_collection_refuses_rather_than_inventing() {
    let c = client().await;
    let (sid, _) = ids("empty");
    let spec = ScriptSpec::new("Anything", narrator(), ("c", "p"));

    let err = generate_script(&c, WORKSPACE, &sid, &spec)
        .await
        .expect_err("an unwritten collection must not produce a script");
    assert!(
        matches!(err, ScriptError::NoGrounding { .. }),
        "expected NoGrounding, got {err}"
    );
}
