//! The OpenNotebook API: one module per domain (`mindmap`, `notes`, `session`,
//! `settings`, `sources`), generated from `oschema/` by `build.rs`.
//!
//! Each module holds the domain's types, its `<Service>Api` trait and
//! `dispatch` for the server, its `<Service>Client` for callers, and its
//! OpenRPC document. This file is the runtime they share: the error types, the
//! request context, and JSON-RPC 2.0 framing.
//!
//! # The wire
//!
//! `POST <root>/api/<domain>/rpc` with
//! `{"jsonrpc":"2.0","id":1,"method":"notes_get","params":{"req":{...}}}`.
//! Params are always an object keyed by parameter name; a method with none
//! takes `{}`, `null` or no `params`. A result is the output type's JSON, or a
//! bare value for a `bool` or `str` result. An error is HTTP 200 with
//! `{"error":{"code","message","data"?}}`.

use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};

include!(concat!(env!("OUT_DIR"), "/api.rs"));

pub mod client;
mod jsonrpc;

pub use client::ClientError;
pub use jsonrpc::{Request, handle};

/// JSON-RPC 2.0 error codes.
pub mod codes {
    pub const PARSE_ERROR: i64 = -32700;
    pub const INVALID_REQUEST: i64 = -32600;
    pub const METHOD_NOT_FOUND: i64 = -32601;
    pub const INVALID_PARAMS: i64 = -32602;
    pub const INTERNAL_ERROR: i64 = -32603;
}

/// What the server knows about a request beyond its params. Empty today: one
/// person, one studio. It is passed to every method so that adding an
/// authenticated caller later is not a change to every signature.
#[derive(Debug, Clone, Default)]
pub struct RequestContext {}

/// An error a method returns, sent as the JSON-RPC `error` member.
#[derive(Debug, Clone, PartialEq)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    pub data: Option<Value>,
}

impl RpcError {
    pub fn new(code: i64, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            data: None,
        }
    }

    /// `-32602`: the caller asked for something that cannot be done.
    pub fn invalid_params(message: impl Into<String>) -> Self {
        Self::new(codes::INVALID_PARAMS, message)
    }

    /// `-32603`: the service could not do something it should have.
    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(codes::INTERNAL_ERROR, message)
    }

    pub fn method_not_found(method: &str) -> Self {
        Self::new(
            codes::METHOD_NOT_FOUND,
            format!("method not found: {method}"),
        )
    }

    pub fn with_data(mut self, data: Value) -> Self {
        self.data = Some(data);
        self
    }

    fn to_json(&self) -> Value {
        let mut e = json!({ "code": self.code, "message": self.message });
        if let Some(d) = &self.data {
            e["data"] = d.clone();
        }
        e
    }
}

impl std::fmt::Display for RpcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} ({})", self.message, self.code)
    }
}

impl std::error::Error for RpcError {}

/// A method's params as its input type. `null` is an empty object, so a
/// method with no parameters needs none sent.
pub(crate) fn params<T: DeserializeOwned>(params: Value) -> Result<T, RpcError> {
    let v = match params {
        Value::Null => Value::Object(Default::default()),
        other => other,
    };
    serde_json::from_value(v).map_err(|e| RpcError::invalid_params(format!("params: {e}")))
}

pub(crate) fn result<T: Serialize>(out: &T) -> Result<Value, RpcError> {
    serde_json::to_value(out).map_err(|e| RpcError::internal(format!("result: {e}")))
}

#[cfg(test)]
mod tests;
