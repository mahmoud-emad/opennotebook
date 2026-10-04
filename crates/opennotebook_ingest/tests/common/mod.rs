// Each integration test binary compiles this module separately, so a helper
// used by only one of them reads as dead code in the other.
#![allow(dead_code)]

//! Shared setup for the integration tests.
//!
//! The tests seed their own collections through `ingest_resources`, against a
//! real SQLite file and a local stand-in for the extraction model, so they
//! need no service and no network.

use std::path::PathBuf;
use std::sync::OnceLock;

use axum::{Json, Router, routing::post};
use opennotebook_memory::{Memory, QaModel};
use opennotebook_session::Db;
use serde_json::{Value, json};

pub const WORKSPACE: &str = "opennotebook_tests";

fn db_file() -> &'static PathBuf {
    static FILE: OnceLock<PathBuf> = OnceLock::new();
    FILE.get_or_init(|| tempfile::tempdir().unwrap().keep().join("ingest.db"))
}

/// The store, on one database shared by every test in the binary: a second
/// `client()` sees what the first wrote.
pub async fn client() -> Memory {
    Memory::open(Db::open(db_file()).unwrap(), None)
        .await
        .expect("the retrieval store opens")
}

/// An extraction model that answers every call with one pair quoting the
/// start of the document it was given.
pub async fn qa() -> QaModel {
    let app = Router::new().route(
        "/v1/chat/completions",
        post(|Json(body): Json<Value>| async move {
            let doc = body["messages"][1]["content"].as_str().unwrap_or("");
            let opening: String = doc.chars().take(80).collect();
            let pairs = json!({ "pairs": [{
                "question": "What does the document open with?",
                "answer": format!("It opens with: {opening}. That is its first passage."),
                "anchor": null
            }]});
            Json(json!({ "choices": [{ "message": { "content": pairs.to_string() } }] }))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    QaModel {
        provider: opennotebook_ai::Provider::new(format!("http://{addr}/v1"), None),
        model: "stand-in".into(),
    }
}

/// A fresh session id per test, so no two tests share a collection.
pub fn session(tag: &str) -> (String, PathBuf) {
    let sid = format!(
        "t{tag}{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    );
    (sid, std::env::temp_dir().join("opennotebook_ingest_tests"))
}

/// Which dimensions extraction pulls out. The spec does not say, so the tests
/// name them rather than letting a default hide the choice.
pub fn dimensions() -> Vec<String> {
    vec!["technology".to_string(), "architecture".to_string()]
}
