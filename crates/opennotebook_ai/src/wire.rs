//! The OpenAI chat completions wire shape, both directions.

use serde_json::{Value, json};

use crate::{
    CompletionResponse, CompletionsError, ContentPart, Embeddings, FinishReason, Message, Role,
    TokenUsage, ToolCall, ToolChoice, ToolDefinition,
};

fn role(r: Role) -> &'static str {
    match r {
        Role::System => "system",
        Role::User => "user",
        Role::Assistant => "assistant",
        Role::Tool => "tool",
    }
}

fn part(p: &ContentPart) -> Value {
    match p {
        ContentPart::Text { text } => json!({ "type": "text", "text": text }),
        ContentPart::ImageUrl { url } => {
            json!({ "type": "image_url", "image_url": { "url": url } })
        }
        ContentPart::ImageBase64 { mime_type, data } => json!({
            "type": "image_url",
            "image_url": { "url": format!("data:{mime_type};base64,{data}") }
        }),
        ContentPart::Audio { mime_type, data } => json!({
            "type": "input_audio",
            "input_audio": { "data": data, "format": mime_type }
        }),
    }
}

/// Plain text goes as a string, which every server accepts; anything else as
/// an array of parts.
fn content(parts: &[ContentPart]) -> Value {
    match parts {
        [] => Value::String(String::new()),
        [ContentPart::Text { text }] => Value::String(text.clone()),
        parts if parts.iter().all(|p| matches!(p, ContentPart::Text { .. })) => Value::String(
            parts
                .iter()
                .filter_map(|p| match p {
                    ContentPart::Text { text } => Some(text.as_str()),
                    _ => None,
                })
                .collect::<Vec<_>>()
                .join("\n"),
        ),
        parts => parts.iter().map(part).collect(),
    }
}

pub(crate) fn message(m: &Message) -> Value {
    let mut v = json!({ "role": role(m.role), "content": content(&m.content) });
    if !m.tool_calls.is_empty() {
        v["tool_calls"] = m
            .tool_calls
            .iter()
            .map(|c| {
                json!({
                    "id": c.id,
                    "type": "function",
                    "function": { "name": c.name, "arguments": c.arguments.to_string() }
                })
            })
            .collect();
        // An assistant turn that only called tools has no text, and some
        // servers reject an empty string there.
        if m.content.is_empty() {
            v["content"] = Value::Null;
        }
    }
    if let Some(id) = &m.tool_call_id {
        v["tool_call_id"] = id.clone().into();
    }
    v
}

pub(crate) fn tool(t: &ToolDefinition) -> Value {
    json!({
        "type": "function",
        "function": { "name": t.name, "description": t.description, "parameters": t.input_schema }
    })
}

pub(crate) fn tool_choice(c: ToolChoice) -> Value {
    match c {
        ToolChoice::Auto => "auto".into(),
        ToolChoice::None => "none".into(),
        ToolChoice::Required => "required".into(),
    }
}

/// Tool-call arguments arrive as a JSON string.
pub(crate) fn arguments(s: &str) -> Value {
    if s.trim().is_empty() {
        return json!({});
    }
    match serde_json::from_str(s) {
        Ok(v @ Value::Object(_)) => v,
        _ => json!({}),
    }
}

pub(crate) fn usage(u: &Value) -> Option<TokenUsage> {
    if !u.is_object() {
        return None;
    }
    Some(TokenUsage {
        input_tokens: u["prompt_tokens"].as_u64(),
        output_tokens: u["completion_tokens"].as_u64(),
        cost_usd: u["cost"].as_f64(),
    })
}

pub(crate) fn finish(v: &Value) -> Option<FinishReason> {
    v.as_str().map(FinishReason::parse)
}

pub(crate) fn response(raw: Value) -> Result<CompletionResponse, CompletionsError> {
    if raw["choices"].as_array().is_none_or(|c| c.is_empty()) {
        if raw["error"].is_object() {
            return Err(CompletionsError::from_body(&raw["error"]));
        }
        return Err(CompletionsError::Decode {
            detail: "the response has no choices".into(),
        });
    }
    let choice = &raw["choices"][0];
    let msg = &choice["message"];
    let text = match &msg["content"] {
        Value::String(s) => s.clone(),
        // Some servers return content as parts even when it is all text.
        Value::Array(parts) => parts
            .iter()
            .filter_map(|p| p["text"].as_str())
            .collect::<Vec<_>>()
            .join(""),
        // An audio model: the words are in the transcript.
        _ => msg["audio"]["transcript"]
            .as_str()
            .unwrap_or_default()
            .to_string(),
    };
    let tool_calls = msg["tool_calls"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|c| ToolCall {
            id: c["id"].as_str().unwrap_or_default().to_string(),
            name: c["function"]["name"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
            arguments: arguments(c["function"]["arguments"].as_str().unwrap_or_default()),
        })
        .collect();
    Ok(CompletionResponse {
        text,
        tool_calls,
        usage: usage(&raw["usage"]),
        finish_reason: finish(&choice["finish_reason"]),
        model: raw["model"].as_str().unwrap_or_default().to_string(),
        raw,
    })
}

pub(crate) fn embeddings(raw: &Value, expected: usize) -> Result<Embeddings, CompletionsError> {
    if raw["error"].is_object() {
        return Err(CompletionsError::from_body(&raw["error"]));
    }
    let mut rows: Vec<(usize, Vec<f32>)> = raw["data"]
        .as_array()
        .into_iter()
        .flatten()
        .enumerate()
        .map(|(i, d)| {
            let at = d["index"].as_u64().map_or(i, |n| n as usize);
            let v = d["embedding"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|x| x.as_f64().map(|f| f as f32))
                .collect();
            (at, v)
        })
        .collect();
    rows.sort_by_key(|r| r.0);
    if rows.len() != expected || rows.iter().any(|r| r.1.is_empty()) {
        return Err(CompletionsError::Decode {
            detail: format!(
                "asked for {expected} embeddings, got {} usable",
                rows.iter().filter(|r| !r.1.is_empty()).count()
            ),
        });
    }
    Ok(Embeddings {
        vectors: rows.into_iter().map(|r| r.1).collect(),
        usage: usage(&raw["usage"]),
    })
}
