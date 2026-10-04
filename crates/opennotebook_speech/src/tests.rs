//! Against a local stand-in for an OpenAI-compatible speech server.

use std::sync::{Arc, Mutex};

use axum::{
    Json, Router,
    extract::Multipart,
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    routing::post,
};
use serde_json::{Value, json};

use super::*;

/// The request body and the `Authorization` header the stand-in was sent.
type Seen = Arc<Mutex<Option<(Value, Option<String>)>>>;

async fn serve(app: Router) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{addr}/v1/")
}

#[tokio::test]
async fn synthesis_asks_for_wav_and_delivers_24k_mono() {
    let seen: Seen = Arc::default();
    let log = seen.clone();
    let app = Router::new().route(
        "/v1/audio/speech",
        post(move |h: HeaderMap, Json(body): Json<Value>| {
            let log = log.clone();
            async move {
                let auth = h
                    .get("authorization")
                    .map(|v| v.to_str().unwrap().to_string());
                *log.lock().unwrap() = Some((body, auth));
                // A 48 kHz stereo server: normalised on the way out.
                wav::test_wav(48_000, 2, 4800)
            }
        }),
    );
    let url = serve(app).await;
    let s = Speech::new(&url, &url, "kokoro", "whisper", Some("k".into()));
    let out = s.synthesize("Hello there.", "af_bella").await.unwrap();

    assert_eq!(&out[0..4], b"RIFF");
    assert_eq!(
        u32::from_le_bytes([out[24], out[25], out[26], out[27]]),
        24_000
    );
    assert_eq!(u16::from_le_bytes([out[22], out[23]]), 1, "mono");

    let (body, auth) = seen.lock().unwrap().clone().unwrap();
    assert_eq!(
        body,
        json!({ "model": "kokoro", "input": "Hello there.", "voice": "af_bella", "response_format": "wav" })
    );
    assert_eq!(auth.as_deref(), Some("Bearer k"));
}

#[tokio::test]
async fn a_server_error_names_the_reason() {
    let app = Router::new().route(
        "/v1/audio/speech",
        post(|| async {
            (
                StatusCode::NOT_FOUND,
                Json(json!({ "detail": "Voice 'zz_nobody' not found" })),
            )
                .into_response()
        }),
    );
    let url = serve(app).await;
    let s = Speech::new(&url, &url, "m", "m", None);
    let e = s.synthesize("Hi.", "zz_nobody").await.unwrap_err();
    assert_eq!(
        e,
        SpeechError::Refused {
            status: 404,
            detail: "Voice 'zz_nobody' not found".into()
        }
    );
}

#[tokio::test]
async fn transcription_sends_the_wav_as_a_file() {
    let seen: Arc<Mutex<Vec<(String, usize)>>> = Arc::default();
    let log = seen.clone();
    let app = Router::new().route(
        "/v1/audio/transcriptions",
        post(move |mut form: Multipart| {
            let log = log.clone();
            async move {
                while let Some(f) = form.next_field().await.unwrap() {
                    let name = f.name().unwrap_or("").to_string();
                    let len = f.bytes().await.unwrap().len();
                    log.lock().unwrap().push((name, len));
                }
                Json(json!({ "text": "  what is a page table?  " }))
            }
        }),
    );
    let url = serve(app).await;
    let s = Speech::new(&url, &url, "tts", "whisper-small", None);
    let wav = wav::test_wav(16_000, 1, 1600);
    assert_eq!(s.transcribe(&wav).await.unwrap(), "what is a page table?");
    let seen = seen.lock().unwrap();
    assert!(seen.contains(&("file".to_string(), wav.len())));
    assert!(seen.iter().any(|(n, _)| n == "model"));
}

#[tokio::test]
async fn an_unreachable_server_says_where_it_looked() {
    let s = Speech::new(
        "http://127.0.0.1:9/v1",
        "http://127.0.0.1:9/v1",
        "m",
        "m",
        None,
    );
    let e = s.synthesize("Hi.", "af_bella").await.unwrap_err();
    assert!(e.to_string().contains("127.0.0.1:9"), "{e}");
}
