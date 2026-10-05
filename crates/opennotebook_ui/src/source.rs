//! A source, opened from a citation: its text in a drawer at the side of the
//! page, the cited passage marked and scrolled into view. NotebookLM's
//! citation click, and the same from the chat, a mind map's answers and study
//! notes, since every chip is drawn by `mindmap::with_chips`.

use dioxus::prelude::*;

use crate::Icon;
use crate::api::{api_base, get_text, gloo_sleep};
use crate::chat::md_to_html;

/// A citation clicked: which source, and the passage it cites.
#[derive(Clone, PartialEq, Debug)]
pub(crate) struct Opened {
    pub(crate) name: String,
    pub(crate) passage: String,
}

/// The chips are HTML the app does not draw element by element, so one
/// listener on the document hears a click, or Enter, on any of them and
/// passes its source and passage over. Replaced, not added to, when the page
/// mounts again: the old listener's channel is gone with its page.
const CITE_OPEN_JS: &str = "\
if(window.__citeOpen){document.removeEventListener('click',window.__citeOpen,true);\
document.removeEventListener('keydown',window.__citeKey,true);}\
const pick=e=>e.target&&e.target.closest&&e.target.closest('.cite[data-src]');\
const go=c=>dioxus.send({name:c.dataset.src,x:c.dataset.x||''});\
window.__citeOpen=e=>{const c=pick(e);if(!c)return;e.preventDefault();go(c);};\
window.__citeKey=e=>{if(e.key!=='Enter'&&e.key!==' ')return;const c=pick(e);if(!c)return;e.preventDefault();go(c);};\
document.addEventListener('click',window.__citeOpen,true);\
document.addEventListener('keydown',window.__citeKey,true);";

/// The source a citation opened, set whenever a chip on the page is clicked.
pub(crate) fn use_cite_open() -> Signal<Option<Opened>> {
    let mut opened = use_signal(|| None::<Opened>);
    use_future(move || async move {
        let mut ev = document::eval(CITE_OPEN_JS);
        while let Ok(v) = ev.recv::<serde_json::Value>().await {
            let name = v["name"].as_str().unwrap_or_default().to_string();
            if !name.is_empty() {
                opened.set(Some(Opened {
                    name,
                    passage: v["x"].as_str().unwrap_or_default().to_string(),
                }));
            }
        }
    });
    opened
}

/// What `source/read` returns.
#[derive(Clone, PartialEq, serde::Deserialize)]
struct Text {
    title: String,
    #[serde(default)]
    url: String,
    text: String,
}

/// A source's paragraphs, each with whether the passage holds it.
///
/// A passage is whole paragraphs of its source, trimmed and joined by blank
/// lines (`grounding::pieces_of`), so a paragraph is marked when it is one of
/// the passage's. Should none match exactly, which a source changed since the
/// answer would do, the paragraphs that hold the passage's opening words are
/// marked instead, so the reader still lands near it.
pub(crate) fn marked_paragraphs(text: &str, passage: &str) -> Vec<(String, bool)> {
    let paras: Vec<&str> = text
        .split("\n\n")
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .collect();
    let wanted: Vec<&str> = passage
        .split("\n\n")
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .collect();
    let mut marks: Vec<bool> = paras.iter().map(|p| wanted.contains(p)).collect();
    if !marks.iter().any(|m| *m) {
        let squash = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
        let opening: String = squash(passage).chars().take(60).collect();
        if !opening.is_empty() {
            marks = paras.iter().map(|p| squash(p).contains(&opening)).collect();
        }
    }
    paras
        .into_iter()
        .zip(marks)
        .map(|(p, m)| (p.to_string(), m))
        .collect()
}

/// The drawer: the source's title and page, then its text with the passage
/// marked. Escape or the close button shuts it.
#[component]
pub(crate) fn SourceDrawer(cid: String, opened: Opened, on_close: EventHandler<()>) -> Element {
    let mut got = use_signal(|| None::<Result<Text, String>>);
    let name = opened.name.clone();
    use_effect(use_reactive!(|(cid, name)| {
        spawn(async move {
            got.set(None);
            let url = format!(
                "{}/source/read?session={}&name={}",
                api_base(),
                js_sys::encode_uri_component(&cid),
                js_sys::encode_uri_component(&name),
            );
            let r = get_text(&url)
                .await
                .and_then(|j| serde_json::from_str::<Text>(&j).map_err(|e| e.to_string()))
                .map_err(|e| crate::errors::readable(&e));
            got.set(Some(r));
        });
    }));
    // To the passage once the text is in, and again when another chip of the
    // same source names another passage.
    let passage = opened.passage.clone();
    use_effect(use_reactive!(|(passage,)| {
        let _ = passage;
        if got.read().as_ref().is_some_and(|r| r.is_ok()) {
            spawn(async move {
                gloo_sleep(0).await;
                let _ = document::eval(
                    "document.querySelector('.srcv-body .mark')?.scrollIntoView({block:'center'})",
                );
            });
        }
    }));
    use_hook(|| crate::focus_id("srcv-close"));

    let body = match got.read().as_ref() {
        None => {
            rsx! { div { class: "srcv-msg", span { class: "mini-spin" } "Reading the source…" } }
        }
        Some(Err(e)) => {
            rsx! { div { class: "srcv-msg bad", role: "alert", "This source could not be opened. {e}" } }
        }
        Some(Ok(t)) => {
            let paras = marked_paragraphs(&t.text, &opened.passage);
            rsx! {
                for (i, (p, mark)) in paras.into_iter().enumerate() {
                    div {
                        key: "{i}",
                        class: if mark { "md mark" } else { "md" },
                        dangerous_inner_html: md_to_html(&p),
                    }
                }
            }
        }
    };
    let (title, url) = match got.read().as_ref() {
        Some(Ok(t)) => (t.title.clone(), t.url.clone()),
        _ => (String::new(), String::new()),
    };
    rsx! {
        aside {
            class: "srcv",
            role: "dialog",
            aria_label: "Source",
            onkeydown: move |e| if e.key() == Key::Escape { on_close.call(()) },
            div { class: "srcv-head",
                div { class: "srcv-t",
                    div { class: "srcv-n", title: "{title}", if title.is_empty() { "Source" } else { "{title}" } }
                    if !url.is_empty() {
                        a { class: "srcv-u", href: "{url}", target: "_blank", rel: "noopener noreferrer",
                            Icon { name: "link-45deg" }
                            "Open the page"
                        }
                    }
                }
                button {
                    id: "srcv-close",
                    class: "icon-btn",
                    title: "Close (Esc)",
                    aria_label: "Close the source",
                    onclick: move |_| on_close.call(()),
                    Icon { name: "x-lg" }
                }
            }
            div { class: "srcv-body", {body} }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::marked_paragraphs;

    #[test]
    fn the_passages_paragraphs_are_marked_and_nothing_else() {
        let text = "# Pyramids\n\nSource: https://w.org\n\nRamps were used to haul blocks up.\n\n\
                    Menu\n\nWorkforce estimates vary widely.\n\nThe end.";
        let passage = "Ramps were used to haul blocks up.\n\nWorkforce estimates vary widely.";
        let m: Vec<bool> = marked_paragraphs(text, passage)
            .into_iter()
            .map(|p| p.1)
            .collect();
        assert_eq!(m, [false, false, true, false, true, false]);
    }

    #[test]
    fn a_passage_that_drifted_still_lands_near_its_words() {
        let text = "Intro.\n\nRamps   were used to haul\nblocks up the side, it is thought.";
        let m: Vec<bool> = marked_paragraphs(text, "Ramps were used to haul blocks up the side")
            .into_iter()
            .map(|p| p.1)
            .collect();
        assert_eq!(m, [false, true]);
    }
}
