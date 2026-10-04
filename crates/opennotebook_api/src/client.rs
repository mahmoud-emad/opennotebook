//! The client transport: one JSON-RPC call over HTTP. The browser's `fetch`
//! on wasm32, reqwest everywhere else.

use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};

/// Why a call did not return a result. The `Display` text is what a page
/// shows, through its own cleaning, so its wording is part of the contract:
/// `RPC error <code>: <message>` for an error the service returned.
#[derive(Debug, Clone, PartialEq)]
pub enum ClientError {
    Connection(String),
    Serialization(String),
    Deserialization(String),
    Transport(String),
    /// A JSON-RPC error the service returned.
    RpcError {
        code: i64,
        message: String,
        data: Option<Value>,
    },
    InvalidResponse(String),
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Connection(m) => write!(f, "Connection error: {m}"),
            Self::Serialization(m) => write!(f, "Serialization error: {m}"),
            Self::Deserialization(m) => write!(f, "Deserialization error: {m}"),
            Self::Transport(m) => write!(f, "Transport error: {m}"),
            Self::RpcError {
                code,
                message,
                data,
            } => {
                write!(f, "RPC error {code}: {message}")?;
                if let Some(d) = data {
                    write!(f, " (data: {d})")?;
                }
                Ok(())
            }
            Self::InvalidResponse(m) => write!(f, "Invalid response: {m}"),
        }
    }
}

impl std::error::Error for ClientError {}

/// The `result` of a response body, or the error it carries.
fn read(body: &str) -> Result<Value, ClientError> {
    let v: Value = serde_json::from_str(body)
        .map_err(|e| ClientError::InvalidResponse(format!("{e}: {}", short(body))))?;
    if let Some(e) = v.get("error").filter(|e| !e.is_null()) {
        return Err(ClientError::RpcError {
            code: e["code"].as_i64().unwrap_or(crate::codes::INTERNAL_ERROR),
            message: e["message"].as_str().unwrap_or_default().to_string(),
            data: e.get("data").cloned().filter(|d| !d.is_null()),
        });
    }
    v.get("result")
        .cloned()
        .ok_or_else(|| ClientError::InvalidResponse(format!("no result: {}", short(body))))
}

fn short(s: &str) -> String {
    s.chars().take(200).collect()
}

/// Call `method` at `url` with `input` as its named params.
pub async fn call<I: Serialize, O: DeserializeOwned>(
    url: &str,
    method: &str,
    input: &I,
) -> Result<O, ClientError> {
    let params =
        serde_json::to_value(input).map_err(|e| ClientError::Serialization(e.to_string()))?;
    let body = json!({ "jsonrpc": "2.0", "id": 1, "method": method, "params": params }).to_string();
    let text = post(url, body).await?;
    let result = read(&text)?;
    serde_json::from_value(result).map_err(|e| ClientError::Deserialization(e.to_string()))
}

#[cfg(not(target_arch = "wasm32"))]
async fn post(url: &str, body: String) -> Result<String, ClientError> {
    let resp = reqwest::Client::new()
        .post(url)
        .header("content-type", "application/json")
        .body(body)
        .send()
        .await
        .map_err(|e| ClientError::Connection(e.to_string()))?;
    let status = resp.status();
    let text = resp
        .text()
        .await
        .map_err(|e| ClientError::Transport(e.to_string()))?;
    if !status.is_success() {
        return Err(ClientError::Transport(format!(
            "HTTP {}: {}",
            status.as_u16(),
            short(&text)
        )));
    }
    Ok(text)
}

#[cfg(target_arch = "wasm32")]
async fn post(url: &str, body: String) -> Result<String, ClientError> {
    use wasm_bindgen::JsCast;
    let js = |e: wasm_bindgen::JsValue| {
        e.as_string()
            .or_else(|| {
                e.dyn_ref::<js_sys::Error>()
                    .map(|e| String::from(e.message()))
            })
            .unwrap_or_else(|| format!("{e:?}"))
    };
    let opts = web_sys::RequestInit::new();
    opts.set_method("POST");
    opts.set_body(&wasm_bindgen::JsValue::from_str(&body));
    let req = web_sys::Request::new_with_str_and_init(url, &opts)
        .map_err(|e| ClientError::Connection(js(e)))?;
    req.headers()
        .set("content-type", "application/json")
        .map_err(|e| ClientError::Connection(js(e)))?;
    let window = web_sys::window().ok_or_else(|| ClientError::Connection("no window".into()))?;
    let resp: web_sys::Response =
        wasm_bindgen_futures::JsFuture::from(window.fetch_with_request(&req))
            .await
            .map_err(|e| ClientError::Connection(js(e)))?
            .dyn_into()
            .map_err(|e| ClientError::Transport(js(e)))?;
    let text = wasm_bindgen_futures::JsFuture::from(
        resp.text().map_err(|e| ClientError::Transport(js(e)))?,
    )
    .await
    .map_err(|e| ClientError::Transport(js(e)))?
    .as_string()
    .unwrap_or_default();
    if !resp.ok() {
        return Err(ClientError::Transport(format!(
            "HTTP {}: {}",
            resp.status(),
            short(&text)
        )));
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_error_reads_as_the_ui_expects() {
        let e = read(r#"{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"the AI account is out of credit: HTTP 402"}}"#)
            .unwrap_err();
        assert_eq!(
            e.to_string(),
            "RPC error -32603: the AI account is out of credit: HTTP 402"
        );
        assert_eq!(
            read(r#"{"jsonrpc":"2.0","id":1,"result":true}"#).unwrap(),
            json!(true)
        );
        assert!(matches!(
            read("<html>"),
            Err(ClientError::InvalidResponse(_))
        ));
    }
}
