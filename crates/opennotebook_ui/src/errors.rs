//! Every error a person sees, in words they can act on.
//!
//! Errors reach the page from the RPC client, from the agent's stream, from a
//! page that could not be read and from a build that failed, and each used to
//! be shown as it came. That put `credits","type":"insufficient_quota"}}` in
//! the chat: half of a provider's JSON reply, cut at a colon. Everything shown
//! now goes through [`readable`], which says the failures people actually meet
//! (out of credit, a refused key, a busy or unreachable provider, the studio
//! itself unreachable) as one plain sentence with what to do, and tidies
//! anything else: no wire prefix, no JSON, no Rust debug output, no wall of
//! text. It can be applied twice, so a layer that is unsure whether text was
//! already cleaned can clean it again.

/// The failures with a fixed wording, and the words that give each away. The
/// wording of each never contains its own or an earlier kind's keys under a
/// different kind, so cleaning twice gives the same sentence.
const KNOWN: &[(&[&str], &str)] = &[
    (
        &["no sources"],
        "Add a source first: a link, a note, or a topic to research.",
    ),
    (
        &[
            "out of credit",
            "insufficient credit",
            "insufficient_quota",
            "credit balance",
            "more credits",
            "quota exhausted",
            "exceeded your current quota",
            "http 402",
        ],
        "The AI account is out of credit, so nothing can be read or made right now. \
         Add credit at openrouter.ai/settings/credits, then try again.",
    ),
    (
        &[
            "refused the key",
            "invalid api key",
            "no auth credentials",
            "missing authentication",
            "http 401",
        ],
        "The AI provider refused the studio's API key. Check the key, then try again.",
    ),
    (
        &["rate limited", "too many requests", "http 429"],
        "The AI provider is busy right now. Wait a moment and try again.",
    ),
    (
        &[
            "is the model id right",
            "model not found",
            "no endpoints found",
            "not a valid model",
        ],
        "The AI model chosen in Settings is not available. Pick another one in Settings › Models.",
    ),
    (
        &[
            "provider is unavailable",
            "upstream",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
        ],
        "The AI provider is not answering right now. Try again in a minute.",
    ),
    (
        &[
            "network error",
            "failed to fetch",
            "networkerror",
            "load failed",
            "connection refused",
        ],
        "The studio cannot be reached. Check your connection and that the studio is running, \
         then try again.",
    ),
];

/// The longest message shown whole; past it, cut at a word with "…".
const MAX_CHARS: usize = 220;

/// The fixed sentence for a failure people meet often, if `raw` is one.
pub(crate) fn known(raw: &str) -> Option<&'static str> {
    let low = raw.to_ascii_lowercase();
    KNOWN
        .iter()
        .find(|(keys, _)| keys.iter().any(|k| low.contains(k)))
        .map(|(_, say)| *say)
}

/// `raw` as a person should read it.
pub(crate) fn readable(raw: &str) -> String {
    let msg = without_wire_prefix(raw);
    if let Some(say) = known(msg) {
        return say.to_string();
    }
    // A bare status says nothing on its own.
    if let Some(code) = msg
        .trim()
        .strip_prefix("HTTP ")
        .and_then(|c| c.trim().parse::<u16>().ok())
    {
        return match code {
            404 => "That is no longer there. Reload the page and try again.".to_string(),
            408 | 504 => "The studio took too long to answer. Try again.".to_string(),
            500..=599 => "The studio ran into a problem. Try again; if it keeps happening, \
                 restart the studio."
                .to_string(),
            _ => "The studio refused that request. Reload the page and try again.".to_string(),
        };
    }
    tidy(msg)
}

/// Any message made presentable without changing what it says: no JSON, no
/// debug output, whitespace collapsed, a capital to start, a full stop to end,
/// and cut short when it runs on.
pub(crate) fn tidy(raw: &str) -> String {
    let msg = without_json(without_wire_prefix(raw));
    // `Os { code: 2, kind: NotFound, .. }` and the like: a type's debug form,
    // its name included.
    let msg = match msg.find(" { ") {
        Some(i) => {
            let head = msg[..i].trim_end();
            let head = match head.rsplit_once(' ') {
                Some((before, name)) if name.starts_with(char::is_uppercase) => before,
                _ => head,
            };
            head.trim_end_matches([':', ' ']).to_string()
        }
        None => msg,
    };
    let mut s = msg.split_whitespace().collect::<Vec<_>>().join(" ");
    if s.is_empty() {
        return "Something went wrong. Try again.".to_string();
    }
    if s.chars().count() > MAX_CHARS {
        let cut: String = s.chars().take(MAX_CHARS).collect();
        let cut = cut.rsplit_once(' ').map_or(cut.as_str(), |(a, _)| a);
        s = format!("{}…", cut.trim_end_matches([',', ';', ':', ' ']));
    }
    let mut chars = s.chars();
    let first = chars.next().map(|c| c.to_uppercase().collect::<String>());
    let mut s = first.unwrap_or_default() + chars.as_str();
    if s.ends_with(|c: char| c.is_alphanumeric() || c == ')' || c == '`') {
        s.push('.');
    }
    s
}

/// The message of `RPC error -32602: <message>`, and anything else as it is.
fn without_wire_prefix(e: &str) -> &str {
    match e.find("RPC error") {
        Some(i) => e[i..].split_once(": ").map_or(e, |(_, m)| m),
        None => e,
    }
}

/// A message without the raw JSON a provider's reply can bring along: its
/// `message` when one can be read out of it, else the words before it.
fn without_json(msg: &str) -> String {
    let Some(i) = msg.find('{') else {
        return msg.trim().to_string();
    };
    let said = serde_json::from_str::<serde_json::Value>(&msg[i..])
        .ok()
        .and_then(|v| {
            v["error"]["message"]
                .as_str()
                .or_else(|| v["message"].as_str())
                .map(str::to_string)
        });
    let before = msg[..i].trim().trim_end_matches(':').trim();
    match said {
        Some(m) if before.is_empty() => m,
        Some(m) => format!("{before}: {m}"),
        // Not JSON: a type's debug form, `Os { code: 28, .. }`, whose name goes
        // with its braces.
        None => match before.rsplit_once(' ') {
            Some((head, name))
                if name.starts_with(char::is_uppercase)
                    && name.chars().all(char::is_alphanumeric) =>
            {
                head.trim_end_matches([':', ' ']).to_string()
            }
            _ => before.to_string(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::{readable, tidy};

    #[test]
    fn out_of_credit_is_said_in_words_with_what_to_do() {
        let e = r#"RPC error -32603: the sources could not be asked: the AI account is out of credit: HTTP 402: {"error":{"message": "Insufficient credits. Add more using https://openrouter.ai/settings/credits","type":"insufficient_quota"}}"#;
        let m = readable(e);
        assert!(m.starts_with("The AI account is out of credit"), "{m}");
        assert!(
            m.contains("openrouter.ai/settings/credits") && !m.contains('{'),
            "{m}"
        );
        // What the chat showed, cut in half, is still recognised.
        assert_eq!(readable(r#"credits","type":"insufficient_quota"}}"#), m);
    }

    #[test]
    fn the_common_failures_each_have_one_sentence() {
        for (raw, starts) in [
            (
                "the AI provider refused the key: HTTP 401: User not found",
                "The AI provider refused",
            ),
            (
                "rate limited: HTTP 429: slow down",
                "The AI provider is busy",
            ),
            (
                "not found (is the model id right?): HTTP 404",
                "The AI model chosen",
            ),
            (
                "the AI provider is unavailable: HTTP 503",
                "The AI provider is not answering",
            ),
            ("network error", "The studio cannot be reached"),
            ("HTTP 500", "The studio ran into a problem"),
            ("HTTP 404", "That is no longer there"),
        ] {
            assert!(
                readable(raw).starts_with(starts),
                "{raw} -> {}",
                readable(raw)
            );
        }
    }

    #[test]
    fn cleaning_twice_says_the_same() {
        for raw in [
            "insufficient_quota",
            "invalid api key",
            "rate limited",
            "model not found",
            "upstream error",
            "failed to fetch",
            "HTTP 502",
            "HTTP 404",
            "HTTP 500",
            "map failed: the model said no",
        ] {
            let once = readable(raw);
            assert_eq!(readable(&once), once, "{raw}");
        }
    }

    #[test]
    fn anything_else_is_tidied_not_reworded() {
        assert_eq!(
            readable("RPC error -32602: map failed: the model said no"),
            "Map failed: the model said no."
        );
        assert_eq!(
            readable(r#"RPC error -32603: parse: {"error":{"message":"bad shape"}}"#),
            "Parse: bad shape."
        );
        assert_eq!(readable(r#"half: {"error":{"mess"#), "Half.");
        assert_eq!(
            tidy("could not keep it: Os { code: 28, kind: StorageFull }"),
            "Could not keep it."
        );
        let long = "word ".repeat(100);
        let t = tidy(&long);
        assert!(t.chars().count() <= 222 && t.ends_with('…'), "{t}");
        assert_eq!(tidy("   "), "Something went wrong. Try again.");
    }
}
