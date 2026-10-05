//! Settings: the theme, which is this browser's, and the studio's own
//! settings, drawn from what the server sends.
//!
//! The dialog's tabs, groups, labels, help, control kind and choices all come
//! from `opennotebook_session::settings::CATALOGUE`, so this page carries no
//! second copy of the list. Every change saves the moment it is made, as
//! ChatGPT's and Claude's settings do; there is no Save button to forget.
//! Values live in `settings.toml` in the studio's data directory, and an
//! environment variable overrides any of them.
//!
//! The settings are read once per page into a context ([`Settings`]) so the
//! app's contextual hints ("5 slides · about 5 min · Host and Expert") cost
//! nothing to draw, and are read again when the dialog closes. A hint links to
//! the tab that changes it through [`SettingsLink`], which opens the dialog on
//! that tab.

use dioxus::prelude::*;
use opennotebook_sdk::styles::STYLES;

use crate::api::{api_base, get_text, post_json, storage};
use crate::{Icon, thumb_url};

/// The setting keys the app reads for its hints and defaults. The server's
/// catalogue is the authority; these only name what the page looks up.
pub(crate) mod keys {
    pub(crate) const STYLE: &str = "OPENNOTEBOOK_STYLE";
    pub(crate) const SLIDE_COUNT: &str = "OPENNOTEBOOK_SLIDE_COUNT";
    pub(crate) const SESSION_MINUTES: &str = "OPENNOTEBOOK_SESSION_MINUTES";
    pub(crate) const AUDIO_FORMAT: &str = "OPENNOTEBOOK_AUDIO_FORMAT";
    pub(crate) const AUDIO_LENGTH: &str = "OPENNOTEBOOK_AUDIO_LENGTH";
    pub(crate) const RESEARCH_DEPTH: &str = "OPENNOTEBOOK_RESEARCH_DEPTH";
    pub(crate) const AUTO_NAME: &str = "OPENNOTEBOOK_AUTO_NAME";
    pub(crate) const COVERS: &str = "OPENNOTEBOOK_COVERS";
    pub(crate) const SPEAKER_COUNT: &str = "OPENNOTEBOOK_SPEAKER_COUNT";
    pub(crate) const SPEAKER1_NAME: &str = "OPENNOTEBOOK_SPEAKER1_NAME";
    pub(crate) const SPEAKER2_NAME: &str = "OPENNOTEBOOK_SPEAKER2_NAME";
    pub(crate) const LANGUAGE: &str = "OPENNOTEBOOK_LANGUAGE";
    pub(crate) const CHAT_MODEL: &str = "OPENNOTEBOOK_CHAT_MODEL";
    pub(crate) const SHOW_COST: &str = "OPENNOTEBOOK_SHOW_COST";
}

/// The viewer's theme: dark, the studio's default, or light. Chosen in
/// Settings, Appearance, and kept in this browser under the key the player
/// reads too, so both pages agree; applied as Bootstrap's own `data-bs-theme`
/// on <html>. The index page applies a stored choice before the first paint
/// (opennotebook_sdk::theme::BOOT_SCRIPT).
#[derive(Clone, Copy, PartialEq, Debug)]
pub(crate) enum ThemePref {
    Dark,
    Light,
}

/// The theme in force, for what the page's CSS cannot restyle: a cover is
/// drawn by the server in the theme its address asks for, so it is fetched
/// again when this changes.
pub(crate) static THEME: GlobalSignal<ThemePref> = Signal::global(ThemePref::load);

impl ThemePref {
    pub(crate) fn load() -> Self {
        match storage()
            .and_then(|s| {
                s.get_item(opennotebook_sdk::theme::STORAGE_KEY)
                    .ok()
                    .flatten()
            })
            .as_deref()
        {
            Some("light") => ThemePref::Light,
            _ => ThemePref::Dark,
        }
    }
    /// The name the server reads, as in a cover's address.
    pub(crate) fn wire(self) -> &'static str {
        match self {
            ThemePref::Dark => "dark",
            ThemePref::Light => "light",
        }
    }
    pub(crate) fn label(self) -> &'static str {
        match self {
            ThemePref::Dark => "Dark",
            ThemePref::Light => "Light",
        }
    }
    pub(crate) fn icon(self) -> &'static str {
        match self {
            ThemePref::Dark => "moon-stars",
            ThemePref::Light => "sun",
        }
    }
    pub(crate) fn apply(self) {
        *THEME.write() = self;
        let key = opennotebook_sdk::theme::STORAGE_KEY;
        let root = web_sys::window()
            .and_then(|w| w.document())
            .and_then(|d| d.document_element());
        if let Some(root) = root {
            let _ = match self {
                ThemePref::Light => root.set_attribute("data-bs-theme", "light"),
                ThemePref::Dark => root.remove_attribute("data-bs-theme"),
            };
        }
        if let Some(st) = storage() {
            let _ = match self {
                ThemePref::Light => st.set_item(key, "light"),
                ThemePref::Dark => st.remove_item(key),
            };
        }
    }
}

// ── what the server sends ────────────────────────────────────────────────────

#[derive(Clone, PartialEq, Default, serde::Deserialize)]
pub(crate) struct SettingOpt {
    value: String,
    label: String,
    /// A model's price; empty for every other choice.
    #[serde(default)]
    hint: String,
}

#[derive(Clone, PartialEq, Default, serde::Deserialize)]
pub(crate) struct SettingItem {
    key: String,
    /// The tab's label, one of `tab_info`'s.
    tab: String,
    /// A heading inside the tab; empty for none.
    #[serde(default)]
    group: String,
    label: String,
    help: String,
    kind: String,
    #[serde(default)]
    options: Vec<SettingOpt>,
    #[serde(default)]
    suggestions: Vec<String>,
    min: Option<i64>,
    max: Option<i64>,
    /// What a number counts ("slides", "min").
    #[serde(default)]
    unit: String,
    /// A model id: the tested `options` plus any id the catalogue lists.
    #[serde(default)]
    model: bool,
    /// The price of the model in effect; empty when unknown.
    #[serde(default)]
    price: String,
    default: String,
    /// The stored override; empty means the default applies.
    value: String,
}

impl SettingItem {
    pub(crate) fn effective(&self) -> String {
        if self.value.is_empty() {
            self.default.clone()
        } else {
            self.value.clone()
        }
    }

    /// A value as the person reads it: its choice's label, On or Off.
    fn shown(&self, v: &str) -> String {
        if self.kind == "toggle" {
            return if v == "off" { "Off" } else { "On" }.into();
        }
        if let Some(o) = self.options.iter().find(|o| o.value == v) {
            return o.label.clone();
        }
        // A model the catalogue offers no label for: its id made readable.
        if self.model && !v.is_empty() {
            return crate::dialogs::model_name(v);
        }
        if !self.unit.is_empty() {
            return format!("{v} {}", self.unit);
        }
        v.to_string()
    }
}

/// One tab as the server describes it.
#[derive(Clone, PartialEq, Default, serde::Deserialize)]
pub(crate) struct TabInfo {
    id: String,
    label: String,
    #[serde(default)]
    note: String,
    #[serde(default)]
    advanced: bool,
}

#[derive(Clone, PartialEq, Default, serde::Deserialize)]
pub(crate) struct SettingsDoc {
    #[serde(default)]
    ok: bool,
    #[serde(default)]
    error: String,
    #[serde(default)]
    tab_info: Vec<TabInfo>,
    #[serde(default)]
    items: Vec<SettingItem>,
}

impl SettingsDoc {
    /// A setting's effective value, or `None` when it is not in the document.
    pub(crate) fn value(&self, key: &str) -> Option<String> {
        self.items
            .iter()
            .find(|i| i.key == key)
            .map(|i| i.effective())
    }

    /// A setting's effective value as the person reads it.
    pub(crate) fn shown(&self, key: &str) -> Option<String> {
        self.items
            .iter()
            .find(|i| i.key == key)
            .map(|i| i.shown(&i.effective()))
    }
}

/// What a reply the page cannot parse is called: the studio and this page
/// are from different builds, most likely.
const UNREADABLE: &str = "The studio's answer could not be read. Reload the page and try again.";

async fn settings_load() -> Result<SettingsDoc, String> {
    let txt = get_text(&format!("{}/settings", api_base())).await?;
    let doc: SettingsDoc = serde_json::from_str(&txt).map_err(|_| UNREADABLE.to_string())?;
    if doc.ok {
        Ok(doc)
    } else {
        Err(crate::errors::readable(&doc.error))
    }
}

// ── the page's copy, shared ─────────────────────────────────────────────────

/// The settings as this page knows them, and which Settings tab is open.
#[derive(Clone, Copy, PartialEq)]
pub(crate) struct Settings {
    pub(crate) doc: Signal<SettingsDoc>,
    /// The first read has answered, well or not.
    pub(crate) loaded: Signal<bool>,
    pub(crate) err: Signal<String>,
    /// The dialog's tab id while it is open.
    pub(crate) open: Signal<Option<String>>,
}

impl Settings {
    /// A setting's effective value once the settings are read; `None` before,
    /// so a hint never shows a value that may not be the one in force.
    pub(crate) fn get(&self, key: &str) -> Option<String> {
        self.doc.read().value(key)
    }
    pub(crate) fn shown(&self, key: &str) -> Option<String> {
        self.doc.read().shown(key)
    }
    /// A toggle: on unless it reads `off`, and on while unknown.
    pub(crate) fn on(&self, key: &str) -> bool {
        self.get(key).as_deref() != Some("off")
    }
}

/// Read the settings into the page's copy.
pub(crate) async fn reload(s: Settings) {
    let Settings {
        mut doc,
        mut loaded,
        mut err,
        ..
    } = s;
    match settings_load().await {
        Ok(d) => {
            doc.set(d);
            err.set(String::new());
        }
        Err(e) => err.set(e),
    }
    loaded.set(true);
}

/// Provide the page's settings and read them once. Called by the app's root.
pub(crate) fn use_settings_provider() -> Settings {
    let s = use_context_provider(|| Settings {
        doc: Signal::new(SettingsDoc::default()),
        loaded: Signal::new(false),
        err: Signal::new(String::new()),
        open: Signal::new(None),
    });
    use_hook(move || {
        spawn(reload(s));
    });
    s
}

/// The page's settings.
pub(crate) fn use_settings() -> Settings {
    use_context::<Settings>()
}

/// The tab ids the app links to, with their labels for while the settings are
/// still on their way. The server's `tab_info` is the authority.
const TAB_LABELS: [(&str, &str); 7] = [
    (APPEARANCE, "Appearance"),
    ("defaults", "Generation defaults"),
    ("voices", "Voices"),
    ("language", "Language"),
    ("conversation", "Live conversation"),
    ("costs", "Costs & limits"),
    ("models", "Models"),
];

/// A Settings tab's glyph, by id. One per tab and no two alike.
pub(crate) fn tab_glyph(id: &str) -> &'static str {
    match id {
        APPEARANCE => "circle-half",
        "defaults" => "sliders",
        "voices" => "people",
        "language" => "translate",
        "conversation" => "chat-dots",
        "costs" => "coin",
        "models" => "cpu",
        _ => "gear",
    }
}

/// The Settings tab for how the studio looks in this browser.
pub(crate) const APPEARANCE: &str = "appearance";

/// "Settings › Voices": a link that opens Settings on that tab.
#[component]
pub(crate) fn SettingsLink(
    /// A tab id from `tab_info`, or `appearance`.
    tab: &'static str,
    /// The words to show; "Settings › <tab>" when absent.
    #[props(default)]
    text: Option<String>,
) -> Element {
    let s = use_settings();
    let mut open = s.open;
    let label = s
        .doc
        .read()
        .tab_info
        .iter()
        .find(|t| t.id == tab)
        .map(|t| t.label.clone())
        .or_else(|| {
            TAB_LABELS
                .iter()
                .find(|t| t.0 == tab)
                .map(|t| t.1.to_string())
        })
        .unwrap_or_else(|| "Settings".into());
    let text = text.unwrap_or_else(|| format!("Settings › {label}"));
    rsx! {
        button {
            class: "link-btn set-link",
            title: "Open Settings on {label}",
            onclick: move |_| open.set(Some(tab.to_string())),
            "{text}"
        }
    }
}

// ── the dialog ───────────────────────────────────────────────────────────────

/// The id of the Settings dialog's one tab panel.
const SET_PANEL: &str = "set-panel";

/// A Settings tab's element id.
fn tab_el(id: &str) -> String {
    format!("set-tab-{id}")
}

/// The tabs in order: Appearance, then the server's.
fn all_tabs(d: &SettingsDoc) -> Vec<TabInfo> {
    std::iter::once(TabInfo {
        id: APPEARANCE.into(),
        label: "Appearance".into(),
        note: String::new(),
        advanced: false,
    })
    .chain(d.tab_info.iter().cloned())
    .collect()
}

/// Rows in their catalogue order, gathered under their group headings.
fn grouped(rows: Vec<SettingItem>) -> Vec<(String, Vec<SettingItem>)> {
    let mut out: Vec<(String, Vec<SettingItem>)> = Vec::new();
    for r in rows {
        match out.last_mut() {
            Some((g, v)) if *g == r.group => v.push(r),
            _ => out.push((r.group.clone(), vec![r])),
        }
    }
    out
}

/// What a save says under its control: saving, saved, saved with a caveat, or
/// why it was refused.
#[derive(Clone, PartialEq)]
pub(crate) enum SaveState {
    Saving,
    Saved,
    Note(String),
    Failed(String),
}

#[component]
pub(crate) fn SettingsDialog(on_close: EventHandler<()>) -> Element {
    let s = use_settings();
    let mut doc = s.doc;
    let loaded = s.loaded;
    let load_err = s.err;
    let mut open = s.open;
    // The tab shown, by id. Starts where the link that opened it pointed.
    let mut tab = use_signal(|| {
        open.peek()
            .clone()
            .unwrap_or_else(|| "defaults".to_string())
    });
    let mut status = use_signal(std::collections::HashMap::<String, SaveState>::new);

    // Read again on opening: another tab or an agent may have changed them.
    use_hook(move || {
        spawn(reload(s));
    });

    let save = move |key: String, value: String| {
        spawn(async move {
            status.write().insert(key.clone(), SaveState::Saving);
            let body = serde_json::json!({ "key": key, "value": value }).to_string();
            let res = post_json(&format!("{}/settings", api_base()), &body).await;
            let outcome = res.and_then(|txt| {
                let v: serde_json::Value =
                    serde_json::from_str(&txt).map_err(|_| UNREADABLE.to_string())?;
                if v["ok"].as_bool() == Some(true) {
                    let item = serde_json::from_value::<SettingItem>(v["item"].clone())
                        .map_err(|_| UNREADABLE.to_string())?;
                    Ok((item, v["note"].as_str().unwrap_or("").to_string()))
                } else {
                    Err(crate::errors::readable(
                        v["error"].as_str().unwrap_or("the studio refused it"),
                    ))
                }
            });
            let st = match outcome {
                Ok((item, note)) => {
                    if let Some(slot) = doc.write().items.iter_mut().find(|i| i.key == item.key) {
                        *slot = item;
                    }
                    if note.is_empty() {
                        SaveState::Saved
                    } else {
                        SaveState::Note(note)
                    }
                }
                Err(e) => SaveState::Failed(e),
            };
            status.write().insert(key, st);
        });
    };

    let d = doc.read().clone();
    let tabs = all_tabs(&d);
    let mut current = tab.read().clone();
    // A link to a tab this server does not have lands on the first one.
    if *loaded.read() && !tabs.iter().any(|t| t.id == current) {
        current = tabs
            .get(1)
            .map(|t| t.id.clone())
            .unwrap_or_else(|| APPEARANCE.into());
    }
    let info = tabs
        .iter()
        .find(|t| t.id == current)
        .cloned()
        .unwrap_or_else(|| TabInfo {
            id: current.clone(),
            label: TAB_LABELS
                .iter()
                .find(|t| t.0 == current)
                .map_or("Settings", |t| t.1)
                .to_string(),
            ..Default::default()
        });
    let one_speaker = d.value(keys::SPEAKER_COUNT).as_deref() == Some("1");
    let groups = grouped(
        d.items
            .iter()
            .filter(|i| i.tab == info.label)
            .cloned()
            .collect(),
    );
    // In the order they are drawn: the advanced ones last.
    let ids: Vec<String> = tabs
        .iter()
        .filter(|t| !t.advanced)
        .chain(tabs.iter().filter(|t| t.advanced))
        .map(|t| t.id.clone())
        .collect();
    let close = move |_| {
        open.set(None);
        on_close.call(());
    };

    rsx! {
        div { class: "set-veil over", onclick: close }
        div {
            class: "set-dialog over",
            role: "dialog",
            aria_modal: "true",
            aria_label: "Settings",
            tabindex: "-1",
            // Focus moves into the dialog so Escape and Tab work from the start.
            onmounted: move |e| { spawn(async move { let _ = e.set_focus(true).await; }); },
            onkeydown: move |e: Event<KeyboardData>| {
                if e.key() == Key::Escape {
                    open.set(None);
                    on_close.call(());
                }
            },
            nav { class: "set-nav",
                h2 { "Settings" }
                // Up and down move between the tabs, as in any vertical
                // tablist; only the selected tab is in the Tab order.
                div {
                    class: "set-tabs",
                    role: "tablist",
                    aria_label: "Settings",
                    "aria-orientation": "vertical",
                    onkeydown: {
                        let ids = ids.clone();
                        move |e: Event<KeyboardData>| {
                            let n = ids.len().max(1);
                            let at = ids.iter().position(|i| *i == *tab.peek()).unwrap_or(0);
                            let to = match e.key() {
                                Key::ArrowDown => (at + 1) % n,
                                Key::ArrowUp => (at + n - 1) % n,
                                Key::Home => 0,
                                Key::End => n - 1,
                                _ => return,
                            };
                            e.prevent_default();
                            if let Some(id) = ids.get(to) {
                                tab.set(id.clone());
                                crate::focus_id(&tab_el(id));
                            }
                        }
                    },
                    for t in tabs.iter().filter(|t| !t.advanced).cloned() {
                        TabButton { key: "{t.id}", t: t.clone(), on: t.id == current, tab }
                    }
                    // The advanced tabs under their own heading, after the rest.
                    if tabs.iter().any(|t| t.advanced) {
                        div { class: "set-tabs-h", role: "presentation", "Advanced" }
                    }
                    for t in tabs.iter().filter(|t| t.advanced).cloned() {
                        TabButton { key: "{t.id}", t: t.clone(), on: t.id == current, tab }
                    }
                }
            }
            section {
                id: SET_PANEL,
                class: "set-body",
                role: "tabpanel",
                aria_labelledby: tab_el(&current),
                div { class: "set-head",
                    h3 { "{info.label}" }
                    button { class: "icon-btn", aria_label: "Close settings", title: "Close", onclick: close, Icon { name: "x-lg" } }
                }
                if !info.note.is_empty() {
                    p { class: "set-tnote", Icon { name: "info-circle" } span { "{info.note}" } }
                }
                if current == APPEARANCE {
                    div { class: "set-row",
                        div { class: "set-text",
                            div { class: "set-label", "Theme" }
                            div { class: "set-help", "Applies here and in the player." }
                        }
                        div { class: "set-ctl",
                            div { class: "seg", role: "radiogroup", aria_label: "Theme",
                                for t in [ThemePref::Dark, ThemePref::Light] {
                                    button {
                                        key: "{t.label()}",
                                        class: if *THEME.read() == t { "on" } else { "" },
                                        role: "radio",
                                        "aria-checked": if *THEME.read() == t { "true" } else { "false" },
                                        onclick: move |_| t.apply(),
                                        Icon { name: t.icon() }
                                        "{t.label()}"
                                    }
                                }
                            }
                        }
                    }
                } else if !*loaded.read() && d.items.is_empty() {
                    div { class: "set-note", span { class: "mini-spin" } " Loading settings…" }
                } else if !load_err.read().is_empty() && d.items.is_empty() {
                    div { class: "set-note err", role: "alert",
                        "Settings could not be loaded: {load_err}"
                        div {
                            button { class: "est-retry", onclick: move |_| { spawn(reload(s)); }, "Try again" }
                        }
                    }
                } else {
                    for (g, rows) in groups {
                        div {
                            key: "g-{g}",
                            class: if one_speaker && g == "Second voice" { "set-grp off" } else { "set-grp" },
                            if !g.is_empty() {
                                h4 { class: "set-gh", "{g}" }
                            }
                            if one_speaker && g == "Second voice" {
                                p { class: "set-gnote",
                                    "Decks use one voice while Speakers in a deck is One. "
                                    "Deep Dive, Critique and Debate still use this one."
                                }
                            }
                            for item in rows {
                                SettingRow {
                                    key: "{item.key}",
                                    item: item.clone(),
                                    status: status.read().get(&item.key).cloned(),
                                    on_save: move |(k, v): (String, String)| save(k, v),
                                }
                            }
                        }
                    }
                }
                if current == APPEARANCE {
                    p { class: "set-foot", "Kept in this browser and applied at once." }
                } else {
                    p { class: "set-foot",
                        "Saved as soon as you change it, for the next thing you make or open."
                    }
                }
            }
        }
    }
}

/// One tab in the Settings tablist.
#[component]
fn TabButton(t: TabInfo, on: bool, tab: Signal<String>) -> Element {
    let mut tab = tab;
    rsx! {
        button {
            id: tab_el(&t.id),
            class: if on { "set-tab on" } else { "set-tab" },
            role: "tab",
            tabindex: if on { "0" } else { "-1" },
            aria_selected: if on { "true" } else { "false" },
            "aria-controls": SET_PANEL,
            onclick: { let id = t.id.clone(); move |_| tab.set(id.clone()) },
            span { class: "set-glyph", Icon { name: tab_glyph(&t.id) } }
            "{t.label}"
        }
    }
}

/// The value a model row's select shows when the id is not a tested one.
const CUSTOM: &str = "\u{0}custom";

#[component]
pub(crate) fn SettingRow(
    item: SettingItem,
    status: Option<SaveState>,
    on_save: EventHandler<(String, String)>,
) -> Element {
    let key = item.key.clone();
    let eff = item.effective();
    let changed = !item.value.is_empty();
    let list_id = format!("sug-{}", item.key);
    // A model row: the person chose "Custom…" and is typing an id.
    let mut custom_open = use_signal(|| false);
    let tested = item.options.iter().any(|o| o.value == eff);
    let custom = item.model && (*custom_open.read() || !tested);
    let style_row = item.key == keys::STYLE;

    let control = if style_row {
        // The slide styles as their sample slides, as the Create panel shows them.
        let k = key.clone();
        rsx! {
            div { class: "style-grid set-styles", role: "radiogroup", aria_label: "{item.label}",
                for st in STYLES.iter() {
                    button {
                        key: "{st.id}",
                        class: if eff == st.id { "style on" } else { "style" },
                        role: "radio",
                        "aria-checked": if eff == st.id { "true" } else { "false" },
                        title: "{st.blurb}",
                        onclick: { let k = k.clone(); move |_| on_save.call((k.clone(), st.id.to_string())) },
                        span { class: "sw", style: "background-image:url({thumb_url(st.id)})" }
                        span { class: "style-n", "{st.label}" }
                    }
                }
            }
        }
    } else if item.model {
        let k = key.clone();
        let k2 = key.clone();
        rsx! {
            div { class: "set-model",
                select {
                    class: "set-input",
                    aria_label: "{item.label}",
                    onchange: move |e| {
                        let v = e.value();
                        if v == CUSTOM {
                            custom_open.set(true);
                            crate::focus_id(&format!("cm-{k}"));
                        } else {
                            custom_open.set(false);
                            on_save.call((k.clone(), v));
                        }
                    },
                    for o in item.options.clone() {
                        option {
                            key: "{o.value}",
                            value: "{o.value}",
                            selected: !custom && o.value == eff,
                            if o.hint.is_empty() { "{o.label}" } else { "{o.label} · {o.hint}" }
                        }
                    }
                    option { value: CUSTOM, selected: custom, "Custom…" }
                }
                if custom {
                    input {
                        id: "cm-{item.key}",
                        class: "set-input",
                        r#type: "text",
                        aria_label: "{item.label}, a model id",
                        list: "{list_id}",
                        spellcheck: "false",
                        placeholder: "provider/model-id",
                        value: if tested { String::new() } else { eff.clone() },
                        onchange: move |e| {
                            let v = e.value().trim().to_string();
                            if !v.is_empty() {
                                on_save.call((k2.clone(), v));
                            }
                        },
                    }
                    datalist { id: "{list_id}",
                        for sgg in item.suggestions.clone() {
                            option { key: "{sgg}", value: "{sgg}" }
                        }
                    }
                }
            }
        }
    } else {
        match item.kind.as_str() {
            "choice" => {
                let k = key.clone();
                rsx! {
                    select {
                        class: "set-input",
                        aria_label: "{item.label}",
                        onchange: move |e| on_save.call((k.clone(), e.value())),
                        for o in item.options.clone() {
                            option { key: "{o.value}", value: "{o.value}", selected: o.value == eff, "{o.label}" }
                        }
                    }
                }
            }
            "toggle" => {
                let on = eff != "off";
                let k = key.clone();
                rsx! {
                    button {
                        class: if on { "switch on" } else { "switch" },
                        role: "switch",
                        aria_checked: if on { "true" } else { "false" },
                        aria_label: "{item.label}",
                        onclick: move |_| on_save.call((k.clone(), if on { "off".into() } else { "on".into() })),
                        span { class: "knob" }
                    }
                }
            }
            "number" => {
                let k = key.clone();
                let range = match (item.min, item.max) {
                    (Some(a), Some(b)) => format!("{a} to {b}"),
                    _ => String::new(),
                };
                rsx! {
                    input {
                        class: "set-input narrow",
                        r#type: "number",
                        aria_label: if item.unit.is_empty() { item.label.clone() } else { format!("{}, in {}", item.label, item.unit) },
                        title: "{range}",
                        min: item.min.map(|m| m.to_string()).unwrap_or_default(),
                        max: item.max.map(|m| m.to_string()).unwrap_or_default(),
                        value: "{eff}",
                        onchange: move |e| on_save.call((k.clone(), e.value())),
                    }
                    if !item.unit.is_empty() {
                        span { class: "set-unit", "{item.unit}" }
                    }
                }
            }
            _ => {
                let k = key.clone();
                rsx! {
                    input {
                        class: "set-input",
                        r#type: "text",
                        aria_label: "{item.label}",
                        list: "{list_id}",
                        // The value in force, default included, so a field never
                        // looks empty when something applies. Saving the default
                        // back removes the override on the server.
                        placeholder: "Uses the default",
                        value: "{eff}",
                        onchange: move |e| on_save.call((k.clone(), e.value())),
                    }
                    if !item.suggestions.is_empty() {
                        datalist { id: "{list_id}",
                            for sgg in item.suggestions.clone() {
                                option { key: "{sgg}", value: "{sgg}" }
                            }
                        }
                    }
                }
            }
        }
    };
    let (st_class, st_text) = match &status {
        Some(SaveState::Saving) => ("set-st", "Saving…".to_string()),
        Some(SaveState::Saved) => ("set-st ok", "Saved".to_string()),
        Some(SaveState::Note(n)) => ("set-st warn", n.clone()),
        Some(SaveState::Failed(e)) => ("set-st err", e.clone()),
        None => ("set-st", String::new()),
    };
    let default_shown = item.shown(&item.default);
    let reset = rsx! {
        if changed {
            button {
                class: "set-reset",
                title: "Back to the default: {default_shown}",
                onclick: move |_| {
                    custom_open.set(false);
                    on_save.call((key.clone(), String::new()));
                },
                Icon { name: "arrow-counterclockwise" }
                "Reset"
            }
        }
    };
    rsx! {
        div { class: if style_row { "set-row wide" } else { "set-row" },
            div { class: "set-text",
                div { class: "set-label", "{item.label}" }
                if !item.help.is_empty() {
                    div { class: "set-help", "{item.help}" }
                }
                if item.model && !item.price.is_empty() {
                    div { class: "set-meta num", "In use: {item.price}" }
                }
                if changed {
                    div { class: "set-meta", "Default: {default_shown}" }
                }
                if !st_text.is_empty() {
                    div { class: "{st_class}", role: if st_class.ends_with("err") { "alert" } else { "status" }, "{st_text}" }
                }
            }
            div { class: "set-ctl",
                {control}
                {reset}
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(key: &str, group: &str) -> SettingItem {
        SettingItem {
            key: key.into(),
            group: group.into(),
            ..Default::default()
        }
    }

    /// Rows keep their order, and a group is one block under one heading.
    #[test]
    fn rows_are_grouped_in_order() {
        let g = grouped(vec![
            item("a", ""),
            item("b", "Host"),
            item("c", "Host"),
            item("d", "Second voice"),
        ]);
        let shape: Vec<(String, usize)> = g.iter().map(|(n, v)| (n.clone(), v.len())).collect();
        assert_eq!(
            shape,
            vec![
                (String::new(), 1),
                ("Host".into(), 2),
                ("Second voice".into(), 1)
            ]
        );
    }

    /// Every tab the app links to has its own glyph, distinct from the rest.
    #[test]
    fn every_tab_has_its_own_glyph() {
        let glyphs: Vec<&str> = TAB_LABELS.iter().map(|t| tab_glyph(t.0)).collect();
        assert!(!glyphs.contains(&"gear"), "{glyphs:?}");
        let mut uniq = glyphs.clone();
        uniq.sort();
        uniq.dedup();
        assert_eq!(uniq.len(), glyphs.len(), "{glyphs:?}");
    }

    /// The server's answer as it is sent today reads into the page's shape.
    #[test]
    fn the_server_document_reads() {
        let doc: SettingsDoc = serde_json::from_str(
            r#"{"ok":true,"tabs":["Models"],
               "tab_info":[{"id":"models","label":"Models","note":"Changing these affects quality and cost.","advanced":true}],
               "items":[{"key":"OPENNOTEBOOK_CHAT_MODEL","tab":"Models","group":"Chat & answers","label":"Chat & Ask model",
                         "help":"h","kind":"text","value":"","default":"google/gemini-2.5-flash-lite",
                         "options":[{"value":"google/gemini-2.5-flash-lite","label":"Gemini 2.5 Flash Lite","hint":"$0.1 / $0.4 per M tokens"}],
                         "suggestions":["google/gemini-2.5-flash-lite"],"unit":"","advanced":true,"model":true,"price":"$0.1 / $0.4 per M tokens"}]}"#,
        )
        .unwrap();
        assert!(doc.tab_info[0].advanced);
        assert_eq!(
            doc.shown(keys::CHAT_MODEL).as_deref(),
            Some("Gemini 2.5 Flash Lite")
        );
        assert!(doc.items[0].model && doc.items[0].min.is_none());
    }

    /// A model with no label in the catalogue is named from its id, not shown
    /// raw.
    #[test]
    fn a_model_without_a_label_reads_as_a_name() {
        let doc: SettingsDoc = serde_json::from_str(
            r#"{"ok":true,"items":[{"key":"OPENNOTEBOOK_CHAT_MODEL","tab":"Models","label":"Chat & Ask model",
                         "help":"h","kind":"text","value":"amazon/nova-micro-v1","default":"google/gemini-2.5-flash-lite",
                         "options":[{"value":"google/gemini-2.5-flash-lite","label":"Gemini 2.5 Flash Lite"}],
                         "model":true}]}"#,
        )
        .unwrap();
        assert_eq!(
            doc.shown(keys::CHAT_MODEL).as_deref(),
            Some("Nova Micro V1")
        );
    }
}
