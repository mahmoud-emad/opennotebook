//! The `settings` domain: the studio's defaults as tools.
//!
//! The same catalogue and the same store as the settings dialog
//! (`settings_api`): `opennotebook_session::settings::CATALOGUE` says what exists,
//! `settings.toml` in the data directory holds what was changed. A change made here reaches
//! the next build and the next question without a restart. Both surfaces
//! describe and check a setting through the functions below, so they cannot
//! disagree about what a value means or whether it is allowed.

use async_trait::async_trait;

use opennotebook_sdk::styles::STYLES;
use opennotebook_session::settings::{self as store, CATALOGUE, Def, Kind, TAB_INFO};

use crate::estimate::Prices;
use crate::settings::{
    Setting, SettingOption, SettingTab, SettingsGetInput, SettingsGetOutput, SettingsServiceApi,
    SettingsSetInput, SettingsSetOutput, Style, StylesListInput, StylesListOutput,
};

pub struct SettingsService;

type Ctx = opennotebook_api::RequestContext;
type RpcError = opennotebook_api::RpcError;

#[async_trait]
impl SettingsServiceApi for SettingsService {
    async fn settings_get(
        &self,
        _ctx: &Ctx,
        _input: SettingsGetInput,
    ) -> Result<SettingsGetOutput, RpcError> {
        let (tabs, settings) = described().await.map_err(RpcError::internal)?;
        Ok(SettingsGetOutput { tabs, settings })
    }

    async fn settings_set(
        &self,
        _ctx: &Ctx,
        input: SettingsSetInput,
    ) -> Result<SettingsSetOutput, RpcError> {
        let req = input.req;
        let (setting, note) = save(&req.key, &req.value).await.map_err(|e| match e {
            SaveError::Refused(m) => RpcError::invalid_params(m),
            SaveError::Store(m) => RpcError::internal(m),
        })?;
        Ok(SettingsSetOutput { setting, note })
    }

    async fn styles_list(
        &self,
        _ctx: &Ctx,
        _input: StylesListInput,
    ) -> Result<StylesListOutput, RpcError> {
        Ok(StylesListOutput {
            styles: STYLES
                .iter()
                .map(|s| Style {
                    id: s.id.to_string(),
                    label: s.label.to_string(),
                    blurb: s.blurb.to_string(),
                })
                .collect(),
        })
    }
}

/// Why a save did not happen: the value is not allowed, or the store failed.
#[derive(Debug)]
pub(crate) enum SaveError {
    Refused(String),
    Store(String),
}

impl std::fmt::Display for SaveError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SaveError::Refused(m) | SaveError::Store(m) => f.write_str(m),
        }
    }
}

/// Every tab and every setting with its stored override, priced where it is a
/// model and the catalogue answers.
pub(crate) async fn described() -> Result<(Vec<SettingTab>, Vec<Setting>), String> {
    let current = store::all().await?;
    let prices = crate::estimate_live::catalogue().await;
    let settings = CATALOGUE
        .iter()
        .map(|d| {
            let value = current
                .iter()
                .find(|c| c.key == d.key)
                .map(|c| c.value.clone())
                .unwrap_or_default();
            setting(d, &value, prices.as_ref())
        })
        .collect();
    Ok((tabs(), settings))
}

pub(crate) fn tabs() -> Vec<SettingTab> {
    TAB_INFO
        .iter()
        .map(|t| SettingTab {
            id: t.id.to_string(),
            label: t.label.to_string(),
            note: t.note.to_string(),
            advanced: t.advanced,
        })
        .collect()
}

/// Check and store one setting. Returns it as it now reads, and a note when
/// the save went through with a caveat.
pub(crate) async fn save(key: &str, value: &str) -> Result<(Setting, String), SaveError> {
    let Some(d) = store::def(key) else {
        return Err(SaveError::Refused(format!(
            "no setting named `{key}`; settings_get lists them"
        )));
    };
    let v = value.trim();
    let note = check(d, v).await.map_err(SaveError::Refused)?;
    store::set(d.key, v).await.map_err(SaveError::Store)?;
    let stored = if v == d.default { "" } else { v };
    let prices = if matches!(d.kind, Kind::Model(_)) {
        crate::estimate_live::catalogue().await
    } else {
        None
    };
    Ok((setting(d, stored, prices.as_ref()), note))
}

/// Whether `v` may be stored for `d`; `Ok` carries a note for the person,
/// empty when there is nothing to say.
///
/// A model id that is not one of the tested ones is checked against
/// the AI endpoint's catalogue. When the catalogue cannot be read the id is
/// kept and the note says it was not checked: an endpoint that is down is no
/// reason to lose a setting someone typed on purpose.
async fn check(d: &Def, v: &str) -> Result<String, String> {
    store::validate(d, v)?;
    if v.is_empty() {
        return Ok(String::new());
    }
    match d.kind {
        Kind::Style if !STYLES.iter().any(|s| s.id == v) => {
            Err(format!("`{v}` is not a slide style; styles_list has them"))
        }
        Kind::Model(tested) if !tested.iter().any(|(id, _)| *id == v) => {
            match crate::estimate_live::model_listed(v).await {
                Ok(true) => Ok(String::new()),
                Ok(false) => Err(format!(
                    "`{v}` is not a model the AI endpoint offers; check the id, e.g. `{}`",
                    d.default
                )),
                Err(why) => Ok(format!(
                    "Saved, but `{v}` could not be checked because the model catalogue is unreachable ({why})."
                )),
            }
        }
        _ => Ok(String::new()),
    }
}

/// One setting as both surfaces describe it.
pub(crate) fn setting(d: &Def, value: &str, prices: Option<&Prices>) -> Setting {
    let opt = |value: &str, label: &str, hint: String| SettingOption {
        value: value.to_string(),
        label: label.to_string(),
        hint,
    };
    let price = |id: &str| {
        prices
            .and_then(|p| p.get(id))
            .map(crate::estimate_live::price_hint)
            .unwrap_or_default()
    };
    let (kind, options, range) = match d.kind {
        Kind::Choice(opts) => (
            "choice",
            opts.iter().map(|(v, l)| opt(v, l, String::new())).collect(),
            None,
        ),
        Kind::Style => (
            "choice",
            STYLES
                .iter()
                .map(|s| opt(s.id, s.label, String::new()))
                .collect(),
            None,
        ),
        Kind::Number { min, max } => ("number", Vec::new(), Some((min, max))),
        Kind::Toggle => ("toggle", Vec::new(), None),
        Kind::Text(_) => ("text", Vec::new(), None),
        // Text on the wire, so a page that knows only text still edits it.
        Kind::Model(tested) => (
            "text",
            tested.iter().map(|(v, l)| opt(v, l, price(v))).collect(),
            None,
        ),
    };
    let model = matches!(d.kind, Kind::Model(_));
    let effective = if value.is_empty() { d.default } else { value };
    Setting {
        key: d.key.to_string(),
        tab: d.tab.to_string(),
        group: d.group.to_string(),
        label: d.label.to_string(),
        help: d.help.to_string(),
        kind: kind.to_string(),
        value: value.to_string(),
        default: d.default.to_string(),
        options,
        min: range.map(|r| r.0),
        max: range.map(|r| r.1),
        unit: d.unit.to_string(),
        advanced: d.advanced,
        model,
        price: if model {
            price(effective)
        } else {
            String::new()
        },
    }
}

/// The free-text suggestions a text setting offers: a model's tested ids.
pub(crate) fn suggestions(d: &Def) -> Vec<&'static str> {
    match d.kind {
        Kind::Text(s) => s.to_vec(),
        Kind::Model(m) => m.iter().map(|(id, _)| *id).collect(),
        _ => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::estimate::Price;

    #[test]
    fn a_model_setting_is_text_with_priced_tested_options() {
        let d = store::def(store::SCRIPT_MODEL_KEY).unwrap();
        let mut p = Prices::new();
        p.insert(
            store::SCRIPT_MODEL_DEFAULT.to_string(),
            Price {
                prompt: 0.000001,
                completion: 0.000005,
            },
        );
        let s = setting(d, "", Some(&p));
        assert_eq!(s.kind, "text");
        assert!(s.model && s.advanced);
        assert_eq!(s.group, "Writing");
        assert_eq!(s.price, "$1 / $5 per M tokens");
        let haiku = s
            .options
            .iter()
            .find(|o| o.value == store::SCRIPT_MODEL_DEFAULT)
            .unwrap();
        assert_eq!(haiku.hint, "$1 / $5 per M tokens");
        assert!(suggestions(d).contains(&store::SCRIPT_MODEL_DEFAULT));
    }

    #[test]
    fn a_number_carries_its_unit_and_range() {
        let d = store::def(store::SLIDE_COUNT_KEY).unwrap();
        let s = setting(d, "", None);
        assert_eq!((s.min, s.max), (Some(3), Some(12)));
        assert_eq!(s.unit, "slides");
        assert!(!s.model && !s.advanced && s.price.is_empty());
    }

    #[tokio::test]
    async fn a_tested_model_and_bad_values_are_judged_without_the_catalogue() {
        let d = store::def(store::SLIDE_MODEL_KEY).unwrap();
        assert_eq!(
            check(d, "anthropic/claude-sonnet-5.5").await,
            Ok(String::new())
        );
        assert!(check(d, "has spaces in it").await.is_err());
        let style = store::def(store::STYLE_KEY).unwrap();
        assert!(check(style, "no-such-style").await.is_err());
        assert_eq!(tabs().len(), store::TABS.len());
    }
}
