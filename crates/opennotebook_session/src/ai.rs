//! The one way OpenNotebook reaches a model.
//!
//! Every model call — the outline, the script, the slides, the create-page
//! agent, web search, answers during playback — goes to one OpenAI-compatible
//! endpoint, built here from two settings:
//!
//! * [`BASE_URL_KEY`]: OpenRouter by default; any OpenAI-compatible server
//!   works (`http://localhost:11434/v1` for Ollama).
//! * the API key: [`API_KEY_KEY`], or the conventional `OPENROUTER_API_KEY`.
//!
//! Both are read like every other setting — environment first, then
//! `settings.toml` — but neither is in the settings catalogue, so the key is
//! never sent to the settings dialog in the browser.

pub use opennotebook_ai::Provider;

/// The OpenAI-compatible base URL.
pub const BASE_URL_KEY: &str = "OPENNOTEBOOK_AI_BASE_URL";
pub const BASE_URL_DEFAULT: &str = opennotebook_ai::OPENROUTER_BASE_URL;

/// The API key. [`OPENROUTER_KEY`] is read when this is not set.
pub const API_KEY_KEY: &str = "OPENNOTEBOOK_AI_API_KEY";
pub const OPENROUTER_KEY: &str = "OPENROUTER_API_KEY";

/// The base URL every call goes to, without a trailing slash.
pub async fn base_url() -> String {
    crate::settings::setting(BASE_URL_KEY, BASE_URL_DEFAULT)
        .await
        .trim_end_matches('/')
        .to_string()
}

async fn api_key() -> Option<String> {
    let own = crate::settings::setting(API_KEY_KEY, "").await;
    let key = if own.is_empty() {
        crate::settings::setting(OPENROUTER_KEY, "").await
    } else {
        own
    };
    (!key.is_empty()).then_some(key)
}

/// The provider every model call is made with.
///
/// Refuses to build one for OpenRouter with no key, naming the setting to fix,
/// rather than let every call fail later with a bare 401.
pub async fn provider() -> Result<Provider, String> {
    let url = base_url().await;
    let key = api_key().await;
    check(&url, key.is_some())?;
    Ok(Provider::new(url, key))
}

fn check(url: &str, has_key: bool) -> Result<(), String> {
    if !has_key && url.contains("openrouter.ai") {
        return Err(format!(
            "no API key: set {OPENROUTER_KEY} in the environment or in settings.toml, \
             or point {BASE_URL_KEY} at a local OpenAI-compatible server"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn openrouter_without_a_key_names_the_fix() {
        let e = super::check("https://openrouter.ai/api/v1", false).unwrap_err();
        assert!(e.contains("OPENROUTER_API_KEY"), "{e}");
        assert!(super::check("https://openrouter.ai/api/v1", true).is_ok());
        assert!(super::check("http://localhost:11434/v1", false).is_ok());
    }
}
