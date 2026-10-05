use std::sync::{Arc, Mutex};

use axum::{Json, Router, routing::post};
use serde_json::{Value, json};

use super::*;

/// A deterministic embedder: a bag of hashed words. Texts sharing words land
/// near each other, which is all a test of the plumbing needs.
struct Hashing;

#[async_trait::async_trait]
impl Embedder for Hashing {
    fn model(&self) -> &str {
        "hashing-16"
    }
    async fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, MemoryError> {
        Ok(texts
            .iter()
            .map(|t| {
                let mut v = vec![0.0f32; 16];
                for w in t.split_whitespace() {
                    let h = w
                        .to_lowercase()
                        .bytes()
                        .fold(7u32, |a, b| a.wrapping_mul(31).wrapping_add(b as u32));
                    v[(h % 16) as usize] += 1.0;
                }
                v
            })
            .collect())
    }
}

async fn memory(embedder: Option<Arc<dyn Embedder>>) -> Memory {
    Memory::open(Db::open_in_memory().unwrap(), embedder)
        .await
        .unwrap()
}

fn doc(id: &str, text: &str) -> Doc {
    Doc {
        id: id.into(),
        text: text.into(),
    }
}

fn sources() -> Vec<Doc> {
    vec![
        doc(
            "kernel.md",
            "# Scheduling\n\nThe scheduler picks the next process to run from the run queue \
             each time a time slice ends.\n\n# Memory\n\nPage tables map virtual addresses to \
             physical frames, and the TLB caches recent translations.",
        ),
        doc(
            "printers.md",
            "Printers were once shared through spooling daemons that queued jobs on disk.",
        ),
    ]
}

#[tokio::test]
async fn full_text_search_finds_the_passage_without_an_embedder() {
    let m = memory(None).await;
    let stats = m.index_add("ws", "c1", &sources()).await.unwrap();
    assert_eq!((stats.docs, stats.embedded), (2, 0));
    assert!(stats.chunks >= 2);

    let hits = m.search("ws", "c1", "page tables TLB", 3).await.unwrap();
    assert_eq!(hits[0].doc_id, "kernel.md");
    assert!(hits[0].text.contains("TLB"));
    assert!(hits[0].score > 0.0, "fused scores are positive");
    // The offsets point at the passage in its source.
    let src = &sources()[0].text;
    assert_eq!(&src[hits[0].start..hits[0].end], hits[0].text);

    assert!(m.search("ws", "c1", "quantum", 3).await.unwrap().is_empty());
    assert!(m.search("ws", "c1", "?!", 3).await.unwrap().is_empty());
}

#[tokio::test]
async fn vectors_are_stored_and_searched_beside_full_text() {
    let m = memory(Some(Arc::new(Hashing))).await;
    let stats = m.index_add("ws", "c1", &sources()).await.unwrap();
    assert_eq!(stats.embedded, stats.chunks);
    let hits = m.search("ws", "c1", "spooling daemons", 1).await.unwrap();
    assert_eq!(hits[0].doc_id, "printers.md");
}

#[tokio::test]
async fn collections_do_not_see_each_other_and_delete_forgets() {
    let m = memory(None).await;
    m.index_add("ws", "a", &sources()).await.unwrap();
    m.index_add(
        "ws",
        "b",
        &[doc(
            "other.md",
            "Nothing about printers or kernels at all here.",
        )],
    )
    .await
    .unwrap();
    m.index_add(
        "other_ws",
        "a",
        &[doc("x.md", "Spooling daemons in another workspace.")],
    )
    .await
    .unwrap();

    let in_b = m.search("ws", "b", "spooling daemons", 5).await.unwrap();
    assert!(in_b.iter().all(|h| h.doc_id == "other.md"));
    let in_a = m.search("ws", "a", "spooling daemons", 5).await.unwrap();
    assert!(
        in_a.iter().all(|h| h.doc_id != "x.md"),
        "workspaces are namespaces"
    );

    assert!(m.delete_collection("ws", "a").await.unwrap());
    assert!(
        m.search("ws", "a", "scheduler", 5)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(!m.delete_collection("ws", "a").await.unwrap());
    assert!(
        !m.search("other_ws", "a", "spooling", 5)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn indexing_a_document_again_replaces_it() {
    let m = memory(None).await;
    m.index_add("ws", "c", &[doc("d.md", "The old text mentions zebras.")])
        .await
        .unwrap();
    m.index_add("ws", "c", &[doc("d.md", "The new text mentions giraffes.")])
        .await
        .unwrap();
    assert!(m.search("ws", "c", "zebras", 5).await.unwrap().is_empty());
    assert_eq!(m.search("ws", "c", "giraffes", 5).await.unwrap().len(), 1);
}

/// A model that answers every extraction with two pairs naming the dimension,
/// and records each request it was sent.
async fn mock_model() -> (QaModel, Arc<Mutex<Vec<Value>>>) {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let app = Router::new().route(
        "/v1/chat/completions",
        post(move |Json(body): Json<Value>| {
            let log = log.clone();
            async move {
                let request = body["messages"][1]["content"].as_str().unwrap_or("").to_string();
                let dim = request
                    .lines()
                    .find_map(|l| l.strip_prefix("Dimension: "))
                    .unwrap_or("?")
                    .to_string();
                log.lock().unwrap().push(body);
                let pairs = json!({ "pairs": [
                    { "question": format!("What does the {dim} view say about the scheduler?"),
                      "answer": "It picks the next process. It runs when a slice ends.", "anchor": null },
                    { "question": format!("What does the {dim} view say about page tables?"),
                      "answer": "They map virtual addresses. The TLB caches them.", "anchor": null }
                ]});
                Json(json!({
                    "choices": [{ "message": { "content": pairs.to_string() } }],
                    "usage": { "prompt_tokens": 100, "completion_tokens": 50, "cost": 0.0001 }
                }))
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (
        QaModel {
            provider: opennotebook_ai::Provider::new(format!("http://{addr}/v1"), None),
            model: "m".into(),
        },
        seen,
    )
}

#[tokio::test]
async fn pairs_are_extracted_per_document_and_dimension_and_searchable() {
    let (model, seen) = mock_model().await;
    let m = memory(None).await;
    let dims = vec!["technology".to_string(), "product".to_string()];
    let docs = sources();
    let n = m.qa_extract("ws", "c", &docs, &dims, &model).await.unwrap();
    assert_eq!(n, 2 * 2 * 2, "2 docs x 2 dimensions x 2 pairs");
    {
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 4);
        assert_eq!(seen[0]["response_format"]["type"], "json_schema");
        assert!(seen[0]["temperature"].as_f64().unwrap() <= 0.3);
        assert!(seen[0]["max_tokens"].as_u64().is_some());
        // The dimension and its focus are in the request, the source after them.
        let request = seen[0]["messages"][1]["content"].as_str().unwrap();
        assert!(request.contains(dimension_description("technology")));
        assert!(request.contains("The scheduler picks the next process"));
    }

    let all = m.qa_list("ws", "c").await.unwrap();
    assert_eq!(all.len(), 8);
    assert!(
        all.iter()
            .any(|p| p.dimension == "product" && p.doc_id == "printers.md")
    );

    let hits = m.qa_search("ws", "c", "page tables", 2).await.unwrap();
    assert_eq!(hits.len(), 2);
    assert!(hits.iter().all(|h| h.pair.question.contains("page tables")));

    // Extracting again replaces rather than duplicates.
    m.qa_extract("ws", "c", &docs, &dims, &model).await.unwrap();
    assert_eq!(m.qa_list("ws", "c").await.unwrap().len(), 8);
    assert!(m.delete_collection("ws", "c").await.unwrap());
    assert!(m.qa_list("ws", "c").await.unwrap().is_empty());
}

#[tokio::test]
async fn one_failed_call_fails_the_whole_extraction_and_stores_nothing() {
    // Answers the dimension `business` with prose instead of JSON.
    let app = Router::new().route(
        "/v1/chat/completions",
        post(|Json(body): Json<Value>| async move {
            let request = body["messages"][1]["content"].as_str().unwrap_or("");
            let content = if request.contains("Dimension: business") {
                "Sorry, I cannot help with that.".to_string()
            } else {
                json!({ "pairs": [{ "question": "Q?", "answer": "A." }] }).to_string()
            };
            Json(json!({ "choices": [{ "message": { "content": content } }] }))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let model = QaModel {
        provider: opennotebook_ai::Provider::new(format!("http://{addr}/v1"), None),
        model: "m".into(),
    };
    let m = memory(None).await;
    let dims = vec!["technology".to_string(), "business".to_string()];
    let err = m
        .qa_extract("ws", "c", &sources(), &dims, &model)
        .await
        .unwrap_err();
    assert!(
        matches!(&err, MemoryError::Extract { dimension, .. } if dimension == "business"),
        "{err}"
    );
    assert!(m.qa_list("ws", "c").await.unwrap().is_empty());
}
