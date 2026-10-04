//! A small client for OpenAI-compatible chat completions.
//!
//! One base URL and an optional key reach OpenRouter (the default), OpenAI,
//! Ollama, LM Studio or vLLM. It covers what OpenNotebook uses and no more:
//! system/user/assistant/tool messages, text, image and audio input, tool
//! calls, streaming text, and usage with the provider-reported cost.
//!
//! Three things a general-purpose client tends to hide are kept in reach on
//! purpose:
//!
//! * `usage.cost`, which OpenRouter reports per call, becomes
//!   [`TokenUsage::cost_usd`]; the spend ledger needs it.
//! * the raw response JSON, [`CompletionResponse::raw`]: a search model
//!   returns its sources in fields outside the OpenAI shape (`citations`).
//! * out-of-credit (HTTP 402) is its own error,
//!   [`CompletionsError::QuotaExceeded`], because "try again" is the wrong
//!   advice for it.

use std::pin::Pin;
use std::time::Duration;

use futures_util::{Stream, StreamExt};
use serde_json::{Value, json};

mod error;
mod stream;
mod wire;

pub use error::CompletionsError;
pub use stream::{CompletionEvent, CompletionStream};

/// Where OpenRouter's OpenAI-compatible API is.
pub const OPENROUTER_BASE_URL: &str = "https://openrouter.ai/api/v1";

/// The endpoint and credentials every call is made with. Cheap to clone.
#[derive(Clone, Debug)]
pub struct Provider {
    base_url: String,
    api_key: Option<String>,
    http: reqwest::Client,
}

impl Provider {
    /// A provider at `base_url` (e.g. `https://openrouter.ai/api/v1`, or
    /// `http://localhost:11434/v1` for Ollama). `api_key` is sent as a bearer
    /// token when present; a local server usually needs none.
    pub fn new(base_url: impl Into<String>, api_key: Option<String>) -> Self {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(20))
            .build()
            .unwrap_or_default();
        Self {
            base_url: base_url.into().trim().trim_end_matches('/').to_string(),
            api_key: api_key.filter(|k| !k.trim().is_empty()),
            http,
        }
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub fn has_key(&self) -> bool {
        self.api_key.is_some()
    }

    /// Start a chat completion request.
    pub fn completions(&self) -> CompletionBuilder {
        CompletionBuilder {
            provider: self.clone(),
            model: String::new(),
            messages: Vec::new(),
            tools: Vec::new(),
            tool_choice: None,
            max_tokens: None,
            temperature: None,
            response_format: None,
            options: CallOptions::default(),
        }
    }

    /// Embed `inputs` with `model` (`POST /embeddings`), one vector per input,
    /// in order.
    pub async fn embeddings(
        &self,
        model: &str,
        inputs: &[String],
    ) -> Result<Embeddings, CompletionsError> {
        let resp = self
            .authed(self.http.post(format!("{}/embeddings", self.base_url)))
            .json(&json!({ "model": model, "input": inputs }))
            .timeout(Duration::from_secs(120))
            .send()
            .await
            .map_err(|e| CompletionsError::Unavailable {
                detail: e.to_string(),
            })?;
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            return Err(CompletionsError::from_status(status.as_u16(), &body));
        }
        let raw: Value = serde_json::from_str(&body).map_err(|e| CompletionsError::Decode {
            detail: e.to_string(),
        })?;
        wire::embeddings(&raw, inputs.len())
    }

    /// `GET /models`, as the provider returns it. OpenRouter lists every model
    /// with its `pricing`; a local server lists its models without one.
    pub async fn models(&self) -> Result<Value, CompletionsError> {
        let resp = self
            .authed(self.http.get(format!("{}/models", self.base_url)))
            .timeout(Duration::from_secs(30))
            .send()
            .await
            .map_err(|e| CompletionsError::Unavailable {
                detail: e.to_string(),
            })?;
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            return Err(CompletionsError::from_status(status.as_u16(), &body));
        }
        serde_json::from_str(&body).map_err(|e| CompletionsError::Decode {
            detail: e.to_string(),
        })
    }

    fn authed(&self, req: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        let req = req
            // OpenRouter's attribution headers; ignored everywhere else.
            .header(
                "HTTP-Referer",
                "https://github.com/mahmoud-emad/opennotebook",
            )
            .header("X-Title", "OpenNotebook");
        match &self.api_key {
            Some(k) => req.bearer_auth(k),
            None => req,
        }
    }
}

/// Who a message is from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

/// One piece of a message's content.
#[derive(Clone, Debug, PartialEq)]
pub enum ContentPart {
    Text {
        text: String,
    },
    /// An image by URL, or a `data:` URL.
    ImageUrl {
        url: String,
    },
    /// An image as base64 bytes.
    ImageBase64 {
        mime_type: String,
        data: String,
    },
    /// Audio as base64 bytes. `mime_type` is the provider's `format` field,
    /// which wants a container name (`wav`, `mp3`), not a MIME type.
    Audio {
        mime_type: String,
        data: String,
    },
}

/// One message of a conversation.
#[derive(Clone, Debug, PartialEq)]
pub struct Message {
    pub role: Role,
    pub content: Vec<ContentPart>,
    /// The calls an assistant message made, sent back with the conversation.
    pub tool_calls: Vec<ToolCall>,
    /// The call a tool message answers.
    pub tool_call_id: Option<String>,
}

impl Message {
    fn text(role: Role, text: impl Into<String>) -> Self {
        let text = text.into();
        Self {
            role,
            content: if text.is_empty() {
                Vec::new()
            } else {
                vec![ContentPart::Text { text }]
            },
            tool_calls: Vec::new(),
            tool_call_id: None,
        }
    }

    pub fn system(text: impl Into<String>) -> Self {
        Self::text(Role::System, text)
    }

    pub fn user(text: impl Into<String>) -> Self {
        Self::text(Role::User, text)
    }

    pub fn assistant(text: impl Into<String>) -> Self {
        Self::text(Role::Assistant, text)
    }

    /// A tool's result, answering the call with id `call_id`.
    pub fn tool(call_id: impl Into<String>, result: impl Into<String>) -> Self {
        let mut m = Self::text(Role::Tool, result);
        m.tool_call_id = Some(call_id.into());
        m
    }
}

/// A function the model may call.
#[derive(Clone, Debug, PartialEq)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    /// The arguments, as a JSON Schema object.
    pub input_schema: Value,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ToolChoice {
    Auto,
    None,
    Required,
}

/// A call the model made.
#[derive(Clone, Debug, PartialEq)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    /// The arguments, parsed. Arguments that are not valid JSON arrive as an
    /// empty object, so a tool reads absent fields rather than crashing.
    pub arguments: Value,
}

/// Why the model stopped. `Length` means it ran out of `max_tokens`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FinishReason {
    Stop,
    Length,
    ToolCalls,
    ContentFilter,
    Other(String),
}

impl FinishReason {
    fn parse(s: &str) -> Self {
        match s {
            "stop" | "end_turn" => Self::Stop,
            "length" | "max_tokens" => Self::Length,
            "tool_calls" | "function_call" | "tool_use" => Self::ToolCalls,
            "content_filter" => Self::ContentFilter,
            other => Self::Other(other.to_string()),
        }
    }
}

/// What a call used, and what it cost when the provider says.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TokenUsage {
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    /// USD, as reported by the provider (OpenRouter's `usage.cost`). `None`
    /// when it does not report one; the caller may price it from a catalog.
    pub cost_usd: Option<f64>,
}

/// A finished, non-streamed completion.
#[derive(Clone, Debug)]
pub struct CompletionResponse {
    pub text: String,
    pub tool_calls: Vec<ToolCall>,
    pub usage: Option<TokenUsage>,
    pub finish_reason: Option<FinishReason>,
    /// The model that answered, as the provider names it.
    pub model: String,
    /// The whole response body.
    pub raw: Value,
}

/// The vectors an embeddings call returned, and what it used.
#[derive(Clone, Debug, PartialEq)]
pub struct Embeddings {
    pub vectors: Vec<Vec<f32>>,
    pub usage: Option<TokenUsage>,
}

/// Per-call behaviour.
#[derive(Clone, Debug)]
pub struct CallOptions {
    /// Attempts after the first for a call that failed in a way worth
    /// repeating: a rate limit, an unavailable upstream, a dropped connection.
    pub retries: u32,
    /// The whole call, every attempt included, gives up after this.
    pub timeout: Duration,
    /// Kept for callers written against a client with a circuit breaker;
    /// this one has none, so the flag changes nothing.
    pub use_breaker: bool,
}

impl Default for CallOptions {
    fn default() -> Self {
        Self {
            retries: 2,
            timeout: Duration::from_secs(300),
            use_breaker: true,
        }
    }
}

/// A chat completion being put together. Consumed by [`send`](Self::send) or
/// [`send_stream`](Self::send_stream).
#[derive(Clone, Debug)]
pub struct CompletionBuilder {
    provider: Provider,
    model: String,
    messages: Vec<Message>,
    tools: Vec<ToolDefinition>,
    tool_choice: Option<ToolChoice>,
    max_tokens: Option<u32>,
    temperature: Option<f32>,
    response_format: Option<Value>,
    options: CallOptions,
}

impl CompletionBuilder {
    pub fn model(mut self, model: impl Into<String>) -> Self {
        self.model = model.into();
        self
    }

    pub fn message(mut self, m: Message) -> Self {
        self.messages.push(m);
        self
    }

    pub fn system(self, text: impl Into<String>) -> Self {
        self.message(Message::system(text))
    }

    pub fn user(self, text: impl Into<String>) -> Self {
        self.message(Message::user(text))
    }

    pub fn max_tokens(mut self, n: u32) -> Self {
        self.max_tokens = Some(n);
        self
    }

    pub fn temperature(mut self, t: f32) -> Self {
        self.temperature = Some(t);
        self
    }

    /// Ask for JSON matching `schema` (OpenAI structured outputs). `strict`
    /// asks the provider to enforce it; a provider without structured outputs
    /// may ignore it, so the caller still parses defensively.
    pub fn json_schema(mut self, name: &str, strict: bool, schema: Value) -> Self {
        self.response_format = Some(json!({
            "type": "json_schema",
            "json_schema": { "name": name, "strict": strict, "schema": schema }
        }));
        self
    }

    pub fn tool(mut self, t: ToolDefinition) -> Self {
        self.tools.push(t);
        self
    }

    pub fn tool_choice(mut self, c: ToolChoice) -> Self {
        self.tool_choice = Some(c);
        self
    }

    pub fn options(mut self, o: CallOptions) -> Self {
        self.options = o;
        self
    }

    fn body(&self, stream: bool) -> Value {
        let mut body = json!({
            "model": self.model,
            "messages": self.messages.iter().map(wire::message).collect::<Vec<_>>(),
            // OpenRouter: report cost in `usage.cost`. Ignored elsewhere.
            "usage": { "include": true },
        });
        if let Some(n) = self.max_tokens {
            body["max_tokens"] = n.into();
        }
        if let Some(t) = self.temperature {
            body["temperature"] = t.into();
        }
        if let Some(f) = &self.response_format {
            body["response_format"] = f.clone();
        }
        if !self.tools.is_empty() {
            body["tools"] = self.tools.iter().map(wire::tool).collect();
            if let Some(c) = self.tool_choice {
                body["tool_choice"] = wire::tool_choice(c);
            }
        }
        if stream {
            body["stream"] = true.into();
            body["stream_options"] = json!({ "include_usage": true });
        }
        body
    }

    /// One POST, without retries.
    async fn post(&self, body: &Value) -> Result<reqwest::Response, CompletionsError> {
        let resp = self
            .provider
            .authed(
                self.provider
                    .http
                    .post(format!("{}/chat/completions", self.provider.base_url)),
            )
            .json(body)
            .send()
            .await
            .map_err(|e| CompletionsError::Unavailable {
                detail: e.to_string(),
            })?;
        let status = resp.status();
        if status.is_success() {
            return Ok(resp);
        }
        let text = resp.text().await.unwrap_or_default();
        Err(CompletionsError::from_status(status.as_u16(), &text))
    }

    /// POST with the retries the options allow.
    async fn post_retrying(&self, body: &Value) -> Result<reqwest::Response, CompletionsError> {
        let mut attempt = 0;
        loop {
            match self.post(body).await {
                Err(e) if e.is_retryable() && attempt < self.options.retries => {
                    attempt += 1;
                    tokio::time::sleep(Duration::from_millis(750 * 2u64.pow(attempt - 1))).await;
                }
                other => return other,
            }
        }
    }

    /// Send and wait for the whole answer.
    pub async fn send(self) -> Result<CompletionResponse, CompletionsError> {
        if self.model.is_empty() {
            return Err(CompletionsError::InvalidRequest("no model named".into()));
        }
        let body = self.body(false);
        let call = async {
            let resp = self.post_retrying(&body).await?;
            let raw: Value = resp.json().await.map_err(|e| CompletionsError::Decode {
                detail: e.to_string(),
            })?;
            wire::response(raw)
        };
        tokio::time::timeout(self.options.timeout, call)
            .await
            .map_err(|_| CompletionsError::Unavailable {
                detail: format!("no answer within {}s", self.options.timeout.as_secs()),
            })?
    }

    /// Send and read the answer as it is written.
    pub async fn send_stream(self) -> Result<CompletionStream, CompletionsError> {
        if self.model.is_empty() {
            return Err(CompletionsError::InvalidRequest("no model named".into()));
        }
        let body = self.body(true);
        let resp = tokio::time::timeout(self.options.timeout, self.post_retrying(&body))
            .await
            .map_err(|_| CompletionsError::Unavailable {
                detail: format!("no answer within {}s", self.options.timeout.as_secs()),
            })??;
        let bytes: Pin<Box<dyn Stream<Item = Result<Vec<u8>, String>> + Send>> = Box::pin(
            resp.bytes_stream()
                .map(|r| r.map(|b| b.to_vec()).map_err(|e| e.to_string())),
        );
        Ok(stream::events(bytes))
    }
}

#[cfg(test)]
mod tests;
