//! The settings dialog's two routes: read every setting, write one.
//!
//! Settings live in `settings.toml` in the data directory, with environment
//! overrides, and every consumer reads them at the moment it needs a value.
//! So a change here reaches the next prep, the next question and the next page
//! load without a restart.
//!
//! The dialog is drawn from what `GET` returns — tabs, labels, help, control
//! kind and options all come from `opennotebook_session::settings::CATALOGUE` —
//! so the page carries no copy of the list to drift from it.

use axum::Json;
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::{Value, json};

use opennotebook_session::settings::{self, CATALOGUE, TABS};

use crate::settings_impl::{self, SaveError};

/// `GET /api/session/settings`
///
/// `tabs` is the tab labels in order, each item's `tab` one of them;
/// `tab_info` adds an id, a note and whether the tab is advanced.
pub async fn serve_get() -> Response {
    match settings_impl::described().await {
        Ok((tab_info, described)) => {
            let items: Vec<Value> = CATALOGUE
                .iter()
                .zip(described)
                .map(|(d, s)| item(d, s))
                .collect();
            Json(json!({ "ok": true, "tabs": TABS, "tab_info": tab_info, "items": items }))
                .into_response()
        }
        Err(e) => Json(json!({ "ok": false, "error": e })).into_response(),
    }
}

#[derive(Deserialize)]
pub struct SetReq {
    pub key: String,
    #[serde(default)]
    pub value: String,
}

/// `POST /api/session/settings` — `{key, value}`. An empty value resets the
/// setting to its default. `note`, when not empty, is a caveat about a save
/// that went through.
///
/// Answers 200 with `ok: false` and the reason on a refusal, not a 4xx: the
/// page shows the reason under the control, and a status code alone would
/// reach it as "HTTP 400".
pub async fn serve_set(Json(req): Json<SetReq>) -> Response {
    match settings_impl::save(&req.key, &req.value).await {
        Ok((s, note)) => {
            let d = settings::def(&req.key).expect("save found it");
            Json(json!({ "ok": true, "item": item(d, s), "note": note })).into_response()
        }
        Err(SaveError::Refused(e) | SaveError::Store(e)) => {
            Json(json!({ "ok": false, "error": e })).into_response()
        }
    }
}

/// One setting, as the dialog draws it: the RPC shape plus the text
/// suggestions the page offers in a datalist.
fn item(d: &settings::Def, s: crate::settings::Setting) -> Value {
    let mut v = serde_json::to_value(s).unwrap_or_default();
    v["suggestions"] = json!(settings_impl::suggestions(d));
    v
}

#[cfg(test)]
mod tests {
    /// Every style default names a style that exists, or the dialog opens on a
    /// blank select.
    #[test]
    fn the_default_style_exists() {
        let d =
            opennotebook_session::settings::def(opennotebook_session::settings::STYLE_KEY).unwrap();
        assert!(
            opennotebook_sdk::styles::STYLES
                .iter()
                .any(|s| s.id == d.default)
        );
    }

    #[test]
    fn a_choice_is_described_with_its_options() {
        let d =
            opennotebook_session::settings::def(opennotebook_session::settings::SPEAKER_COUNT_KEY)
                .unwrap();
        let v = super::item(d, crate::settings_impl::setting(d, "2", None));
        assert_eq!(v["kind"], "choice");
        assert_eq!(v["value"], "2");
        assert_eq!(v["options"].as_array().unwrap().len(), 3);
        assert_eq!(v["group"], "");
        assert_eq!(v["advanced"], false);
    }

    /// The page reads these names; a rename here is a blank dialog there.
    /// `min` and `max` are left out when absent, which the page reads as none.
    #[test]
    fn a_model_item_keeps_the_fields_the_page_reads() {
        let d = opennotebook_session::settings::def(opennotebook_session::settings::CHAT_MODEL_KEY)
            .unwrap();
        let v = super::item(d, crate::settings_impl::setting(d, "", None));
        for k in [
            "key",
            "tab",
            "label",
            "help",
            "kind",
            "options",
            "suggestions",
            "default",
            "value",
            "group",
            "unit",
            "advanced",
            "model",
            "price",
        ] {
            assert!(v.get(k).is_some(), "{k} is missing");
        }
        assert_eq!(v["kind"], "text");
        assert_eq!(v["model"], true);
        assert!(!v["suggestions"].as_array().unwrap().is_empty());
    }
}
