//! The studio's own dialogs: the confirm/prompt every screen asks through, and
//! the itemised cost of a build before it runs.

use dioxus::prelude::*;
use opennotebook_sdk::session::{CostLine, SessionEstimateOutput};

use crate::settings::SettingsLink;
use crate::{Icon, style_label};

/// Money for a person, not a ledger. Sub-cent amounts keep two significant
/// digits ($0.042, $0.0004) so a paid step never rounds to a "$0.00" that reads
/// as free; that is kept for steps that really are free.
pub(crate) fn usd(x: f64) -> String {
    if x <= 0.0 {
        return "$0.00".into();
    }
    if x < 0.0001 {
        return "< $0.0001".into();
    }
    if x >= 1.0 {
        return format!("${x:.2}");
    }
    let mag = x.log10().floor() as i32;
    let decimals = ((1 - mag).max(2)) as usize;
    format!("${x:.decimals$}")
}

/// A range when the ends really differ, one "about" figure when they do not.
pub(crate) fn usd_range(low: f64, high: f64) -> String {
    if high <= 0.0 {
        return "$0.00".into();
    }
    if high <= low * 1.5 {
        format!("about {}", usd((low + high) / 2.0))
    } else {
        format!("{} – {}", usd(low), usd(high))
    }
}

/// The same range as a screen reader should say it.
pub(crate) fn usd_range_spoken(low: f64, high: f64) -> String {
    let r = usd_range(low, high).replace("< ", "less than ");
    match r.split_once(" – ") {
        Some((a, b)) => format!("between {a} and {b}"),
        None => r,
    }
}

/// 850, 12.4k, 1.2M: a count someone can take in at a glance.
pub(crate) fn count_short(n: i64) -> String {
    let f = n as f64;
    if n >= 1_000_000 {
        format!("{:.1}M", f / 1e6)
    } else if n >= 10_000 {
        format!("{:.0}k", f / 1e3)
    } else if n >= 1_000 {
        format!("{:.1}k", f / 1e3)
    } else {
        n.to_string()
    }
}

/// A slide model id as a person reads it: `anthropic/claude-haiku-4.5` →
/// "Claude Haiku 4.5".
pub(crate) fn model_name(id: &str) -> String {
    let name = id.rsplit('/').next().unwrap_or(id);
    name.split('-')
        .map(|w| {
            let mut c = w.chars();
            match c.next() {
                Some(f) if f.is_ascii_alphabetic() => {
                    f.to_uppercase().collect::<String>() + c.as_str()
                }
                Some(f) => f.to_string() + c.as_str(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Every step of a build and what it costs, before it runs.
///
/// The shape follows what people look for in a checkout: the total first, then
/// the lines grouped by phase in the order they happen, free steps listed with
/// their reason rather than dropped, then how the number was reached. The
/// build button repeats the amount, so pressing it is agreeing to that.
#[component]
pub(crate) fn CostDialog(
    est: Option<SessionEstimateOutput>,
    /// An audio overview rather than slides, as its format and length read
    /// ("Deep Dive · shorter"): its facts, and what to cut to fit the limit.
    audio: Option<String>,
    loading: bool,
    err: String,
    on_close: EventHandler<()>,
    on_retry: EventHandler<()>,
    on_build: EventHandler<()>,
) -> Element {
    let mut prices = use_signal(|| false);
    let groups: Vec<(String, Vec<CostLine>)> = est
        .as_ref()
        .map(|e| {
            let mut g: Vec<(String, Vec<CostLine>)> = Vec::new();
            for l in &e.lines {
                match g.iter_mut().find(|(k, _)| *k == l.group) {
                    Some((_, v)) => v.push(l.clone()),
                    None => g.push((l.group.clone(), vec![l.clone()])),
                }
            }
            g
        })
        .unwrap_or_default();
    rsx! {
        div { class: "set-veil", onclick: move |_| on_close.call(()) }
        div {
            class: "set-dialog est-dialog",
            role: "dialog",
            aria_modal: "true",
            aria_labelledby: "est-title",
            tabindex: "-1",
            onmounted: move |e| { spawn(async move { let _ = e.set_focus(true).await; }); },
            onkeydown: move |e: Event<KeyboardData>| {
                if e.key() == Key::Escape {
                    on_close.call(());
                }
            },
            div { class: "set-head est-head",
                h3 { id: "est-title", "Estimated cost" }
                button { class: "icon-btn", aria_label: "Close", title: "Close", onclick: move |_| on_close.call(()), Icon { name: "x-lg" } }
            }
            div { class: "est-body",
                if loading && est.is_none() {
                    div { class: "set-note", span { class: "mini-spin" } " Working out what this build will cost…" }
                } else if !err.is_empty() {
                    div { class: "set-note err",
                        "The cost could not be estimated: {err}"
                        div { button { class: "est-retry", onclick: move |_| on_retry.call(()), "Try again" } }
                    }
                } else if let Some(e) = est.clone() {
                    div { class: "est-total",
                        div { class: "est-amt", aria_label: "{usd_range_spoken(e.total_low_usd, e.total_high_usd)}",
                            "{usd_range(e.total_low_usd, e.total_high_usd)}"
                        }
                        div { class: "est-sub",
                            "USD · most likely about "
                            strong { "{usd(e.total_typical_usd)}" }
                            if loading { span { class: "dim", " · updating…" } }
                        }
                    }
                    div { class: "est-facts",
                        if let Some(a) = audio.as_ref() {
                            span { class: "est-fact", "{a}" }
                        } else {
                            span { class: "est-fact", "{e.slides} slides" }
                        }
                        if e.minutes > 0 { span { class: "est-fact", "about {e.minutes} minutes" } }
                        span { class: "est-fact", if e.speakers == 1 { "1 voice" } else { "{e.speakers} voices" } }
                        span { class: "est-fact",
                            if e.sources == 1 { "1 source" } else { "{e.sources} sources" }
                            " · {count_short(e.source_chars)} characters"
                        }
                        if audio.is_none() {
                            span { class: "est-fact", "{style_label(&e.style)} style" }
                            span { class: "est-fact", "slides by {model_name(&e.slides_tier)}" }
                        }
                    }
                    if e.over_limit {
                        LimitNote { e: e.clone(), audio: audio.is_some(), class: "set-note err est-limit" }
                    } else if e.limit_usd > 0.0 {
                        div { class: "est-fine est-limit", "Within your {usd(e.limit_usd)} limit per output. "
                            SettingsLink { tab: "costs" } }
                    }
                    for (group, lines) in groups {
                        {
                            let sub_low: f64 = lines.iter().map(|l| l.cost_low_usd).sum();
                            let sub_high: f64 = lines.iter().map(|l| l.cost_high_usd).sum();
                            let all_free = lines.iter().all(|l| l.free);
                            rsx! {
                                section { key: "{group}", class: "est-group",
                                    div { class: "est-gh",
                                        h4 { "{group}" }
                                        span { class: "est-gsum",
                                            if all_free { "Free" } else { "{usd_range(sub_low, sub_high)}" }
                                        }
                                    }
                                    table { class: "est-tbl",
                                        colgroup {
                                            col { class: "c-step" }
                                            col { class: "c-model" }
                                            col { class: "c-tok" }
                                            col { class: "c-cost" }
                                        }
                                        thead {
                                            tr {
                                                th { scope: "col", "Step" }
                                                th { scope: "col", class: "est-model-h", "Model" }
                                                th { scope: "col", class: "num est-tok-h", "Tokens" }
                                                th { scope: "col", class: "num", "Cost" }
                                            }
                                        }
                                        tbody {
                                            for l in lines {
                                                CostRow { key: "{l.step}", l: l.clone(), prices: *prices.read() }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    label { class: "est-toggle",
                        input {
                            r#type: "checkbox",
                            checked: *prices.read(),
                            onchange: move |ev| prices.set(ev.checked()),
                        }
                        "Show unit prices"
                    }
                    details { class: "est-how",
                        summary { "How this was worked out" }
                        ul {
                            for a in e.assumptions.iter() {
                                li { key: "{a}", "{a}" }
                            }
                        }
                    }
                }
            }
            div { class: "est-foot",
                span { class: "est-fine",
                    "An estimate, not a quote: you pay for what the models actually use."
                    if let Some(e) = est.as_ref() {
                        " Prices as of {e.priced_at.get(..10).unwrap_or_default()}."
                    }
                }
                div { class: "est-actions",
                    button { onclick: move |_| on_close.call(()), "Close" }
                    button {
                        class: "primary",
                        disabled: est.as_ref().is_none_or(|e| e.over_limit),
                        onclick: move |_| on_build.call(()),
                        {
                            match est.as_ref() {
                                // A model with no price counts as $0, so any
                                // figure would understate it.
                                Some(e) if any_unpriced(e) => "Build (cost unknown)".to_string(),
                                Some(e) if e.total_typical_usd > 0.0 => format!("Build for about {}", usd(e.total_typical_usd)),
                                Some(_) => "Build (free)".to_string(),
                                None => "Start building".to_string(),
                            }
                        }
                    }
                }
            }
        }
    }
}

/// Whether a step's model has no price in the catalog. It is counted as $0,
/// so the total is then a floor, not an estimate.
pub(crate) fn any_unpriced(e: &SessionEstimateOutput) -> bool {
    e.lines.iter().any(|l| l.unpriced)
}

/// Why a build over the spending limit would be refused, and what to change,
/// in the server's own words (`estimate_live::over_limit_message`), up to the
/// link to the setting that raises it.
pub(crate) fn limit_lead(e: &SessionEstimateOutput, audio: bool) -> String {
    let fewer = if audio {
        "a shorter length"
    } else {
        "fewer slides, a shorter length"
    };
    format!(
        "This could cost up to {}, over your {} limit. Use {fewer}, or raise the limit in ",
        usd_up(e.total_high_usd),
        usd(e.limit_usd)
    )
}

/// Money rounded up to the next whole cent, for an amount stated against a
/// limit: $0.2525 over a $0.25 limit reads $0.26, never "$0.25, over $0.25".
/// The slack keeps float noise from adding a cent.
pub(crate) fn usd_up(x: f64) -> String {
    format!("${:.2}", (x * 100.0 - 1e-6).ceil() / 100.0)
}

/// A build over the spending limit: why it would be refused, what to change,
/// and the way to the limit itself. Shown whether or not costs are shown,
/// because it is the reason Generate is off.
#[component]
pub(crate) fn LimitNote(e: SessionEstimateOutput, audio: bool, class: &'static str) -> Element {
    rsx! {
        div { class, role: "alert",
            "{limit_lead(&e, audio)}"
            SettingsLink { tab: "costs" }
            "."
        }
    }
}

/// One step: what it does and why that count, the model and who calls it, the
/// tokens, the cost. A free step says why it is free.
#[component]
pub(crate) fn CostRow(l: CostLine, prices: bool) -> Element {
    let cost = if l.free {
        rsx! { span { class: "est-free", "Free" } }
    } else if l.unpriced {
        rsx! { span { class: "est-unpriced", title: "This model has no price in the catalog", "no price" } }
    } else {
        let label = if (l.cost_high_usd - l.cost_low_usd).abs() < 1e-9 {
            usd(l.cost_typical_usd)
        } else if l.cost_low_usd <= 0.0 {
            // Usually does not run at all; "$0.00 – $x" would read as free.
            format!("up to {}", usd(l.cost_high_usd))
        } else {
            format!("{} – {}", usd(l.cost_low_usd), usd(l.cost_high_usd))
        };
        rsx! { span { aria_label: "{usd_range_spoken(l.cost_low_usd, l.cost_high_usd)}", "{label}" } }
    };
    let calls = if l.calls_low == l.calls_high {
        format!("{}", l.calls_typical)
    } else {
        format!("{}–{}", l.calls_low, l.calls_high)
    };
    let out = if l.output_tokens_low == l.output_tokens_high {
        count_short(l.output_tokens_typical)
    } else {
        format!(
            "{}–{}",
            count_short(l.output_tokens_low),
            count_short(l.output_tokens_high)
        )
    };
    rsx! {
        tr { class: if l.free { "free" } else { "" },
            td {
                div { class: "est-step", "{l.step}" }
                div { class: "est-dt", "{l.detail}" }
                if prices && !l.free && !l.unpriced {
                    div { class: "est-price",
                        if let (Some(i), Some(o)) = (l.price_in_per_million, l.price_out_per_million) {
                            "${i:.2} / 1M in · ${o:.2} / 1M out"
                        }
                    }
                }
            }
            td { class: "est-model",
                if l.model.is_empty() { span { class: "dim", "—" } } else { code { title: "{l.model}", "{l.model}" } }
                div { class: "est-via", "{l.via}" }
            }
            td { class: "num est-tok",
                if l.free {
                    span { class: "dim", if l.calls_typical > 0 { "{calls} ×" } else { "—" } }
                } else {
                    div { "{count_short(l.input_tokens)} in" }
                    div { class: "dim", "{out} out" }
                    div { class: "est-calls", "{calls} call" if calls != "1" { "s" } }
                }
            }
            td { class: "num est-cost", {cost} }
        }
    }
}

/// A question put to the user in the studio's own dialog: a yes/no when
/// `input` is `None`, a one-line text answer when it holds the starting value.
#[derive(Clone)]
pub(crate) struct Ask {
    title: String,
    body: String,
    ok_label: String,
    danger: bool,
    input: Option<String>,
    on_ok: Callback<String>,
}

impl Ask {
    pub(crate) fn confirm(
        title: &str,
        body: &str,
        ok_label: &str,
        on_ok: impl FnMut(String) + 'static,
    ) -> Self {
        Ask {
            title: title.into(),
            body: body.into(),
            ok_label: ok_label.into(),
            danger: true,
            input: None,
            on_ok: Callback::new(on_ok),
        }
    }

    pub(crate) fn prompt(
        title: &str,
        current: &str,
        ok_label: &str,
        on_ok: impl FnMut(String) + 'static,
    ) -> Self {
        Ask {
            title: title.into(),
            body: String::new(),
            ok_label: ok_label.into(),
            danger: false,
            input: Some(current.into()),
            on_ok: Callback::new(on_ok),
        }
    }
}

/// Open the shared dialog. The App owns it, so any screen can ask.
pub(crate) fn ask(a: Ask) {
    consume_context::<Signal<Option<Ask>>>().set(Some(a));
}

/// Whether keyboard focus is on a button (or a link), whose own Enter press
/// the browser turns into a click.
pub(crate) fn focus_on_button() -> bool {
    web_sys::window()
        .and_then(|w| w.document())
        .and_then(|d| d.active_element())
        .is_some_and(|el| matches!(el.tag_name().as_str(), "BUTTON" | "A"))
}

#[component]
pub(crate) fn AskDialog() -> Element {
    let mut state = use_context::<Signal<Option<Ask>>>();
    let mut value = use_signal(|| {
        state
            .peek()
            .as_ref()
            .and_then(|a| a.input.clone())
            .unwrap_or_default()
    });
    let Some(a) = state.read().clone() else {
        return rsx! {};
    };
    let is_prompt = a.input.is_some();
    let on_ok = a.on_ok;
    let mut close = move || state.set(None);
    let mut submit = move || {
        let v = value.read().clone();
        if is_prompt && v.trim().is_empty() {
            return;
        }
        state.set(None);
        on_ok.call(v);
    };
    rsx! {
        div { class: "set-veil", onclick: move |_| close() }
        div {
            class: "set-dialog ask-dialog",
            role: "alertdialog",
            aria_modal: "true",
            aria_labelledby: "ask-title",
            tabindex: "-1",
            onmounted: move |e| {
                if !is_prompt {
                    spawn(async move { let _ = e.set_focus(true).await; });
                }
            },
            // Enter confirms only from the dialog itself or its text field. On
            // a focused button it is that button's own press: Enter on Cancel
            // or Close must cancel, never delete.
            onkeydown: move |e: Event<KeyboardData>| match e.key() {
                Key::Escape => close(),
                Key::Enter if !focus_on_button() => {
                    e.prevent_default();
                    submit();
                }
                _ => {}
            },
            div { class: "set-head",
                h3 { id: "ask-title", "{a.title}" }
                button { class: "icon-btn", aria_label: "Close", title: "Close", onclick: move |_| close(), Icon { name: "x-lg" } }
            }
            if !a.body.is_empty() {
                p { class: "ask-body", "{a.body}" }
            }
            if is_prompt {
                input {
                    r#type: "text",
                    value: "{value}",
                    oninput: move |e| value.set(e.value()),
                    onmounted: move |e| { spawn(async move { let _ = e.set_focus(true).await; }); },
                }
            }
            div { class: "est-actions ask-actions",
                button { onclick: move |_| close(), "Cancel" }
                button {
                    class: if a.danger { "primary danger" } else { "primary" },
                    disabled: is_prompt && value.read().trim().is_empty(),
                    onclick: move |_| submit(),
                    "{a.ok_label}"
                }
            }
        }
    }
}

#[cfg(test)]
mod cost_format_tests {
    use super::{usd, usd_range, usd_range_spoken, usd_up};

    #[test]
    fn a_paid_step_never_reads_as_free() {
        assert_eq!(usd(0.0), "$0.00");
        assert_eq!(usd(0.00004), "< $0.0001");
        assert_eq!(usd(0.000412), "$0.00041");
        assert_eq!(usd(0.0421), "$0.042");
        assert_eq!(usd(0.72), "$0.72");
        assert_eq!(usd(1.456), "$1.46");
    }

    #[test]
    fn a_range_collapses_when_its_ends_are_close() {
        assert_eq!(usd_range(0.40, 0.50), "about $0.45");
        assert_eq!(usd_range(0.40, 1.60), "$0.40 – $1.60");
        assert_eq!(usd_range_spoken(0.40, 1.60), "between $0.40 and $1.60");
    }

    #[test]
    fn an_amount_over_a_limit_rounds_up_to_the_cent() {
        assert_eq!(usd_up(0.2525), "$0.26");
        assert_eq!(usd_up(0.25), "$0.25");
        assert_eq!(usd_up(0.8100000001), "$0.81");
    }
}
