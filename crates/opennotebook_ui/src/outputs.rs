//! Everything made from a collection, as the rows of its outputs list: a deck,
//! an audio overview, a map or a set of notes, each saying where it is.

use dioxus::prelude::*;
use opennotebook_sdk::mindmap::MindMapSummary;
use opennotebook_sdk::notes::StudyNotesSummary;
use opennotebook_sdk::session::{
    SessionDeleteInput, SessionGetInput, SessionPrepareInput, SessionPrepareReq,
    SessionRetitleInput, SessionRetitleReq, SessionSummary,
};
use wasm_bindgen::JsCast;
use wasm_bindgen::prelude::Closure;

use crate::api::{api_base, clean_rpc_error, client, still_there};
use crate::dialogs::{Ask, ask, usd};
use crate::home::coll_title;
use crate::{CardMenu, Icon, mindmap, mmss, notes, report, when};

/// An audio overview's format, as the person reads it.
pub(crate) fn audio_format_label(id: &str) -> &'static str {
    match id {
        "brief" => "Brief",
        "critique" => "Critique",
        "debate" => "Debate",
        _ => "Deep Dive",
    }
}

/// Start a failed output again, as a new output in the same collection.
///
/// The new one is prepared under a fresh sid FIRST, and the failed row is
/// deleted only once that has been accepted. The other way round (delete, then
/// prepare under the same sid) lost the failed row, its reason included, every
/// time the second call was refused: no sources any more, over the cost limit,
/// the studio down. `session_prepare` refuses a sid that already exists, which
/// is right, so the retry cannot reuse the old one while it is still there.
///
/// What is kept: the title, the collection, the style, the audio format,
/// length and focus, the slide count, and the session's OWN speakers, all read
/// back from it. Speakers are deliberately not re-read from settings —
/// somebody editing their voices between the failure and the retry should not
/// silently change what this output sounds like. Whatever the row does not
/// record (an older row has no style; a prep that failed before its outline
/// has no slides or speakers) is left out, and the server fills it from the
/// settings as it does for a new build.
///
/// `Ok(Some(why))` is a retry that started but left the failed row behind.
pub(crate) async fn retry_prep(
    sid: &str,
    title: &str,
    new_sid: &str,
) -> Result<Option<String>, String> {
    let c = client()?;

    let got = c
        .session_get(SessionGetInput {
            sid: sid.to_string(),
        })
        .await
        .map_err(|e| clean_rpc_error(&e.to_string()))?;
    let Some(old) = got.session.filter(|_| got.found) else {
        return Err("this output is gone".to_string());
    };
    // A failed audio overview is retried as the same audio overview, in the
    // same collection.
    let audio = old.audio.clone();
    let collection = old.collection.clone();
    let speakers = old.speakers;
    let style = old.style.filter(|s| !s.trim().is_empty());
    let slide_count = (audio.is_none() && !old.slides.is_empty()).then_some(old.slides.len() as _);

    c.session_prepare(SessionPrepareInput {
        req: SessionPrepareReq {
            sid: new_sid.to_string(),
            title: title.to_string(),
            resource_dir: String::new(),
            speakers,
            slide_count,
            style,
            research_topic: None,
            audio_format: audio.as_ref().map(|a| a.format.clone()),
            audio_length: audio.as_ref().map(|a| a.length.clone()),
            focus: audio.map(|a| a.focus).filter(|f| !f.is_empty()),
            collection,
        },
    })
    .await
    .map_err(|e| clean_rpc_error(&e.to_string()))?;

    // The new one is on its way; the failed one can go.
    let gone = c
        .session_delete(SessionDeleteInput {
            sid: sid.to_string(),
        })
        .await;
    Ok(match gone {
        Ok(_) => None,
        Err(e) => Some(format!(
            "It started again, but the failed one could not be removed: {}",
            clean_rpc_error(&e.to_string())
        )),
    })
}

/// The phases a prep runs, in order, named for a person.
pub(crate) const PHASES: [(&str, &str); 5] = [
    ("research", "Researching the web"),
    ("ingest", "Reading your sources"),
    ("script", "Writing the script"),
    ("deck", "Recording the voices, and drawing any slides"),
    ("validate", "Checking it renders"),
];

/// What a failed prep says to the person who started it, and what it keeps back.
///
/// Returns `(plain, detail)`. `plain` is the only thing shown by default: one
/// sentence, no ids, no internal nouns, and where possible the action that
/// fixes it. `detail` is the prep's own words, kept behind a disclosure for
/// whoever is debugging.
///
/// The split exists because the unsplit version shipped and was wrong. A person
/// who asked for a slide deck was shown ``deck `session` came back `failed`
/// after 600s (0/5 rendered) ... Check run_inspect and run_job_logs ... (prep
/// job 00ac)``: four internal identifiers, an instruction naming two RPCs they
/// cannot call, and no mention of the actual cause, which was an exhausted
/// billing account. Every word of that is for a maintainer.
pub(crate) fn prep_failure_text(raw: &str) -> (String, String) {
    let raw = raw.trim();
    if raw.is_empty() {
        return (
            "Something went wrong while making this. Your sources are kept.".to_string(),
            String::new(),
        );
    }

    let low = raw.to_ascii_lowercase();
    let plain =
        if low.contains("quota exhausted") || low.contains("more credits") || low.contains("402") {
            "The image service has run out of credit, so the slides could not be drawn. \
         Top up the account and try again."
        } else if low.contains("name conflict") && low.contains("theme") {
            "That visual style could not be applied. Pick a different style and try again."
        } else if low.contains("no extracted pairs")
            || low.contains("q&a door is empty")
            || low.contains("not a bot")
        {
            "Your sources could not be read. A link behind a sign-in or a bot check saves \
         the warning page instead of the document, so try a direct link or paste the \
         text in."
        } else if low.contains("timed out") || low.contains("timeout") {
            "The slides took too long to draw and this was stopped. Trying again \
         with fewer slides usually works."
        } else if low.contains("rendered") || low.contains("nothing rendered") {
            "The slides could not be drawn, so there is no deck to narrate. Your sources \
         are kept: try again, or change them."
        } else {
            "Something went wrong while making this. Your sources are kept, so you \
         can try again or change them."
        };

    (plain.to_string(), raw.to_string())
}

/// One thing made from the collection, for the outputs list: every kind in one
/// list, newest first.
#[derive(Clone, PartialEq)]
pub(crate) enum Made {
    Session(SessionSummary),
    Map(MindMapSummary),
    Notes(StudyNotesSummary),
}

impl Made {
    pub(crate) fn created_ms(&self) -> i64 {
        match self {
            Made::Session(s) => s.created_ms,
            Made::Map(m) => m.created_ms,
            Made::Notes(n) => n.created_ms,
        }
    }
    pub(crate) fn key(&self) -> String {
        match self {
            Made::Session(s) => format!("s-{}", s.sid),
            Made::Map(m) => format!("m-{}", m.id),
            Made::Notes(n) => format!("n-{}", n.id),
        }
    }
}

/// One output, by its kind and its id: what a rename or a delete acts on.
#[derive(Clone, PartialEq)]
pub(crate) enum Target {
    Session(String),
    Map(String),
    Notes(String),
}

impl Target {
    /// What it is, at the start of a sentence saying what went wrong with it.
    pub(crate) fn subject(&self) -> &'static str {
        match self {
            Target::Session(_) => "It",
            Target::Map(_) => "The mind map",
            Target::Notes(_) => "The study notes",
        }
    }
}

/// Rename one output of collection `cid`. `Err` says why it was not renamed,
/// including one that is no longer there.
pub(crate) async fn retitle(cid: String, target: &Target, title: String) -> Result<(), String> {
    match target {
        Target::Session(sid) => client()?
            .session_retitle(SessionRetitleInput {
                req: SessionRetitleReq {
                    sid: sid.clone(),
                    title,
                },
            })
            .await
            .map_err(|e| clean_rpc_error(&e.to_string()))
            .and_then(|o| still_there(o.value)),
        Target::Map(id) => mindmap::retitle(cid, id.clone(), title).await,
        Target::Notes(id) => notes::retitle(cid, id.clone(), title).await,
    }
}

/// Delete one output of collection `cid`. `Err` says why it was not.
pub(crate) async fn delete(cid: String, target: &Target) -> Result<(), String> {
    match target {
        Target::Session(sid) => client()?
            .session_delete(SessionDeleteInput { sid: sid.clone() })
            .await
            .map_err(|e| clean_rpc_error(&e.to_string()))
            .and_then(|o| still_there(o.value)),
        Target::Map(id) => mindmap::delete(cid, id.clone()).await,
        Target::Notes(id) => notes::delete(cid, id.clone()).await,
    }
}

/// A fresh output id, `s<epoch ms>` as every sid the studio mints, and never
/// one of the outputs already listed.
pub(crate) fn mint_sid(taken: &[SessionSummary]) -> String {
    let mut ms = js_sys::Date::now() as u64;
    while taken.iter().any(|s| s.sid == format!("s{ms}")) {
        ms += 1;
    }
    format!("s{ms}")
}

/// Something being made that answers in seconds: a map, a set of notes.
#[component]
pub(crate) fn PendingRow(icon: &'static str, text: &'static str) -> Element {
    rsx! {
        div { class: "out-row run",
            span { class: "out-i", Icon { name: icon } }
            div { class: "out-main",
                div { class: "out-t", "{text}" }
                div { class: "pbar wide", div { class: "pbar-run" } }
            }
        }
    }
}

/// An output's ⋯ menu: rename it, or delete it after asking. One for every
/// row of the outputs list, so all of them are acted on the same way.
#[component]
pub(crate) fn OutputMenu(
    /// Its name as stored, where a rename starts from; may be empty.
    title: String,
    /// Its name as the row shows it, for the menu's label and the question.
    shown: String,
    /// What a delete removes and what it keeps, said in the question.
    consequence: String,
    /// The new name, once the person has given one that differs.
    on_rename: EventHandler<String>,
    on_delete: EventHandler<()>,
) -> Element {
    let q = shown.clone();
    rsx! {
        CardMenu {
            label: shown,
            items: vec![("Rename".to_string(), false), ("Delete".to_string(), true)],
            on_pick: move |i: usize| {
                if i == 0 {
                    let was = title.trim().to_string();
                    ask(Ask::prompt("Rename", &title, "Save", move |next: String| {
                        let next = next.trim().to_string();
                        if !next.is_empty() && next != was {
                            on_rename.call(next);
                        }
                    }));
                } else {
                    ask(Ask::confirm(
                        &format!("Delete \u{201c}{q}\u{201d}?"),
                        &consequence,
                        "Delete",
                        move |_| on_delete.call(()),
                    ));
                }
            },
        }
    }
}

/// A failure in the outputs list that is not one output's own: a map or notes
/// that could not be made, a list that could not be read.
#[component]
pub(crate) fn ErrRow(
    #[props(default)] icon: &'static str,
    title: String,
    #[props(default)] detail: String,
    #[props(default)] on_dismiss: Option<EventHandler<()>>,
    #[props(default)] on_retry: Option<EventHandler<()>>,
) -> Element {
    rsx! {
        div { class: "out-row bad", role: "alert",
            if !icon.is_empty() {
                span { class: "out-i", Icon { name: icon } }
            }
            div { class: "out-main",
                div { class: "out-t", "{title}" }
                if !detail.is_empty() {
                    div { class: "out-d", "{detail}" }
                }
            }
            if let Some(retry) = on_retry {
                button { onclick: move |_| retry.call(()), Icon { name: "arrow-clockwise" } "Try again" }
            }
            if let Some(dismiss) = on_dismiss {
                button {
                    class: "icon-btn",
                    title: "Dismiss",
                    aria_label: "Dismiss",
                    onclick: move |_| dismiss.call(()),
                    Icon { name: "x-lg" }
                }
            }
        }
    }
}

/// What deleting a map or notes does, the verb agreeing with the kind: "The
/// mind map is removed", "The study notes are removed".
fn removed_line(what: &str) -> String {
    let verb = if what.ends_with("notes") { "are" } else { "is" };
    format!("The {what} {verb} removed. The sources and everything else made from them stay.")
}

/// A map or a set of notes in the outputs list: opens beside the Studio.
#[component]
pub(crate) fn ItemRow(
    icon: &'static str,
    /// What it is, for the delete question: "mind map", "study notes".
    what: &'static str,
    title: String,
    facts: String,
    when_ms: i64,
    /// Whether it is the one open in the viewer.
    on: bool,
    on_open: EventHandler<()>,
    /// The new name, once the person has given one that differs.
    on_rename: EventHandler<String>,
    on_delete: EventHandler<()>,
) -> Element {
    let shown = if title.trim().is_empty() {
        format!("Untitled {what}")
    } else {
        title.clone()
    };
    let q = shown.clone();
    rsx! {
        div { class: if on { "out-row on" } else { "out-row" },
            button { class: "out-open", onclick: move |_| on_open.call(()),
                span { class: "out-i", Icon { name: icon } }
                span { class: "out-main",
                    span { class: "out-t", "{shown}" }
                    span { class: "out-d num",
                        "{facts}"
                        if !when(when_ms).is_empty() { " · {when(when_ms)}" }
                    }
                }
            }
            OutputMenu {
                title,
                shown: q,
                consequence: removed_line(what),
                on_rename,
                on_delete,
            }
        }
    }
}

/// A deck or an audio overview in the outputs list. Ready, it opens in the
/// player; preparing, it follows the prep's steps live; failed, it says why
/// and offers the way out.
#[component]
pub(crate) fn SessionRow(
    s: SessionSummary,
    /// Whether this row follows its prep's event stream; see `LIVE_MAX`.
    live: bool,
    on_changed: EventHandler<()>,
    /// The new name, once the person has given one that differs.
    on_rename: EventHandler<String>,
    on_delete: EventHandler<()>,
) -> Element {
    let audio = s.kind == "audio";
    let icon = if audio { "soundwave" } else { "easel" };
    let shown = if s.title.trim().is_empty() {
        coll_title("")
    } else {
        s.title.clone()
    };
    let state = s.state.clone();
    let mut retrying = use_signal(|| false);
    // A failed prep's reason, read from the session itself.
    let mut why = use_signal(|| None::<(String, String)>);
    {
        let (sid, failed) = (s.sid.clone(), state == "failed");
        use_effect(use_reactive((&sid, &failed), move |(sid, failed)| {
            if !failed {
                return;
            }
            spawn(async move {
                let raw = match client() {
                    Ok(c) => c
                        .session_get(SessionGetInput { sid })
                        .await
                        .ok()
                        .and_then(|g| g.session.filter(|_| g.found))
                        .and_then(|s| s.failure)
                        .unwrap_or_default(),
                    Err(_) => String::new(),
                };
                why.set(Some(prep_failure_text(&raw)));
            });
        }));
    }

    let menu = rsx! {
        OutputMenu {
            title: s.title.clone(),
            shown: shown.clone(),
            consequence: "It is removed from this collection. The sources and everything else made from them stay.".to_string(),
            on_rename,
            on_delete,
        }
    };

    let body = rsx! {
        span { class: "out-i", Icon { name: icon } }
        span { class: "out-main",
            span { class: "out-t", "{shown}" }
            match state.as_str() {
                // Keyed apart, so a row that becomes live opens its stream.
                "preparing" if live => rsx! { Progress { key: "live-{s.sid}", sid: s.sid.clone() } },
                "preparing" => rsx! { Progress { key: "idle-{s.sid}", sid: String::new() } },
                "failed" => rsx! {
                    span { class: "out-d bad",
                        span { class: "badge failed", "Failed" }
                        " "
                        {why.read().as_ref().map(|w| w.0.clone()).unwrap_or_default()}
                    }
                },
                _ => rsx! {
                    span { class: "out-d num",
                        if audio {
                            "{audio_format_label(&s.audio_format)}"
                            if s.duration_ms > 0 { " · {mmss(s.duration_ms as i64)}" }
                        } else {
                            "{s.slide_count} slides · "
                            if s.speakers > 1 { "{s.speakers} voices" } else { "1 voice" }
                            if s.duration_ms > 0 { " · {mmss(s.duration_ms as i64)}" }
                        }
                        // What its making cost, when the prep recorded it;
                        // older outputs never did, and say nothing.
                        if s.spent_known {
                            " · "
                            span { title: "Model calls this build made; excludes indexing by the memory service",
                                "Spent {usd(s.spent_usd)}"
                            }
                        }
                        if !when(s.created_ms).is_empty() { " · {when(s.created_ms)}" }
                    }
                },
            }
        }
    };

    rsx! {
        div { class: match state.as_str() { "failed" => "out-row bad", "preparing" => "out-row run", _ => "out-row" },
            if state == "ready" {
                a {
                    class: "out-open",
                    href: "{api_base()}/player?session={s.sid}",
                    title: if audio { "Listen" } else { "Play" },
                    {body}
                    span { class: "out-play", Icon { name: "play-fill" } }
                }
            } else {
                div { class: "out-open", {body} }
            }
            if state == "failed" {
                button {
                    disabled: *retrying.read(),
                    onclick: {
                        let (sid, title) = (s.sid.clone(), s.title.clone());
                        move |_| {
                            if *retrying.peek() { return }
                            retrying.set(true);
                            let (sid, title) = (sid.clone(), title.clone());
                            spawn(async move {
                                match retry_prep(&sid, &title, &mint_sid(&[])).await {
                                    Ok(None) => {}
                                    Ok(Some(note)) => report(note),
                                    Err(e) => report(format!("It could not be started again: {e}")),
                                }
                                retrying.set(false);
                                on_changed.call(());
                            });
                        }
                    },
                    Icon { name: "arrow-clockwise" }
                    if *retrying.read() { "Starting…" } else { "Retry" }
                }
            }
            {menu}
        }
        if let Some((_, detail)) = why.read().as_ref().filter(|w| !w.1.is_empty()) {
            details { class: "whydetail out-why",
                summary { "Technical details" }
                pre { "{detail}" }
            }
        }
    }
}

/// How many preparing rows follow their prep live at once.
pub(crate) const LIVE_MAX: usize = 2;

/// A prep's progress: the step it is on and how far, from the studio's event
/// stream for that output. The stream belongs to the row and closes with it,
/// so a list of preparing outputs does not leave connections behind. An empty
/// `sid` follows nothing and shows only that it is preparing.
#[component]
pub(crate) fn Progress(sid: String) -> Element {
    let mut at = use_signal(|| (String::new(), 0u64, 0u64));
    // The stream and its listener, held together: the listener lives exactly
    // as long as the stream it listens to, rather than being leaked.
    type Listener = Closure<dyn FnMut(web_sys::MessageEvent)>;
    let es = use_hook(|| {
        if sid.is_empty() {
            return std::rc::Rc::new(None::<(web_sys::EventSource, Listener)>);
        }
        let es = web_sys::EventSource::new(&format!("{}/events?session={sid}", api_base())).ok();
        let held = es.map(|es| {
            let on_prog = Listener::new(move |e: web_sys::MessageEvent| {
                let Some(txt) = e.data().as_string() else {
                    return;
                };
                let Ok(v) = serde_json::from_str::<serde_json::Value>(&txt) else {
                    return;
                };
                at.set((
                    v["step"].as_str().unwrap_or_default().to_string(),
                    v["steps_done"].as_u64().unwrap_or(0),
                    v["steps_total"].as_u64().unwrap_or(0),
                ));
            });
            let _ = es.add_event_listener_with_callback(
                "prep.progress",
                on_prog.as_ref().unchecked_ref(),
            );
            (es, on_prog)
        });
        std::rc::Rc::new(held)
    });
    use_drop(move || {
        if let Some((es, _)) = es.as_ref() {
            es.close();
        }
    });
    let (step, done, total) = at.read().clone();
    let label = PHASES
        .iter()
        .find(|(k, _)| *k == step)
        .map_or("Starting", |(_, l)| *l);
    let pct = (done * 100).checked_div(total).unwrap_or(0);
    rsx! {
        span { class: "out-d num",
            span { class: "badge preparing", "Preparing" }
            if !sid.is_empty() { " {label}" }
            if total > 0 { " · {done}/{total}" }
        }
        // Indeterminate until a step count is known: no value to announce.
        span { class: "pbar wide", role: "progressbar", aria_label: "Progress",
            "aria-valuenow": (total > 0).then(|| pct.to_string()),
            "aria-valuemin": "0", "aria-valuemax": "100",
            if total > 0 {
                span { class: "pfill", style: "width: {pct}%" }
            } else {
                span { class: "pbar-run" }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::removed_line;

    #[test]
    fn the_delete_question_agrees_with_its_kind() {
        assert!(removed_line("study notes").starts_with("The study notes are removed."));
        assert!(removed_line("mind map").starts_with("The mind map is removed."));
    }
}
