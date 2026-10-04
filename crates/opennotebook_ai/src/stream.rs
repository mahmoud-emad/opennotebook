//! A streamed completion: server-sent events in, [`CompletionEvent`]s out.

use std::pin::Pin;

use futures_util::{Stream, StreamExt};
use serde_json::Value;

use crate::{CompletionResponse, CompletionsError, TokenUsage, wire};

/// One thing that happened while the answer was being written.
#[derive(Debug, Clone)]
pub enum CompletionEvent {
    /// More of the answer's text.
    TextDelta(String),
    /// What the call used, usually in the last chunk.
    UsageDelta(TokenUsage),
    /// The answer is complete. Carries the whole text and the usage.
    Done(Box<CompletionResponse>),
    /// The stream broke. Nothing follows it.
    Error(CompletionsError),
}

pub type CompletionStream = Pin<Box<dyn Stream<Item = CompletionEvent> + Send>>;

type Bytes = Pin<Box<dyn Stream<Item = Result<Vec<u8>, String>> + Send>>;

/// Split an SSE byte stream into `data:` payloads and turn them into events.
pub(crate) fn events(bytes: Bytes) -> CompletionStream {
    struct State {
        bytes: Bytes,
        buf: Vec<u8>,
        text: String,
        usage: Option<TokenUsage>,
        finish: Option<crate::FinishReason>,
        model: String,
        pending: std::collections::VecDeque<CompletionEvent>,
        ended: bool,
    }

    let state = State {
        bytes,
        buf: Vec::new(),
        text: String::new(),
        usage: None,
        finish: None,
        model: String::new(),
        pending: Default::default(),
        ended: false,
    };

    Box::pin(futures_util::stream::unfold(state, |mut s| async move {
        loop {
            if let Some(ev) = s.pending.pop_front() {
                return Some((ev, s));
            }
            if s.ended {
                return None;
            }
            // A whole line in the buffer: handle it.
            if let Some(nl) = s.buf.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = s.buf.drain(..=nl).collect();
                let line = String::from_utf8_lossy(&line);
                let line = line.trim_end_matches(['\r', '\n']);
                let Some(data) = line.strip_prefix("data:") else {
                    // Comments (`: keep-alive`), `event:` lines, blanks.
                    continue;
                };
                let data = data.trim();
                if data == "[DONE]" {
                    s.ended = true;
                    s.pending
                        .push_back(done(&mut s.text, &s.usage, &s.finish, &s.model));
                    continue;
                }
                let Ok(chunk) = serde_json::from_str::<Value>(data) else {
                    continue;
                };
                if chunk["error"].is_object() {
                    s.ended = true;
                    s.pending
                        .push_back(CompletionEvent::Error(CompletionsError::from_body(
                            &chunk["error"],
                        )));
                    continue;
                }
                if let Some(m) = chunk["model"].as_str() {
                    s.model = m.to_string();
                }
                let choice = &chunk["choices"][0];
                // An audio model answers with `content: null` and its words in
                // `audio.transcript`; either is the answer's text.
                let delta = &choice["delta"];
                if let Some(t) = delta["content"]
                    .as_str()
                    .or_else(|| delta["audio"]["transcript"].as_str())
                    && !t.is_empty()
                {
                    s.text.push_str(t);
                    s.pending
                        .push_back(CompletionEvent::TextDelta(t.to_string()));
                }
                if let Some(f) = wire::finish(&choice["finish_reason"]) {
                    s.finish = Some(f);
                }
                if let Some(u) = wire::usage(&chunk["usage"]) {
                    s.usage = Some(u.clone());
                    s.pending.push_back(CompletionEvent::UsageDelta(u));
                }
                continue;
            }
            match s.bytes.next().await {
                Some(Ok(b)) => s.buf.extend_from_slice(&b),
                Some(Err(e)) => {
                    s.ended = true;
                    s.pending
                        .push_back(CompletionEvent::Error(CompletionsError::Unavailable {
                            detail: format!("the stream broke: {e}"),
                        }));
                }
                // A server that closes without `[DONE]` has still finished.
                None => {
                    s.ended = true;
                    s.pending
                        .push_back(done(&mut s.text, &s.usage, &s.finish, &s.model));
                }
            }
        }
    }))
}

fn done(
    text: &mut String,
    usage: &Option<TokenUsage>,
    finish: &Option<crate::FinishReason>,
    model: &str,
) -> CompletionEvent {
    CompletionEvent::Done(Box::new(CompletionResponse {
        text: std::mem::take(text),
        tool_calls: Vec::new(),
        usage: usage.clone(),
        finish_reason: finish.clone(),
        model: model.to_string(),
        raw: Value::Null,
    }))
}
