use thiserror::Error;

/// Why a completion did not come back.
#[derive(Debug, Clone, Error, PartialEq)]
pub enum CompletionsError {
    /// Out of credit or over a spending cap (HTTP 402). Repeating the call
    /// will fail the same way until someone adds credit.
    #[error("the AI account is out of credit: {detail}")]
    QuotaExceeded { detail: String },

    #[error("rate limited: {detail}")]
    RateLimited { detail: String },

    /// The key is missing, wrong or not allowed this model.
    #[error("the AI provider refused the key: {detail}")]
    Authentication { detail: String },

    #[error("not found (is the model id right?): {detail}")]
    NotFound { detail: String },

    /// The provider could not be reached, timed out or failed on its side.
    #[error("the AI provider is unavailable: {detail}")]
    Unavailable { detail: String },

    #[error("the AI provider's answer could not be read: {detail}")]
    Decode { detail: String },

    #[error("the request was refused: {0}")]
    InvalidRequest(String),
}

impl CompletionsError {
    /// Worth repeating unchanged.
    pub fn is_retryable(&self) -> bool {
        matches!(self, Self::RateLimited { .. } | Self::Unavailable { .. })
    }

    /// The error an HTTP status and its body come to.
    pub(crate) fn from_status(status: u16, body: &str) -> Self {
        let detail = detail(status, body);
        // Out of credit wins over the status it came with: relayed through a
        // gateway it can arrive as a 5xx, or inside a 200 with a string code.
        // Not over 401 (a bad key) or 429: a per-minute rate limit is often
        // worded "quota exceeded" and clears by itself.
        if !matches!(status, 401 | 429) && (looks_like_credit(body) || looks_like_credit(&detail)) {
            return Self::QuotaExceeded { detail };
        }
        match status {
            402 => Self::QuotaExceeded { detail },
            401 | 403 => {
                // Some providers answer an exhausted balance with 403.
                if looks_like_credit(&detail) {
                    Self::QuotaExceeded { detail }
                } else {
                    Self::Authentication { detail }
                }
            }
            404 => Self::NotFound { detail },
            408 | 429 => Self::RateLimited { detail },
            500..=599 => Self::Unavailable { detail },
            _ if looks_like_credit(&detail) => Self::QuotaExceeded { detail },
            _ => Self::InvalidRequest(detail),
        }
    }

    /// The error a 200 whose body is `{"error": {...}}` comes to. OpenRouter
    /// answers some upstream failures this way. `code` may be a number or a
    /// string; a string one says nothing about the status.
    pub(crate) fn from_body(error: &serde_json::Value) -> Self {
        let code = error["code"]
            .as_u64()
            .filter(|c| (100..600).contains(c))
            .unwrap_or(0) as u16;
        let body = serde_json::json!({ "error": error }).to_string();
        Self::from_status(if code == 0 { 502 } else { code }, &body)
    }
}

/// The provider's own message when the body has one, else the body itself.
///
/// A relayed upstream error nests: OpenRouter's `error.message` can itself be
/// the upstream's JSON body (`{"error":{"message":"…","type":"insufficient_quota"}}`),
/// so the message is unwrapped until it is prose.
fn detail(status: u16, body: &str) -> String {
    let msg = message_of(body).unwrap_or_else(|| body.chars().take(300).collect());
    if msg.trim().is_empty() {
        format!("HTTP {status}")
    } else {
        format!("HTTP {status}: {}", msg.trim())
    }
}

/// The innermost human message in an error body, if it has one.
fn message_of(body: &str) -> Option<String> {
    let mut text = body.trim().to_string();
    let mut found = None;
    for _ in 0..4 {
        let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) else {
            break;
        };
        let inner = v["error"]["message"]
            .as_str()
            .or_else(|| v["error"].as_str())
            .or_else(|| v["message"].as_str())
            .or_else(|| v["error"]["metadata"]["raw"].as_str());
        match inner {
            Some(m) => {
                text = m.trim().to_string();
                found = Some(text.clone());
            }
            None => break,
        }
    }
    found.filter(|m| !m.starts_with('{'))
}

fn looks_like_credit(detail: &str) -> bool {
    let d = detail.to_ascii_lowercase();
    [
        "insufficient credit",
        "insufficient_quota",
        "out of credit",
        "credit balance",
        "quota",
    ]
    .iter()
    .any(|k| d.contains(k))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_relayed_upstream_body_is_unwrapped_to_its_message() {
        let upstream = json!({ "error": {
            "message": "You exceeded your current quota, please check your plan.",
            "type": "insufficient_quota", "code": "insufficient_quota"
        }})
        .to_string();
        let body = json!({ "error": { "message": upstream, "code": 402 } }).to_string();
        let e = CompletionsError::from_status(402, &body);
        assert_eq!(
            e,
            CompletionsError::QuotaExceeded {
                detail: "HTTP 402: You exceeded your current quota, please check your plan.".into()
            }
        );
        assert!(!e.to_string().contains('{'), "{e}");
    }

    #[test]
    fn raw_metadata_is_read_when_the_message_is_generic() {
        let raw =
            json!({ "error": { "message": "Insufficient credits on the account" } }).to_string();
        let body = json!({ "error": { "metadata": { "raw": raw } } }).to_string();
        assert_eq!(
            message_of(&body).as_deref(),
            Some("Insufficient credits on the account")
        );
    }

    #[test]
    fn out_of_credit_is_quota_whatever_status_it_came_with() {
        let inside_200 = json!({ "message": "insufficient_quota", "code": "insufficient_quota" });
        assert!(matches!(
            CompletionsError::from_body(&inside_200),
            CompletionsError::QuotaExceeded { .. }
        ));
        let as_502 =
            json!({ "error": { "message": "Upstream: insufficient credits" } }).to_string();
        let e = CompletionsError::from_status(502, &as_502);
        assert!(matches!(e, CompletionsError::QuotaExceeded { .. }), "{e:?}");
        assert!(!e.is_retryable());
        // A per-minute limit is still a rate limit, and retried.
        let per_minute =
            json!({ "error": { "message": "Quota exceeded for requests per minute" } });
        let e = CompletionsError::from_status(429, &per_minute.to_string());
        assert!(matches!(e, CompletionsError::RateLimited { .. }), "{e:?}");
        // A bad key stays a bad key.
        assert!(matches!(
            CompletionsError::from_status(401, "{\"error\":{\"message\":\"No auth\"}}"),
            CompletionsError::Authentication { .. }
        ));
    }

    #[test]
    fn a_body_that_is_not_json_is_kept_short() {
        let e = CompletionsError::from_status(503, &"x".repeat(1000));
        assert_eq!(e.to_string().matches('x').count(), 300);
    }
}
