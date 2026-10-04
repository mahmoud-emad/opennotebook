//! JSON-RPC 2.0 framing around a domain's `dispatch`.

use std::future::Future;

use serde::Deserialize;
use serde_json::{Value, json};

use crate::{RpcError, codes};

/// One request. `id` absent means a notification, which gets no response.
#[derive(Debug, Deserialize)]
pub struct Request {
    #[serde(default)]
    pub jsonrpc: Option<String>,
    #[serde(default)]
    pub id: Option<Value>,
    pub method: String,
    #[serde(default)]
    pub params: Value,
}

fn error(id: Value, e: &RpcError) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "error": e.to_json() })
}

/// Answer a request body: one request or a batch of up to 100.
///
/// `call` runs one method; `service` and `openrpc` answer the two reserved
/// methods, `rpc.health` and `rpc.discover`. `None` means there is nothing to
/// send back (only notifications).
pub async fn handle<F, Fut>(
    body: &[u8],
    service: &str,
    version: &str,
    openrpc: &str,
    call: F,
) -> Option<Value>
where
    F: Fn(String, Value) -> Fut,
    Fut: Future<Output = Result<Value, RpcError>>,
{
    let parsed: Value = match serde_json::from_slice(body) {
        Ok(v) => v,
        Err(e) => {
            return Some(error(
                Value::Null,
                &RpcError::new(codes::PARSE_ERROR, format!("parse error: {e}")),
            ));
        }
    };
    let one = |req: Value| {
        let call = &call;
        async move {
            let req: Request = match serde_json::from_value(req) {
                Ok(r) => r,
                Err(e) => {
                    return Some(error(
                        Value::Null,
                        &RpcError::new(codes::INVALID_REQUEST, format!("invalid request: {e}")),
                    ));
                }
            };
            let id = req.id.clone();
            if req.jsonrpc.as_deref() != Some("2.0") {
                return Some(error(
                    id.unwrap_or(Value::Null),
                    &RpcError::new(
                        codes::INVALID_REQUEST,
                        "invalid request: jsonrpc must be \"2.0\"",
                    ),
                ));
            }
            let outcome = match req.method.as_str() {
                "rpc.health" => {
                    Ok(json!({ "status": "ok", "service": service, "version": version }))
                }
                "rpc.discover" => serde_json::from_str(openrpc)
                    .map_err(|e| RpcError::internal(format!("openrpc document: {e}"))),
                _ => call(req.method, req.params).await,
            };
            let id = id?;
            Some(match outcome {
                Ok(result) => json!({ "jsonrpc": "2.0", "id": id, "result": result }),
                Err(e) => error(id, &e),
            })
        }
    };
    match parsed {
        Value::Array(batch) => {
            if batch.is_empty() || batch.len() > 100 {
                return Some(error(
                    Value::Null,
                    &RpcError::new(
                        codes::INVALID_REQUEST,
                        "invalid request: a batch holds 1 to 100 requests",
                    ),
                ));
            }
            let mut out = Vec::new();
            for r in batch {
                if let Some(resp) = one(r).await {
                    out.push(resp);
                }
            }
            (!out.is_empty()).then_some(Value::Array(out))
        }
        single => one(single).await,
    }
}
