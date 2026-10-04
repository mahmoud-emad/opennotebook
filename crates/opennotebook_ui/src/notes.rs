//! Study notes on the collection page: writing them, and the notes themselves
//! in the viewer beside the Studio.
//!
//! The notes are written and checked on the server
//! (`opennotebook_script::notes`); this file only reads them. Their `[n]`
//! markers become the same chips the chat's cited answers use
//! (`mindmap::with_chips`), so a citation looks and behaves the same wherever
//! it appears. See `docs/study-notes-spec.md`.

use dioxus::prelude::*;
use opennotebook_sdk::notes::{
    NotesCreateInput, NotesCreateReq, NotesDeleteInput, NotesEstimateInput, NotesEstimateOutput,
    NotesGetInput, NotesListInput, NotesRef, NotesRetitleInput, NotesRetitleReq, StudyNotes,
    StudyNotesSummary,
};

use crate::mindmap::{Cite, cite_groups, with_chips};

use crate::api::notes_client as client;

/// A create or get output as the notes it is, field for field; the server does
/// the same conversion the other way (`notes_impl::flat`).
fn as_notes<T: serde::Serialize>(out: &T) -> Result<StudyNotes, String> {
    serde_json::to_value(out)
        .and_then(serde_json::from_value)
        .map_err(|e| e.to_string())
}

fn clean_err(e: impl std::fmt::Display) -> String {
    crate::api::clean_rpc_error(&e.to_string())
}

// ── the state the page shares ────────────────────────────────────────────────

/// The collection page's study notes: the list, the options' focus, and notes
/// being written, shared by the Studio's options and its outputs list.
#[derive(Clone, Copy, PartialEq)]
pub struct NotesState {
    pub notes: Signal<Vec<StudyNotesSummary>>,
    pub focus: Signal<String>,
    pub making: Signal<bool>,
    pub err: Signal<String>,
    pub est: Signal<Option<NotesEstimateOutput>>,
    pub est_loading: Signal<bool>,
}

pub fn use_notes_state() -> NotesState {
    NotesState {
        notes: use_signal(Vec::new),
        focus: use_signal(String::new),
        making: use_signal(|| false),
        err: use_signal(String::new),
        est: use_signal(|| None),
        est_loading: use_signal(|| false),
    }
}

/// The collection's notes, newest first.
pub async fn load(sid: String, mut st: NotesState) {
    let got = async {
        client()?
            .notes_list(NotesListInput { sid })
            .await
            .map_err(clean_err)
    }
    .await;
    match got {
        Ok(out) => st.notes.set(out.notes),
        Err(e) => crate::report(format!("The study notes could not be loaded: {e}")),
    }
}

/// What notes of the collection would cost. Free: no model is called.
pub async fn estimate(sid: String, mut st: NotesState) {
    st.est_loading.set(true);
    let got = async {
        client()?
            .notes_estimate(NotesEstimateInput { sid })
            .await
            .map_err(clean_err)
    }
    .await;
    st.est.set(got.ok());
    st.est_loading.set(false);
}

/// Write notes of the collection with the focus typed in, and return their id to
/// open. `None` when it failed; the reason is in `st.err`.
pub async fn make(sid: String, mut st: NotesState) -> Option<String> {
    if *st.making.peek() {
        return None;
    }
    st.making.set(true);
    st.err.set(String::new());
    let f = st.focus.peek().trim().to_string();
    let made = async {
        let c = client()?;
        let out = c
            .notes_create(NotesCreateInput {
                req: NotesCreateReq {
                    sid: sid.clone(),
                    focus: (!f.is_empty()).then_some(f),
                    sources: None,
                },
            })
            .await
            .map_err(clean_err)?;
        let notes = as_notes(&out)?;
        let list = c
            .notes_list(NotesListInput { sid })
            .await
            .map_err(clean_err)?;
        Ok::<_, String>((notes, list.notes))
    }
    .await;
    st.making.set(false);
    match made {
        Ok((n, list)) => {
            st.notes.set(list);
            st.focus.set(String::new());
            Some(n.id)
        }
        Err(e) => {
            st.err.set(e);
            None
        }
    }
}

/// The newest notes made from exactly `sources` with no focus: the ones that
/// are already up to date, so writing more would say the same again.
pub fn covering_notes(notes: &[StudyNotesSummary], sources: &[String]) -> Option<String> {
    if sources.is_empty() {
        return None;
    }
    let want: std::collections::BTreeSet<&str> = sources.iter().map(String::as_str).collect();
    notes
        .iter()
        .filter(|n| n.focus.trim().is_empty())
        .find(|n| {
            n.sources
                .iter()
                .map(String::as_str)
                .collect::<std::collections::BTreeSet<_>>()
                == want
        })
        .map(|n| n.id.clone())
}

/// Delete one set of notes. `Err` when the service could not be reached or
/// refused, so a batch delete can say what it could not remove.
pub(crate) async fn delete(sid: String, id: String) -> Result<(), String> {
    client()?
        .notes_delete(NotesDeleteInput {
            req: NotesRef { sid, id },
        })
        .await
        .map_err(clean_err)
        .and_then(|o| crate::api::still_there(o.value))
}

/// Rename one set of notes. `Err` says why they were not renamed, including
/// notes that are no longer there.
pub(crate) async fn retitle(sid: String, id: String, title: String) -> Result<(), String> {
    let done = client()?
        .notes_retitle(NotesRetitleInput {
            req: NotesRetitleReq { sid, id, title },
        })
        .await
        .map_err(clean_err)?;
    if done.value {
        Ok(())
    } else {
        Err("the study notes are no longer there".into())
    }
}

/// "6 ideas · 10 questions · 18 terms", leaving out what is not there.
pub(crate) fn counts(ideas: i64, questions: i64, terms: i64) -> String {
    let part = |n: i64, one: &str, many: &str| match n {
        0 => None,
        1 => Some(format!("1 {one}")),
        n => Some(format!("{n} {many}")),
    };
    [
        part(ideas, "idea", "ideas"),
        part(questions, "question", "questions"),
        part(terms, "term", "terms"),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join(" · ")
}

// ── the notes ────────────────────────────────────────────────────────────────

/// Markdown with `[n]` markers as HTML with citation chips.
fn cited(md: &str, cites: &[Cite]) -> String {
    with_chips(&crate::chat::md_to_html(md), cites)
}

/// The same, for a line that is not a paragraph (a question, a definition):
/// the `<p>` the Markdown renderer wraps it in is taken off, so it sits inline.
fn cited_inline(md: &str, cites: &[Cite]) -> String {
    let html = cited(md, cites);
    let t = html.trim();
    match t.strip_prefix("<p>").and_then(|r| r.strip_suffix("</p>")) {
        Some(inner) if !inner.contains("<p>") => inner.to_string(),
        _ => html,
    }
}

/// Flip a citation's popover below its chip when there is no room above it.
///
/// The popover opens above the chip, and a chip near the top of a scrolling
/// pane had it cut off by the pane's edge: measured in the notes, a popover at
/// 4 px against a pane starting at 108 px. It is also kept inside its pane
/// sideways, narrowed when the pane is narrow, since a chip near an edge
/// opened its card half outside. Which way it opens is decided at
/// the moment it opens, from where the chip is, for every chip on the page —
/// the chat's included. Installed once.
pub fn install_cite_flip() {
    let _ = document::eval(
        "if(!window.__citeFlip){window.__citeFlip=true;\
         const f=e=>{const c=e.target&&e.target.closest&&e.target.closest('.cite');if(!c)return;\
         let top=0;for(let p=c.parentElement;p;p=p.parentElement){const o=getComputedStyle(p).overflowY;\
         if(o==='auto'||o==='scroll'){top=p.getBoundingClientRect().top;break;}}\
         const r=c.getBoundingClientRect();c.toggleAttribute('data-below',r.top-top<220);\
         let L=8,R=innerWidth-8;for(let p=c.parentElement;p;p=p.parentElement){const o=getComputedStyle(p).overflowX;\
         if(o!=='visible'){const b=p.getBoundingClientRect();L=Math.max(L,b.left+8);R=Math.min(R,b.right-8);break;}}\
         const w=Math.max(160,Math.min(320,R-L)),mid=r.left+r.width/2;\
         const x=Math.min(Math.max(mid-w/2,L),R-w);\
         c.style.setProperty('--pop-w',w+'px');c.style.setProperty('--pop-x',(x-(mid-w/2))+'px');};\
         document.addEventListener('mouseover',f,true);document.addEventListener('focusin',f,true);}",
    );
}

/// The notes open in the viewer: read top to bottom, with the quiz's
/// answers hidden until asked for, the way a study guide is used.
#[component]
pub fn NotesView(
    sid: String,
    id: String,
    /// Their name as the outputs list has it, which a rename changes while
    /// they are open. Empty until the list has loaded.
    title: String,
    on_close: EventHandler<()>,
) -> Element {
    let mut notes = use_signal(|| None::<StudyNotes>);
    let mut err = use_signal(String::new);
    // Which answers are showing, by question index.
    let mut shown = use_signal(std::collections::BTreeSet::<usize>::new);

    use_hook(install_cite_flip);
    // A rename while they are open shows here, and names the download.
    use_effect(use_reactive((&title,), move |(t,)| {
        let stale = notes.peek().as_ref().is_some_and(|n| n.title != t);
        if stale
            && !t.is_empty()
            && let Some(n) = notes.write().as_mut()
        {
            n.title = t;
        }
    }));
    use_effect(use_reactive!(|(sid, id)| {
        spawn(async move {
            notes.set(None);
            err.set(String::new());
            shown.set(Default::default());
            let got = async {
                let c = client()?;
                let out = c
                    .notes_get(NotesGetInput {
                        req: NotesRef { sid, id },
                    })
                    .await
                    .map_err(clean_err)?;
                as_notes(&out)
            }
            .await;
            match got {
                Ok(n) => notes.set(Some(n)),
                Err(e) => err.set(e),
            }
        });
    }));

    let n = notes.read().clone();
    // The list's name for them first, as the map's viewer does.
    let title = if title.trim().is_empty() {
        n.as_ref().map(|n| n.title.clone()).unwrap_or_default()
    } else {
        title.clone()
    };
    let cites: Vec<Cite> = n
        .as_ref()
        .map(|n| {
            n.citations
                .iter()
                .map(|c| Cite {
                    n: c.n as u32,
                    title: c.title.clone(),
                    url: c.url.clone(),
                    excerpt: c.excerpt.clone(),
                })
                .collect()
        })
        .unwrap_or_default();
    // What the person should know about how these were made, in one line.
    let note = n.as_ref().map(|n| {
        let mut parts = vec![match n.citations.len() {
            1 => "1 passage cited".to_string(),
            k => format!("{k} passages cited"),
        }];
        if n.dropped > 0 {
            parts.push(format!(
                "{} citation{} removed: the passage did not say it",
                n.dropped,
                if n.dropped == 1 { "" } else { "s" }
            ));
        }
        if n.unchecked {
            parts.push("citations not checked: the notes are not in English".to_string());
        }
        if n.excerpted {
            parts.push("written from excerpts of long sources".to_string());
        }
        if !n.focus.is_empty() {
            parts.push(format!("focus: {}", n.focus));
        }
        parts.join(" · ")
    });
    let all_shown = n
        .as_ref()
        .is_some_and(|n| !n.quiz.is_empty() && shown.read().len() == n.quiz.len());

    rsx! {
        div { class: "mm-view notes-view",
            div { class: "mm-bar",
                div { class: "mm-title",
                    div { class: "mm-h", "{title}" }
                    if let Some(t) = note { div { class: "mm-note", "{t}" } }
                }
                div { class: "mm-tools", role: "toolbar", aria_label: "Study notes",
                    button { class: "icon-btn", title: "Download as Markdown", aria_label: "Download as Markdown",
                        onclick: move |_| {
                            if let Some(n) = notes.peek().as_ref() {
                                crate::mindmap::download(&format!("{}.md", crate::mindmap::file_stem(&n.title)), "text/markdown", &n.markdown);
                            }
                        },
                        crate::Icon { name: "download" }
                    }
                    button { class: "icon-btn", title: "Close the notes", aria_label: "Close the notes",
                        onclick: move |_| on_close.call(()), crate::Icon { name: "x-lg" }
                    }
                }
            }
            div { class: "notes-body",
                if !err.read().is_empty() {
                    div { class: "mm-msg bad", role: "alert", "{err}" }
                } else if let Some(n) = n {
                    article { class: "notes md",
                        if !n.overview.is_empty() {
                            section { class: "notes-sec",
                                h2 { "Overview" }
                                div { dangerous_inner_html: cited(&n.overview, &cites) }
                            }
                        }
                        if !n.ideas.is_empty() {
                            section { class: "notes-sec",
                                h2 { "Key ideas" }
                                for (i, idea) in n.ideas.iter().enumerate() {
                                    div { key: "{i}", class: "notes-idea",
                                        h3 { "{idea.heading}" }
                                        div { dangerous_inner_html: cited(&idea.body, &cites) }
                                    }
                                }
                            }
                        }
                        if !n.quiz.is_empty() {
                            section { class: "notes-sec",
                                div { class: "notes-sec-h",
                                    h2 { "Quiz" }
                                    button {
                                        class: "chip-btn",
                                        onclick: {
                                            let k = n.quiz.len();
                                            move |_| {
                                                if all_shown { shown.set(Default::default()); } else { shown.set((0..k).collect()); }
                                            }
                                        },
                                        if all_shown { "Hide all answers" } else { "Show all answers" }
                                    }
                                }
                                p { class: "dim small", "Answer each in two or three sentences, then check." }
                                ol { class: "notes-quiz",
                                    for (i, q) in n.quiz.iter().enumerate() {
                                        li { key: "{i}",
                                            div { class: "notes-q", dangerous_inner_html: cited_inline(&q.question, &cites) }
                                            if q.answer.is_empty() {
                                                div { class: "dim small", "The sources gave no answer to check this one against." }
                                            } else if shown.read().contains(&i) {
                                                div { class: "notes-a",
                                                    div { dangerous_inner_html: cited(&q.answer, &cites) }
                                                    button { class: "link-btn", onclick: move |_| { shown.write().remove(&i); }, "Hide answer" }
                                                }
                                            } else {
                                                button {
                                                    class: "link-btn",
                                                    "aria-expanded": "false",
                                                    onclick: move |_| { shown.write().insert(i); },
                                                    "Show answer"
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        if !n.essays.is_empty() {
                            section { class: "notes-sec",
                                h2 { "Essay questions" }
                                p { class: "dim small", "No answers: each one asks you to connect several of the ideas above." }
                                ol {
                                    for (i, e) in n.essays.iter().enumerate() {
                                        li { key: "{i}", dangerous_inner_html: cited_inline(e, &cites) }
                                    }
                                }
                            }
                        }
                        if !n.glossary.is_empty() {
                            section { class: "notes-sec",
                                h2 { "Glossary" }
                                dl { class: "notes-gloss",
                                    for (i, t) in n.glossary.iter().enumerate() {
                                        div { key: "{i}",
                                            dt { "{t.term}" }
                                            dd { dangerous_inner_html: cited_inline(&t.definition, &cites) }
                                        }
                                    }
                                }
                            }
                        }
                        if !cites.is_empty() {
                            section { class: "notes-sec notes-src",
                                h2 { "Sources" }
                                for (i, (title, url, ns)) in cite_groups(&cites).into_iter().enumerate() {
                                    div { key: "{i}", class: "cites",
                                        if url.is_empty() {
                                            "{title}"
                                        } else {
                                            a { href: "{url}", target: "_blank", rel: "noopener noreferrer", "{title}" }
                                        }
                                        span { class: "cite-ns", " passages {ns}" }
                                    }
                                }
                            }
                        }
                    }
                } else {
                    div { class: "mm-msg", span { class: "mini-spin" } " Opening the notes…" }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_leave_out_what_is_not_there() {
        assert_eq!(counts(6, 10, 18), "6 ideas · 10 questions · 18 terms");
        assert_eq!(counts(1, 0, 1), "1 idea · 1 term");
        assert_eq!(counts(0, 0, 0), "");
    }

    #[test]
    fn an_inline_line_loses_its_paragraph_and_keeps_its_chip() {
        let cites = vec![Cite {
            n: 1,
            title: "Moshi".into(),
            url: String::new(),
            excerpt: "Moshi is…".into(),
        }];
        let h = cited_inline("Why does **Moshi** skip text? [1]", &cites);
        assert!(!h.starts_with("<p>"), "{h}");
        assert!(h.contains("<strong>Moshi</strong>"));
        assert!(h.contains("class=\"cite\""));
    }

    #[test]
    fn notes_are_up_to_date_only_for_exactly_their_sources() {
        let s = |id: &str, focus: &str, src: &[&str]| StudyNotesSummary {
            id: id.into(),
            sid: "s".into(),
            title: "t".into(),
            focus: focus.into(),
            created_ms: 0,
            sources: src.iter().map(|x| x.to_string()).collect(),
            ideas: 1,
            questions: 1,
            terms: 1,
            headings: vec![],
        };
        let list = vec![s("a", "latency", &["x.md"]), s("b", "", &["x.md", "y.md"])];
        assert_eq!(
            covering_notes(&list, &["y.md".into(), "x.md".into()]),
            Some("b".into())
        );
        assert_eq!(
            covering_notes(&list, &["x.md".into()]),
            None,
            "a focused set is not the plain one"
        );
        assert_eq!(covering_notes(&list, &[]), None);
    }
}
