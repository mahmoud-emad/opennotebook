//! Against a local mock server: what goes out, and what comes back.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::{Json, Router, extract::State, http::StatusCode, response::IntoResponse, routing::post};
use futures_util::StreamExt;
use serde_json::{Value, json};

use super::*;

/// One canned reply: status, body, content type.
type Reply = (u16, String, &'static str);

/// The replies the mock gives, in order (the last repeats), and every body it
/// was sent.
#[derive(Clone, Default)]
struct Mock {
    replies: Arc<Mutex<Vec<Reply>>>,
    seen: Arc<Mutex<Vec<Value>>>,
}

async fn handler(State(m): State<Mock>, Json(body): Json<Value>) -> impl IntoResponse {
    m.seen.lock().unwrap().push(body);
    let mut r = m.replies.lock().unwrap();
    let (status, text, ctype) = if r.len() > 1 {
        r.remove(0)
    } else {
        r[0].clone()
    };
    (
        StatusCode::from_u16(status).unwrap(),
        [("content-type", ctype)],
        text,
    )
}

async fn serve(replies: Vec<Reply>) -> (Provider, Mock) {
    let mock = Mock {
        replies: Arc::new(Mutex::new(replies)),
        ..Default::default()
    };
    let app = Router::new()
        .route("/v1/chat/completions", post(handler))
        .with_state(mock.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (
        Provider::new(format!("http://{addr}/v1/"), Some("k".into())),
        mock,
    )
}

fn ok_json(v: Value) -> Reply {
    (200, v.to_string(), "application/json")
}

#[tokio::test]
async fn a_completion_reads_text_usage_cost_and_finish() {
    let (p, mock) = serve(vec![ok_json(json!({
        "model": "m",
        "choices": [{ "message": { "content": "hello" }, "finish_reason": "length" }],
        "usage": { "prompt_tokens": 10, "completion_tokens": 2, "cost": 0.0042 },
        "citations": ["https://example.com"]
    }))])
    .await;
    let r = p
        .completions()
        .model("m")
        .system("be brief")
        .user("hi")
        .max_tokens(50)
        .send()
        .await
        .unwrap();
    assert_eq!(r.text, "hello");
    assert_eq!(r.finish_reason, Some(FinishReason::Length));
    // `format!("{:?}")` of the finish reason is what script generation tests
    // for truncation, so its spelling is part of the contract.
    assert!(
        format!("{:?}", r.finish_reason)
            .to_lowercase()
            .contains("length")
    );
    assert_eq!(
        r.usage,
        Some(TokenUsage {
            input_tokens: Some(10),
            output_tokens: Some(2),
            cost_usd: Some(0.0042)
        })
    );
    assert_eq!(r.raw["citations"][0], "https://example.com");

    let sent = mock.seen.lock().unwrap()[0].clone();
    assert_eq!(sent["model"], "m");
    assert_eq!(sent["max_tokens"], 50);
    assert_eq!(sent["usage"]["include"], true);
    assert_eq!(
        sent["messages"][0],
        json!({ "role": "system", "content": "be brief" })
    );
    assert_eq!(
        sent["messages"][1],
        json!({ "role": "user", "content": "hi" })
    );
    assert!(sent.get("tools").is_none());
}

#[tokio::test]
async fn tool_calls_round_trip() {
    let (p, mock) = serve(vec![ok_json(json!({
        "choices": [{ "message": { "content": null, "tool_calls": [
            { "id": "c1", "type": "function",
              "function": { "name": "web_search", "arguments": "{\"query\":\"rust\"}" } },
            { "id": "c2", "type": "function",
              "function": { "name": "broken", "arguments": "not json" } }
        ]}, "finish_reason": "tool_calls" }]
    }))])
    .await;
    let r = p
        .completions()
        .model("m")
        .user("find rust")
        .tool(ToolDefinition {
            name: "web_search".into(),
            description: "search".into(),
            input_schema: json!({ "type": "object" }),
        })
        .tool_choice(ToolChoice::Auto)
        .send()
        .await
        .unwrap();
    assert_eq!(r.text, "");
    assert_eq!(r.tool_calls[0].name, "web_search");
    assert_eq!(r.tool_calls[0].arguments["query"], "rust");
    assert_eq!(
        r.tool_calls[1].arguments,
        json!({}),
        "bad arguments become {{}}"
    );

    let sent = mock.seen.lock().unwrap()[0].clone();
    assert_eq!(sent["tool_choice"], "auto");
    assert_eq!(sent["tools"][0]["function"]["name"], "web_search");

    // The conversation sent back: the assistant's calls, then a tool result.
    let mut a = Message::assistant("");
    a.tool_calls = r.tool_calls.clone();
    let back = wire::message(&a);
    assert_eq!(back["content"], Value::Null);
    assert_eq!(
        back["tool_calls"][0]["function"]["arguments"],
        "{\"query\":\"rust\"}"
    );
    let t = wire::message(&Message::tool("c1", "3 results"));
    assert_eq!(
        t,
        json!({ "role": "tool", "content": "3 results", "tool_call_id": "c1" })
    );
}

#[test]
fn audio_and_images_go_as_parts() {
    let mut m = Message::user("");
    m.content = vec![
        ContentPart::Audio {
            mime_type: "wav".into(),
            data: "AAA".into(),
        },
        ContentPart::ImageBase64 {
            mime_type: "image/png".into(),
            data: "BBB".into(),
        },
    ];
    let v = wire::message(&m);
    assert_eq!(
        v["content"][0],
        json!({ "type": "input_audio", "input_audio": { "data": "AAA", "format": "wav" } })
    );
    assert_eq!(
        v["content"][1]["image_url"]["url"],
        "data:image/png;base64,BBB"
    );
}

#[tokio::test]
async fn out_of_credit_is_its_own_error_and_is_not_retried() {
    let (p, mock) = serve(vec![(
        402,
        json!({ "error": { "message": "Insufficient credits", "code": 402 } }).to_string(),
        "application/json",
    )])
    .await;
    let e = p
        .completions()
        .model("m")
        .user("hi")
        .send()
        .await
        .unwrap_err();
    assert!(matches!(e, CompletionsError::QuotaExceeded { .. }), "{e:?}");
    assert!(e.to_string().contains("Insufficient credits"));
    assert_eq!(mock.seen.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn an_unavailable_upstream_is_retried() {
    let (p, mock) = serve(vec![
        (503, "busy".into(), "text/plain"),
        ok_json(json!({ "choices": [{ "message": { "content": "ok" } }] })),
    ])
    .await;
    let r = p.completions().model("m").user("hi").send().await.unwrap();
    assert_eq!(r.text, "ok");
    assert_eq!(mock.seen.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn an_error_inside_a_200_is_an_error() {
    let (p, _) = serve(vec![ok_json(json!({
        "error": { "message": "Provider returned error", "code": 502 }
    }))])
    .await;
    let e = p
        .completions()
        .model("m")
        .user("hi")
        .send()
        .await
        .unwrap_err();
    assert!(matches!(e, CompletionsError::Unavailable { .. }), "{e:?}");
}

#[tokio::test]
async fn a_stream_yields_text_then_usage_then_done() {
    let sse = [
        ": OPENROUTER PROCESSING",
        r#"data: {"model":"m","choices":[{"delta":{"content":"Hel"}}]}"#,
        r#"data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}"#,
        r#"data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,"cost":0.001}}"#,
        "data: [DONE]",
        "",
    ]
    .join("\n\n");
    let (p, mock) = serve(vec![(200, sse, "text/event-stream")]).await;
    let stream = p
        .completions()
        .model("m")
        .user("hi")
        .send_stream()
        .await
        .unwrap();
    let events: Vec<CompletionEvent> = stream.collect().await;

    let text: String = events
        .iter()
        .filter_map(|e| match e {
            CompletionEvent::TextDelta(t) => Some(t.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(text, "Hello");
    assert!(events.iter().any(|e| matches!(
        e,
        CompletionEvent::UsageDelta(u) if u.cost_usd == Some(0.001)
    )));
    match events.last() {
        Some(CompletionEvent::Done(r)) => {
            assert_eq!(r.text, "Hello");
            assert_eq!(r.finish_reason, Some(FinishReason::Stop));
            assert_eq!(r.usage.as_ref().and_then(|u| u.output_tokens), Some(1));
        }
        other => panic!("ends with Done, got {other:?}"),
    }
    let sent = mock.seen.lock().unwrap()[0].clone();
    assert_eq!(sent["stream"], true);
    assert_eq!(sent["stream_options"]["include_usage"], true);
}

#[tokio::test]
async fn an_audio_models_transcript_is_its_text() {
    let sse = [
        r#"data: {"choices":[{"delta":{"content":null,"audio":{"transcript":"Good "}}}]}"#,
        r#"data: {"choices":[{"delta":{"audio":{"transcript":"question."}}}]}"#,
        "data: [DONE]",
        "",
    ]
    .join("\n\n");
    let (p, _) = serve(vec![(200, sse, "text/event-stream")]).await;
    let events: Vec<CompletionEvent> = p
        .completions()
        .model("m")
        .user("hi")
        .send_stream()
        .await
        .unwrap()
        .collect()
        .await;
    let text: String = events
        .iter()
        .filter_map(|e| match e {
            CompletionEvent::TextDelta(t) => Some(t.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(text, "Good question.");

    let r = wire::response(json!({
        "choices": [{ "message": { "content": null, "audio": { "transcript": "Yes." } } }]
    }))
    .unwrap();
    assert_eq!(r.text, "Yes.");
}

#[tokio::test]
async fn an_unreachable_server_is_unavailable() {
    let p = Provider::new("http://127.0.0.1:9/v1", None);
    let e = p
        .completions()
        .model("m")
        .user("hi")
        .options(CallOptions {
            retries: 0,
            timeout: Duration::from_secs(5),
            ..Default::default()
        })
        .send()
        .await
        .unwrap_err();
    assert!(matches!(e, CompletionsError::Unavailable { .. }), "{e:?}");
}

#[test]
fn embeddings_come_back_in_input_order() {
    let raw = json!({
        "data": [
            { "index": 1, "embedding": [0.0, 1.0] },
            { "index": 0, "embedding": [1.0, 0.0] }
        ],
        "usage": { "prompt_tokens": 4, "cost": 0.00001 }
    });
    let e = wire::embeddings(&raw, 2).unwrap();
    assert_eq!(e.vectors, vec![vec![1.0, 0.0], vec![0.0, 1.0]]);
    assert_eq!(e.usage.unwrap().input_tokens, Some(4));
    assert!(
        wire::embeddings(&raw, 3).is_err(),
        "a missing vector is an error"
    );
}

#[test]
fn a_json_schema_and_temperature_reach_the_body() {
    let b = Provider::new("http://x/v1", None)
        .completions()
        .model("m")
        .temperature(0.2)
        .json_schema("pairs", true, json!({ "type": "object" }))
        .body(false);
    assert!((b["temperature"].as_f64().unwrap() - 0.2).abs() < 1e-6);
    assert_eq!(b["response_format"]["type"], "json_schema");
    assert_eq!(b["response_format"]["json_schema"]["name"], "pairs");
    assert_eq!(b["response_format"]["json_schema"]["strict"], true);
}
