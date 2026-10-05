//! One collection: its sources on the left, its Studio in the centre, and a
//! map or a set of notes open on the right while one is.
//!
//! The Studio is two halves of one column. Above, four tiles say what can be
//! made; choosing one opens its options in place, under the tiles, and its one
//! primary button makes it. Below, everything already made from these sources,
//! newest first and every kind mixed, each saying where it is: preparing with
//! its live step, failed with the reason and a retry, or ready to open.
//!
//! Nothing here waits on a build. A deck or an audio overview prepares in a
//! background job for minutes; its row follows the prep's own event stream and the
//! page polls the collection while anything is moving, so the person can start
//! another output, read, or leave, and the work is where they left it.
//!
//! The chat is the Ask tab beside the Studio, about this collection's sources.

use dioxus::prelude::*;
use opennotebook_sdk::session::{
    CollectionGetInput, CollectionSummary, SessionBuildReq, SessionEstimateInput,
    SessionEstimateOutput, SessionPrepareInput, SessionPrepareReq, SessionSummary,
};
use opennotebook_sdk::sources::{
    DeepResearchInput, DeepResearchReq, SourceRemoveInput, SourceRemoveReq,
};
use opennotebook_sdk::styles::{DEFAULT_STYLE, STYLES};
use wasm_bindgen::JsCast;

use crate::api::{
    UPLOAD_MAX_MB, api_base, clean_rpc_error, client, gloo_sleep, post_file, post_json,
    sources_client, still_there,
};
use crate::chat::{self, Cmd, Msg};
use crate::dialogs::{
    CostDialog, LimitNote, any_unpriced, count_short, usd, usd_range, usd_range_spoken,
};
use crate::mindmap::{self, MindMapView};
use crate::notes;
use crate::outputs::{
    self, ErrRow, ItemRow, LIVE_MAX, Made, PendingRow, SessionRow, Target, mint_sid,
};
use crate::routes::Open;
use crate::settings::{Settings, SettingsLink, keys, use_settings};
use crate::{FETCHING, Icon, Output, Src, SrcRow, report, server_sources, src_from, thumb_url};

/// The centre column's two tabs.
#[derive(Clone, Copy, PartialEq)]
enum Tab {
    Studio,
    Ask,
}

impl Tab {
    fn label(self) -> &'static str {
        match self {
            Tab::Studio => "Studio",
            Tab::Ask => "Ask",
        }
    }
    fn icon(self) -> &'static str {
        match self {
            Tab::Studio => "stars",
            Tab::Ask => "chat-dots",
        }
    }
    fn tab_id(self) -> &'static str {
        match self {
            Tab::Studio => "tab-studio",
            Tab::Ask => "tab-ask",
        }
    }
    /// The panel the tab shows; the Ask panel's is `chat::THREAD_ID`.
    fn panel_id(self) -> &'static str {
        match self {
            Tab::Studio => "panel-studio",
            Tab::Ask => chat::THREAD_ID,
        }
    }
}

/// NotebookLM's four formats: id, name, what it is, and the lengths it offers
/// (none for Brief, which is always about two minutes).
const AUDIO_FORMATS: [(&str, &str, &str, &[&str]); 4] = [
    (
        "deep_dive",
        "Deep Dive",
        "Two hosts in a lively conversation that unpacks your sources",
        &["shorter", "default", "longer"],
    ),
    (
        "brief",
        "Brief",
        "One host, the key points in about two minutes",
        &[],
    ),
    (
        "critique",
        "Critique",
        "An expert review of your sources, with constructive feedback",
        &["shorter", "default"],
    ),
    (
        "debate",
        "Debate",
        "Two hosts argue different sides of what your sources raise",
        &["shorter", "default"],
    ),
];

/// The lengths a format offers; none for Brief.
fn offered_lengths(format: &str) -> &'static [&'static str] {
    AUDIO_FORMATS
        .iter()
        .find(|f| f.0 == format)
        .map(|f| f.3)
        .unwrap_or(&[])
}

/// An audio overview's format and length as a person reads them: "Brief",
/// "Deep Dive · shorter". The default length goes unsaid.
fn audio_desc(format: &str, length: &str) -> String {
    let name = AUDIO_FORMATS
        .iter()
        .find(|f| f.0 == format)
        .map_or(format, |f| f.1);
    if length != "default" && offered_lengths(format).contains(&length) {
        format!("{name} · {length}")
    } else {
        name.to_string()
    }
}

/// A length the format does not offer falls back to its default.
fn fit_length(format: Signal<String>, mut length: Signal<String>) {
    if !offered_lengths(&format.peek()).contains(&length.peek().as_str()) {
        length.set("default".into());
    }
}

/// What can be uploaded as a source, by extension: the file picker offers
/// these and nothing else is sent. The server reads the same list.
const UPLOAD_EXTS: [&str; 8] = [
    "pdf", "docx", "pptx", "xlsx", "md", "markdown", "txt", "csv",
];
/// The picker's `accept`, from the same list.
const UPLOAD_ACCEPT: &str = ".pdf,.docx,.pptx,.xlsx,.md,.markdown,.txt,.csv";
/// The largest file the server takes, in bytes.
const UPLOAD_MAX: f64 = UPLOAD_MAX_MB as f64 * 1024.0 * 1024.0;
/// What can be uploaded, as the add box says it.
fn upload_hint() -> String {
    format!("PDF, Office, Markdown, text or CSV, up to {UPLOAD_MAX_MB} MB. Or drop them here.")
}
/// The most links one Add takes: the server's `sources_impl::MAX_URLS`. More
/// stay in the box for the next Add rather than being dropped.
const MAX_LINKS: usize = 8;

/// Why a file would be refused, said before it is sent; `None` sends it.
fn upload_problem(name: &str, size: f64) -> Option<String> {
    let ext = name
        .rsplit_once('.')
        .map(|(_, e)| e.to_ascii_lowercase())
        .unwrap_or_default();
    if !UPLOAD_EXTS.contains(&ext.as_str()) {
        Some(format!(
            "not a file the studio reads: {}",
            opennotebook_sdk::UPLOAD_KINDS
        ))
    } else if size > UPLOAD_MAX {
        Some(format!(
            "{:.0} MB, over the {UPLOAD_MAX_MB} MB limit",
            (size / (1024.0 * 1024.0)).ceil()
        ))
    } else if size == 0.0 {
        Some("the file is empty".into())
    } else {
        None
    }
}

/// The voices a deck from `n_src` sources is read by, as the settings decide:
/// "Host and Expert", or "Host" alone.
fn deck_voices(cfg: &Settings, n_src: usize) -> Option<String> {
    let host = cfg.get(keys::SPEAKER1_NAME)?;
    let second = cfg.get(keys::SPEAKER2_NAME)?;
    let two = match cfg.get(keys::SPEAKER_COUNT)?.as_str() {
        "1" => false,
        "2" => true,
        _ => n_src >= 2,
    };
    Some(if two {
        format!("{host} and {second}")
    } else {
        host
    })
}

/// "5 slides · about 5 min · Host and Expert": what a deck is made with.
fn deck_summary(cfg: &Settings, n_src: usize) -> Option<String> {
    Some(format!(
        "{} · about {} · {}",
        cfg.shown(keys::SLIDE_COUNT)?,
        cfg.shown(keys::SESSION_MINUTES)?,
        deck_voices(cfg, n_src)?
    ))
}

/// The output language when it is not English, the one the voices speak.
fn other_language(cfg: &Settings) -> Option<String> {
    cfg.get(keys::LANGUAGE).filter(|l| l != "English")
}

/// How long Research a topic reads the web, by the depth setting.
fn research_time(cfg: &Settings) -> &'static str {
    match cfg.get(keys::RESEARCH_DEPTH).as_deref() {
        Some("quick") => "about a minute",
        _ => "a few minutes",
    }
}

/// A source that did not arrive, kept as a row that says why until dismissed.
fn failed_src(name: String, why: String) -> Src {
    Src {
        icon: String::new(),
        name,
        detail: why,
        ok: false,
        url: String::new(),
        file: String::new(),
    }
}

/// The files a drag carries, or `None` when it carries something else: text,
/// a link, an image dragged off the page.
fn dragged_files(e: &Event<DragData>) -> Option<web_sys::DataTransfer> {
    let dt = e.data().downcast::<web_sys::DragEvent>()?.data_transfer()?;
    dt.types()
        .iter()
        .any(|t| t.as_string().as_deref() == Some("Files"))
        .then_some(dt)
}

/// A file list as the files in it.
fn files_of(list: Option<web_sys::FileList>) -> Vec<web_sys::File> {
    let Some(list) = list else { return Vec::new() };
    (0..list.length()).filter_map(|i| list.get(i)).collect()
}

#[component]
pub(crate) fn CollectionPage(
    cid: String,
    /// What is open in the viewer: the address's map or notes.
    open: Option<Open>,
    /// A tile to open on arrival, when an old address asked for one kind.
    start: Option<Output>,
    /// The collection's name, for the top bar's breadcrumb.
    crumb: Signal<String>,
    on_open: EventHandler<Option<Open>>,
    /// The collection is not there: back to home.
    on_gone: EventHandler<()>,
) -> Element {
    let cid_s = use_signal(|| cid.clone());
    let mut crumb = crumb;
    let cfg = use_settings();
    // What `open` is now, for work that finishes later: a map made in the
    // background opens only if nothing else has been opened meanwhile.
    let mut open_now = use_signal(|| open.clone());
    use_effect(use_reactive((&open,), move |(o,)| open_now.set(o)));

    // ── the collection and its decks and audio overviews ────────────────────
    let mut summary = use_signal(|| None::<CollectionSummary>);
    let mut outputs = use_signal(Vec::<SessionSummary>::new);
    let mut loaded = use_signal(|| false);
    let mut missing = use_signal(|| false);
    let mut load_err = use_signal(String::new);
    // The title field. Follows the server's name until the person types.
    let mut title = use_signal(String::new);
    let mut typing = use_signal(|| false);

    // Which read of the collection is the latest. A poll that set out before a
    // Generate and answers after it would put back a list without the new
    // row, which then reappears on the next poll: a flicker. So a reply only
    // lands when nothing newer has happened since it was asked for.
    let mut load_gen = use_signal(|| 0u32);
    let load = move || async move {
        let my = *load_gen.peek() + 1;
        load_gen.set(my);
        let cid = cid_s.peek().clone();
        let got = async {
            client()?
                .collection_get(CollectionGetInput { cid })
                .await
                .map_err(|e| clean_rpc_error(&e.to_string()))
        }
        .await;
        if *load_gen.peek() != my {
            return;
        }
        match got {
            Ok(g) if g.found => {
                if let Some(c) = g.collection {
                    if !*typing.peek() {
                        title.set(c.title.clone());
                    }
                    crumb.set(c.title.clone());
                    summary.set(Some(c));
                }
                outputs.set(g.outputs);
                load_err.set(String::new());
            }
            Ok(_) => missing.set(true),
            Err(e) => load_err.set(e),
        }
        loaded.set(true);
    };

    // ── sources ─────────────────────────────────────────────────────────────
    let mut srcs = use_signal(Vec::<Src>::new);
    let mut srcs_loaded = use_signal(|| false);
    let mut srcs_err = use_signal(String::new);
    // The add box, shared by Add source and Research a topic.
    let mut draft = use_signal(String::new);
    // Topics being researched, each shown as a row until its report lands.
    // By a number of their own, so the same topic asked twice is two rows.
    let mut researching = use_signal(Vec::<(u32, String)>::new);
    // Files being uploaded and read, by a number of their own and their name,
    // each a row until it is a source or says why it is not.
    let mut uploading = use_signal(Vec::<(u32, String)>::new);
    let mut upload_n = use_signal(|| 0u32);
    // Files dragged over the sources panel: the drop overlay shows while this
    // is above zero. A count, because entering a child leaves its parent.
    let mut drag_depth = use_signal(|| 0i32);
    // The icon each page declared while it was read, by its url: the server's
    // list does not carry them, so they are kept for as long as the page is.
    let mut icons = use_signal(std::collections::HashMap::<String, String>::new);

    // The server's list, with the rows it does not have kept after it: a page
    // still being read, or one that failed and says why.
    let load_sources = move || async move {
        let cid = cid_s.peek().clone();
        match server_sources(&cid).await {
            Ok(list) => {
                let local: Vec<Src> = srcs
                    .peek()
                    .iter()
                    .filter(|s| !s.ok || s.detail == FETCHING)
                    .cloned()
                    .collect();
                let mut next: Vec<Src> = list
                    .into_iter()
                    .map(|mut s| {
                        if let Some(i) = icons.peek().get(&s.url) {
                            s.icon = i.clone();
                        }
                        s
                    })
                    .collect();
                next.extend(local);
                srcs.set(next);
                srcs_err.set(String::new());
            }
            Err(e) => srcs_err.set(e),
        }
        srcs_loaded.set(true);
    };

    let mm = mindmap::use_map_state();
    let nt = notes::use_notes_state();
    use_hook(notes::install_cite_flip);
    // A citation clicked anywhere on the page: its source, open at the passage.
    let mut cited = crate::source::use_cite_open();

    use_hook(move || {
        let cid = cid_s.peek().clone();
        // Not the last collection's name while this one's is on its way.
        crumb.set(String::new());
        spawn(async move { load().await });
        spawn(async move { load_sources().await });
        spawn(mindmap::load(cid.clone(), mm));
        spawn(notes::load(cid, nt));
    });

    // While anything is moving, look again every few seconds: an output still
    // preparing, a name the studio has not given yet, or a cover still being
    // designed. Idle otherwise.
    use_future(move || async move {
        loop {
            gloo_sleep(4000).await;
            let preparing = outputs.peek().iter().any(|s| s.state == "preparing");
            let naming = cfg.doc.peek().value(keys::AUTO_NAME).as_deref() != Some("off")
                && summary
                    .peek()
                    .as_ref()
                    .is_some_and(|c| c.title_auto && c.title.trim().is_empty() && c.sources > 0);
            let covers_on = cfg.doc.peek().value(keys::COVERS).as_deref() != Some("off");
            let cover = summary
                .peek()
                .as_ref()
                .is_some_and(|c| crate::home::cover_pending(c, covers_on));
            if preparing || naming || cover {
                load().await;
            }
        }
    });

    // The sources a build would read: on the server, not being read, not failed.
    let ready_n = use_memo(move || {
        srcs.read()
            .iter()
            .filter(|s| s.ok && !s.file.is_empty())
            .count()
    });
    let staged = use_memo(move || {
        srcs.read()
            .iter()
            .filter(|s| s.ok && !s.file.is_empty())
            .map(|s| s.file.clone())
            .collect::<Vec<String>>()
    });

    // One input for both kinds of source: anything that looks like a link is
    // fetched, everything else is kept as a note.
    let add_source = move |_| async move {
        let raw = draft.read().trim().to_string();
        if raw.is_empty() {
            return;
        }
        let mut urls: Vec<String> = raw
            .split_whitespace()
            .filter(|t| t.starts_with("http://") || t.starts_with("https://"))
            .map(|t| t.to_string())
            .collect();
        // Past the most one Add takes, the rest wait in the box.
        let rest = urls.split_off(urls.len().min(MAX_LINKS));
        draft.set(rest.join("\n"));
        let base = api_base();
        let cid = cid_s.peek().clone();
        if !urls.is_empty() {
            for u in &urls {
                srcs.write().push(Src {
                    icon: String::new(),
                    name: u.clone(),
                    detail: FETCHING.into(),
                    ok: true,
                    url: u.clone(),
                    file: String::new(),
                });
            }
            let body = serde_json::json!({ "urls": urls }).to_string();
            let got = post_json(&format!("{base}/source/fetch?session={cid}"), &body).await;
            srcs.write().retain(|s| s.detail != FETCHING);
            match got {
                Ok(txt) => {
                    // A page that did not arrive has to be visible: a deck built
                    // without it looks exactly like a deck built with it. The
                    // ones that did are read back from the server below.
                    let got: Vec<serde_json::Value> =
                        serde_json::from_str(&txt).unwrap_or_default();
                    for g in got {
                        let s = src_from(&g);
                        if !s.ok {
                            srcs.write().push(s);
                        } else if !s.icon.is_empty() {
                            icons.write().insert(s.url, s.icon);
                        }
                    }
                }
                Err(e) => srcs
                    .write()
                    .push(failed_src("Those links could not be read".into(), e)),
            }
        } else {
            let name = raw.split_whitespace().take(6).collect::<Vec<_>>().join(" ");
            let body = serde_json::json!({ "name": name, "text": raw }).to_string();
            if let Err(e) = post_json(&format!("{base}/source/text?session={cid}"), &body).await {
                let name = if name.is_empty() { "Note".into() } else { name };
                srcs.write().push(failed_src(name, e));
            }
        }
        load_sources().await;
        load().await;
    };

    // Research a topic: the add box's text as a brief, read across the web for
    // about a minute and added as one written report.
    let research = move |_| async move {
        let topic = draft.read().trim().to_string();
        if topic.is_empty() {
            return;
        }
        draft.set(String::new());
        let n = *upload_n.peek() + 1;
        upload_n.set(n);
        researching.write().push((n, topic.clone()));
        let cid = cid_s.peek().clone();
        let got = async {
            sources_client()?
                .deep_research(DeepResearchInput {
                    req: DeepResearchReq {
                        sid: cid,
                        topic: topic.clone(),
                    },
                })
                .await
                .map_err(|e| clean_rpc_error(&e.to_string()))
        }
        .await;
        researching.write().retain(|r| r.0 != n);
        let failed = match got {
            Ok(r) if r.ok => None,
            Ok(r) => Some(r.error),
            Err(e) => Some(e),
        };
        if let Some(why) = failed {
            let why = if why.is_empty() {
                "the research did not finish".into()
            } else {
                why
            };
            srcs.write()
                .push(failed_src(format!("Research: {topic}"), why));
        }
        load_sources().await;
        load().await;
    };

    let remove_source = move |s: Src| {
        if s.file.is_empty() {
            srcs.write().retain(|x| *x != s);
            return;
        }
        let cid = cid_s.peek().clone();
        spawn(async move {
            let done = async {
                sources_client()?
                    .source_remove(SourceRemoveInput {
                        req: SourceRemoveReq {
                            sid: cid,
                            name: s.file.clone(),
                        },
                    })
                    .await
                    .map_err(|e| clean_rpc_error(&e.to_string()))
                    .and_then(|o| still_there(o.value))
            }
            .await;
            if let Err(e) = done {
                report(format!(
                    "\u{201c}{}\u{201d} could not be removed. {e}",
                    s.name
                ));
            }
            load_sources().await;
            load().await;
        });
    };

    // Files picked or dropped: refused at once when the studio could not read
    // them, otherwise sent one at a time, each row becoming its source as it
    // lands. The collection is looked at again after, for the name.
    let upload_files = move |files: Vec<web_sys::File>| async move {
        let mut queue = Vec::new();
        for f in files {
            let name = f.name();
            if let Some(why) = upload_problem(&name, f.size()) {
                srcs.write().push(failed_src(name, why));
                continue;
            }
            let n = *upload_n.peek() + 1;
            upload_n.set(n);
            uploading.write().push((n, name));
            queue.push((n, f));
        }
        if queue.is_empty() {
            return;
        }
        let base = api_base();
        let cid = cid_s.peek().clone();
        for (n, f) in queue {
            let name = f.name();
            let url = format!(
                "{base}/source/upload?session={cid}&name={}",
                js_sys::encode_uri_component(&name)
            );
            let got = post_file(&url, &f).await;
            // The list first, then the row out: no gap where the file is
            // neither being read nor listed.
            if got.is_ok() {
                load_sources().await;
            }
            uploading.write().retain(|u| u.0 != n);
            if let Err(e) = got {
                srcs.write().push(failed_src(name, e));
            }
        }
        load().await;
    };
    let pick_files = move |_| {
        if let Some(el) = web_sys::window()
            .and_then(|w| w.document())
            .and_then(|d| d.get_element_by_id("src-files"))
            .and_then(|el| el.dyn_into::<web_sys::HtmlElement>().ok())
        {
            el.click();
        }
    };

    // ── renaming an output ──────────────────────────────────────────────────
    // A deck, an audio overview, a map or notes: renamed in place, in the
    // list and in the viewer if it is open, without reading anything back.
    let rename = move |(target, next): (Target, String)| {
        let cid = cid_s.peek().clone();
        spawn(async move {
            if let Err(e) = outputs::retitle(cid, &target, next.clone()).await {
                report(format!("{} could not be renamed. {e}", target.subject()));
                return;
            }
            match target {
                Target::Session(sid) => {
                    if let Some(s) = outputs.write().iter_mut().find(|s| s.sid == sid) {
                        s.title = next;
                    }
                }
                Target::Map(id) => {
                    let mut maps = mm.maps;
                    if let Some(m) = maps.write().iter_mut().find(|m| m.id == id) {
                        m.title = next;
                    }
                }
                Target::Notes(id) => {
                    let mut list = nt.notes;
                    if let Some(n) = list.write().iter_mut().find(|n| n.id == id) {
                        n.title = next;
                    }
                }
            }
        });
    };
    // Any output, deleted: closed first if it is the one open, then the lists
    // read back.
    let remove = move |target: Target| {
        let cid = cid_s.peek().clone();
        let shown = match &target {
            Target::Map(id) => Some(Open::Map(id.clone())),
            Target::Notes(id) => Some(Open::Notes(id.clone())),
            Target::Session(_) => None,
        };
        if shown.is_some() && *open_now.peek() == shown {
            on_open.call(None);
        }
        spawn(async move {
            if let Err(e) = outputs::delete(cid.clone(), &target).await {
                report(format!("{} could not be deleted. {e}", target.subject()));
            }
            match target {
                Target::Map(_) => mindmap::load(cid, mm).await,
                Target::Notes(_) => notes::load(cid, nt).await,
                Target::Session(_) => {}
            }
            load().await;
        });
    };

    // ── the title ───────────────────────────────────────────────────────────
    // Given to the server on Enter or on leaving the field. Empty hands the
    // naming back to the studio.
    let mut commit_title = move || {
        // Enter commits, and the blur that follows has nothing new to say.
        if !*typing.peek() {
            return;
        }
        typing.set(false);
        let t = title.peek().trim().to_string();
        let was = summary
            .peek()
            .as_ref()
            .map(|c| c.title.trim().to_string())
            .unwrap_or_default();
        if t == was {
            return;
        }
        let cid = cid_s.peek().clone();
        spawn(async move {
            if let Err(e) = crate::home::collection_retitle(cid, t).await {
                report(format!("The collection could not be renamed. {e}"));
            }
            load().await;
        });
    };

    // ── making ──────────────────────────────────────────────────────────────
    let mut tab = use_signal(|| Tab::Studio);
    // The tile whose options are open; None shows the tiles alone.
    let mut chosen = use_signal(move || start);
    let mut style = use_signal(|| DEFAULT_STYLE.id.to_string());
    // An audio overview's three options: NotebookLM's format, length and
    // Customize box. Deep Dive at its default length is what NotebookLM makes
    // when nothing is chosen.
    let mut audio_format = use_signal(|| "deep_dive".to_string());
    let mut audio_length = use_signal(|| "default".to_string());
    // The Create panel starts on the settings' style, format and length, and
    // follows them when they change, until the person picks their own here.
    let mut picked = use_signal(|| false);
    use_effect(move || {
        let d = cfg.doc.read();
        if *picked.peek() {
            return;
        }
        if let Some(v) = d
            .value(keys::STYLE)
            .filter(|v| STYLES.iter().any(|s| s.id == v))
        {
            style.set(v);
        }
        if let Some(f) = d
            .value(keys::AUDIO_FORMAT)
            .filter(|f| AUDIO_FORMATS.iter().any(|a| a.0 == f))
        {
            audio_format.set(f);
        }
        if let Some(l) = d.value(keys::AUDIO_LENGTH) {
            audio_length.set(l);
        }
        fit_length(audio_format, audio_length);
    });
    let mut audio_focus = use_signal(String::new);
    // The prepare call is in flight (seconds); the prep itself is not waited on.
    let mut generating = use_signal(|| false);
    let mut gen_err = use_signal(String::new);
    // A build's estimated cost, fetched whenever what would be built changes.
    let mut est = use_signal(|| None::<SessionEstimateOutput>);
    let mut est_err = use_signal(String::new);
    let mut est_loading = use_signal(|| false);
    let mut est_open = use_signal(|| false);
    // Which estimate request is the latest. Replies come back in any order,
    // and only the newest one describes what Generate would build now.
    let mut est_seq = use_signal(|| 0u32);

    // Start a deck or an audio overview in this collection: a fresh output id,
    // the collection's sources, and what was picked here. Voices, slide count
    // and narration length are left to the server, which takes them from the
    // settings by the same plan as the estimate, so the two cannot disagree.
    // Returns once the prep is submitted; its row follows it from there.
    let generate_build = move |kind: Output, named: Option<String>| async move {
        if *generating.peek() {
            return;
        }
        generating.set(true);
        gen_err.set(String::new());
        let got = async {
            let c = client()?;
            let audio = kind == Output::Audio;
            let sid = mint_sid(&outputs.peek());
            // Empty unless the agent named it: the server names an output by
            // what tells it from the others ("Editorial slides").
            let t = named.map(|t| t.trim().to_string()).unwrap_or_default();
            let req = SessionPrepareReq {
                sid: sid.clone(),
                title: t.clone(),
                // Empty: the server reads the collection's staged sources.
                resource_dir: String::new(),
                // Empty, absent: the settings' voices and slide count.
                speakers: Vec::new(),
                slide_count: None,
                style: (!audio).then(|| style.peek().clone()),
                research_topic: None,
                audio_format: audio.then(|| audio_format.peek().clone()),
                audio_length: audio.then(|| audio_length.peek().clone()),
                focus: audio
                    .then(|| audio_focus.peek().trim().to_string())
                    .filter(|f| !f.is_empty()),
                collection: Some(cid_s.peek().clone()),
            };
            c.session_prepare(SessionPrepareInput { req })
                .await
                .map_err(|e| clean_rpc_error(&e.to_string()))?;
            Ok::<_, String>((sid, t, audio))
        }
        .await;
        match got {
            Ok((sid, t, audio)) => {
                // On the list at once, before the server's row is read back,
                // and no read already on its way may take it off again.
                let g = *load_gen.peek() + 1;
                load_gen.set(g);
                outputs.write().insert(
                    0,
                    SessionSummary {
                        sid,
                        // Until the server's row, with its name, is read back.
                        title: if t.is_empty() {
                            kind.label().to_string()
                        } else {
                            t
                        },
                        state: "preparing".into(),
                        kind: if audio { "audio" } else { "session" }.into(),
                        created_ms: js_sys::Date::now() as i64,
                        collection: cid_s.peek().clone(),
                        ..Default::default()
                    },
                );
                chosen.set(None);
                audio_focus.set(String::new());
                tab.set(Tab::Studio);
                spawn(async move { load().await });
            }
            Err(e) => gen_err.set(e),
        }
        generating.set(false);
    };

    // A build the agent asked for while another was starting: started as soon
    // as that one is submitted.
    let mut queued_build = use_signal(|| None::<(Output, Option<String>)>);
    use_effect(move || {
        if !*generating.read()
            && queued_build.read().is_some()
            && let Some((kind, named)) = queued_build.write().take()
        {
            spawn(generate_build(kind, named));
        }
    });

    // A map or notes: one call of seconds. The row says so while it runs, and
    // what it made opens beside the Studio, in place of whatever was open when
    // it was asked for, unless something else was opened while it was made.
    let generate_map = move || async move {
        chosen.set(None);
        let cid = cid_s.peek().clone();
        let before = open_now.peek().clone();
        if let Some(id) = mindmap::make(cid, mm).await
            && *open_now.peek() == before
        {
            on_open.call(Some(Open::Map(id)));
        }
        load().await;
    };
    let generate_notes = move || async move {
        chosen.set(None);
        let cid = cid_s.peek().clone();
        let before = open_now.peek().clone();
        if let Some(id) = notes::make(cid, nt).await
            && *open_now.peek() == before
        {
            on_open.call(Some(Open::Notes(id)));
        }
        load().await;
    };
    let generate = move |kind: Output| match kind {
        Output::Session | Output::Audio => {
            spawn(generate_build(kind, None));
        }
        Output::MindMap => {
            spawn(generate_map());
        }
        Output::Notes => {
            spawn(generate_notes());
        }
    };

    // The estimate asks for exactly what Generate would build: what was picked
    // here, and the rest left to the settings as Generate leaves it.
    let fetch_estimate = move |_: ()| async move {
        let my = *est_seq.peek() + 1;
        est_seq.set(my);
        let Some(kind) = *chosen.peek() else { return };
        let n = *ready_n.peek();
        if n == 0 || !kind.is_build() {
            est.set(None);
            return;
        }
        est_loading.set(true);
        let Ok(c) = client() else {
            if *est_seq.peek() == my {
                est_err.set("the studio is unreachable".into());
                est_loading.set(false);
            }
            return;
        };
        est_err.set(String::new());
        let audio = kind == Output::Audio;
        let req = SessionBuildReq {
            sid: String::new(),
            collection: Some(cid_s.peek().clone()),
            title: None,
            speakers: None,
            slide_count: None,
            style: (!audio).then(|| style.peek().clone()),
            audio_format: audio.then(|| audio_format.peek().clone()),
            audio_length: audio.then(|| audio_length.peek().clone()),
            focus: audio
                .then(|| audio_focus.peek().trim().to_string())
                .filter(|f| !f.is_empty()),
        };
        let got = c.session_estimate(SessionEstimateInput { req }).await;
        if *est_seq.peek() != my {
            // A newer request is out; its reply is the one to show.
            return;
        }
        match got {
            Ok(e) => est.set(Some(e)),
            Err(e) => {
                est.set(None);
                est_err.set(clean_rpc_error(&format!("{e}")));
            }
        }
        est_loading.set(false);
    };
    // Priced again when what would be made changes: an estimate for a
    // different build is worse than none.
    use_effect(move || {
        let kind = *chosen.read();
        let n = ready_n();
        let _ = (
            style.read().len(),
            audio_format.read().len(),
            audio_length.read().len(),
        );
        // A change in Settings (slides, voices, the limit) is a different build.
        let _ = cfg.doc.read();
        let cid = cid_s.peek().clone();
        match kind {
            Some(k) if k.is_build() && n > 0 => {
                spawn(fetch_estimate(()));
            }
            Some(Output::MindMap) if n > 0 => {
                spawn(mindmap::estimate(cid, mm));
            }
            Some(Output::Notes) if n > 0 => {
                spawn(notes::estimate(cid, nt));
            }
            _ => {
                // Anything still in flight is for a build no longer chosen.
                let n = *est_seq.peek() + 1;
                est_seq.set(n);
                est.set(None);
                est_loading.set(false);
            }
        }
    });
    // The map or notes already made from exactly these sources with no focus:
    // making another would say the same again, so the options say so.
    let fresh_map = use_memo(move || {
        if !mm.focus.read().trim().is_empty() {
            return None;
        }
        mindmap::covering_map(&mm.maps.read(), &staged.read())
    });
    let fresh_notes = use_memo(move || {
        if !nt.focus.read().trim().is_empty() {
            return None;
        }
        notes::covering_notes(&nt.notes.read(), &staged.read())
    });

    // ── the Ask tab ─────────────────────────────────────────────────────────
    let chat = chat::use_chat_state(&cid);
    // Something to make, asked for in the chat: by the agent, or by a slash
    // command. A deck or audio overview already starting is not dropped: the
    // new one is queued, and starts the moment the first has been submitted.
    let make = move |kind: Output, named: Option<String>| {
        if !kind.is_build() {
            generate(kind);
        } else if *generating.peek() {
            queued_build.set(Some((kind, named)));
            let mut msgs = chat.msgs;
            msgs.write().push(Msg::said(
                "Studio",
                "Another build is starting; this one follows as soon as it has been submitted.",
                false,
            ));
        } else {
            spawn(generate_build(kind, named));
        }
    };
    let send_chat = move |text: String| async move {
        if *chat.talking.peek() {
            return;
        }
        // A slash command: the makers, help and clear run here; search and
        // research go to the agent as typed, and its prompt reads them.
        match chat::parse_command(&text) {
            None | Some(Ok((Cmd::Search | Cmd::Research, _))) => {}
            Some(Err(name)) => {
                chat::exchange(
                    chat,
                    &text,
                    format!("There is no /{name} command. Type / to see the ones there are."),
                );
                return;
            }
            Some(Ok((Cmd::Help, _))) => {
                chat::exchange(chat, &text, chat::help_text());
                return;
            }
            Some(Ok((Cmd::Clear, _))) => {
                chat::chat_forget(&cid_s.peek());
                let mut msgs = chat.msgs;
                msgs.set(chat::greeting());
                return;
            }
            Some(Ok((Cmd::Ask, q))) => {
                if q.is_empty() {
                    chat::exchange(chat, &text, "Ask what? Type the question after /ask.");
                } else {
                    chat::ask_sources(chat, q).await;
                }
                return;
            }
            Some(Ok((Cmd::Make(kind), arg))) => {
                let n = *ready_n.peek();
                if n == 0 {
                    chat::exchange(
                        chat,
                        &text,
                        format!(
                            "I make {} from your sources, and there are none yet. Add a link or a \
                             file on the left, or try `/search` and a topic.",
                            kind.label().to_lowercase()
                        ),
                    );
                    return;
                }
                // What follows the name: the deck's title, or what the others
                // should focus on.
                let mut named = None;
                if !arg.is_empty() {
                    match kind {
                        Output::Session => named = Some(arg),
                        Output::Audio => audio_focus.set(arg),
                        Output::MindMap => {
                            let mut f = mm.focus;
                            f.set(arg)
                        }
                        Output::Notes => {
                            let mut f = nt.focus;
                            f.set(arg)
                        }
                    }
                }
                let sources = if n == 1 {
                    "your source".to_string()
                } else {
                    format!("your {n} sources")
                };
                let where_ = if kind.is_build() {
                    "It appears in the Studio tab and takes a few minutes."
                } else {
                    "It opens beside the chat when it is ready."
                };
                chat::exchange(
                    chat,
                    &text,
                    format!(
                        "Making {} from {sources}. {where_}",
                        kind.label().to_lowercase()
                    ),
                );
                let mut make = make;
                make(kind, named);
                return;
            }
        }
        let kind = chosen.peek().unwrap_or(Output::Session);
        let added = chat::send(
            chat,
            text,
            kind,
            // A page the agent read: a source row at once.
            move |s: Src| {
                if !s.icon.is_empty() {
                    icons.write().insert(s.url.clone(), s.icon.clone());
                }
                srcs.write().push(s);
            },
            // The agent asked for something to be made: made the way the
            // Studio's Generate makes it.
            move |kind: Output, named: Option<String>| {
                let mut make = make;
                make(kind, named);
            },
        )
        .await;
        if added {
            load_sources().await;
            load().await;
        }
    };
    // A click on a mind map topic: NotebookLM's question, answered from the
    // sources with citations, in the Ask tab.
    let ask_sources = move |question: String| async move {
        if question.trim().is_empty() || *chat.talking.peek() {
            return;
        }
        tab.set(Tab::Ask);
        chat::ask_sources(chat, question).await;
    };

    // The sources beside an open map, or folded to the strip.
    let mut src_open = use_signal(|| false);

    if *missing.read() {
        return rsx! {
            main {
                div { class: "empty",
                    div { class: "empty-mark", Icon { name: "collection", class: "xl" } }
                    div { class: "empty-t", "This collection is not here" }
                    div { class: "empty-d", "It may have been deleted, or the link is wrong." }
                    button { onclick: move |_| on_gone.call(()), "Back to home" }
                }
            }
        };
    }

    // Everything made, newest first, every kind in one list.
    let made: Vec<Made> = {
        let mut v: Vec<Made> = outputs.read().iter().cloned().map(Made::Session).collect();
        v.extend(mm.maps.read().iter().cloned().map(Made::Map));
        v.extend(nt.notes.read().iter().cloned().map(Made::Notes));
        v.sort_by_key(|m| std::cmp::Reverse(m.created_ms()));
        v
    };
    // Live progress for the newest preparing builds only. Each live row holds
    // an EventSource, and a browser allows six HTTP/1.1 connections per host:
    // a stream per row starved every other request of the page once a few
    // were preparing. The rest say "Preparing" and catch up through the poll.
    let live: Vec<String> = {
        let mut p: Vec<(_, String)> = outputs
            .read()
            .iter()
            .filter(|s| s.state == "preparing")
            .map(|s| (s.created_ms, s.sid.clone()))
            .collect();
        p.sort_by_key(|(ms, _)| std::cmp::Reverse(*ms));
        p.into_iter().take(LIVE_MAX).map(|(_, sid)| sid).collect()
    };
    let n_src = *ready_n.read();
    let n_out = made.len();
    // The build as chosen would be refused for its cost.
    let over_limit = est.read().as_ref().is_some_and(|e| e.over_limit);
    let sum = summary.read().clone();
    // Settings the page says something about. Until they are read the hints
    // stay quiet rather than show a value that may not be the one in force.
    let auto_name = cfg.on(keys::AUTO_NAME);
    let naming = auto_name && sum.as_ref().is_some_and(|c| c.title_auto);
    let show_cost = cfg.on(keys::SHOW_COST);
    let research_takes = research_time(&cfg);
    let language = other_language(&cfg);
    let topic_typed = {
        let d = draft.read();
        !d.trim().is_empty() && !d.contains("http://") && !d.contains("https://")
    };
    let viewer = open.is_some();
    let kind_now = *chosen.read();
    let reload_all = move |_: ()| {
        let cid = cid_s.peek().clone();
        spawn(async move { load().await });
        spawn(mindmap::load(cid.clone(), mm));
        spawn(notes::load(cid, nt));
    };

    rsx! {
        div {
            class: match (viewer, *src_open.read()) {
                (true, true) => "create with-map show-src",
                (true, false) => "create with-map",
                _ => "create two",
            },
            // With a map open, the sources fold to this strip; pressing it
            // opens them beside the map, which stays where it is.
            div { class: "src-strip",
                button {
                    title: "Show the sources",
                    aria_label: "Show the sources",
                    "aria-expanded": "false",
                    onclick: move |_| src_open.set(true),
                    Icon { name: "link-45deg" }
                    "Sources"
                }
            }

            // ── sources ─────────────────────────────────────────────────────
            aside {
                class: "panel sources",
                aria_label: "Sources",
                // Files dropped anywhere on the panel are uploaded. Only a drag
                // that carries files is answered; text dragged about is not.
                ondragenter: move |e: Event<DragData>| {
                    if dragged_files(&e).is_some() {
                        e.prevent_default();
                        drag_depth += 1;
                    }
                },
                ondragover: move |e: Event<DragData>| {
                    if let Some(dt) = dragged_files(&e) {
                        e.prevent_default();
                        dt.set_drop_effect("copy");
                    }
                },
                ondragleave: move |e: Event<DragData>| {
                    if dragged_files(&e).is_some() {
                        let d = *drag_depth.peek();
                        drag_depth.set((d - 1).max(0));
                    }
                },
                ondrop: move |e: Event<DragData>| {
                    let Some(dt) = dragged_files(&e) else { return };
                    e.prevent_default();
                    drag_depth.set(0);
                    spawn(upload_files(files_of(dt.files())));
                },
                if *drag_depth.read() > 0 {
                    div { class: "src-drop", aria_hidden: "true",
                        Icon { name: "upload", class: "xl" }
                        span { "Drop files to add them" }
                    }
                }
                div { class: "src-head",
                    h2 { "Sources" if n_src > 0 { span { class: "h-n", " {n_src}" } } }
                    button {
                        class: "icon-btn src-fold",
                        title: "Fold the sources away",
                        aria_label: "Fold the sources away",
                        "aria-expanded": "true",
                        onclick: move |_| src_open.set(false),
                        Icon { name: "chevron-left" }
                    }
                }
                textarea {
                    aria_label: "A link, some text, or a topic",
                    value: "{draft}",
                    placeholder: "Paste up to {MAX_LINKS} links, text, or a topic to research",
                    oninput: move |e| draft.set(e.value()),
                }
                div { class: "src-actions", role: "group", aria_label: "Add sources",
                    button {
                        disabled: draft.read().trim().is_empty(),
                        onclick: add_source,
                        Icon { name: "plus-lg" }
                        "Add source"
                    }
                    button {
                        class: "ghost",
                        title: "Read the web on this topic for {research_takes} and add a written report",
                        disabled: draft.read().trim().is_empty(),
                        onclick: research,
                        Icon { name: "search" }
                        "Research a topic"
                    }
                    button {
                        class: "src-upload",
                        title: "PDF, Word, PowerPoint, Excel, Markdown, text or CSV, up to {UPLOAD_MAX_MB} MB each. Or drop files on this panel.",
                        onclick: pick_files,
                        Icon { name: "upload" }
                        "Upload files"
                    }
                    p { class: "src-upload-d", "{upload_hint()}" }
                }
                // A topic in the box: what Research a topic will do with it.
                if topic_typed && *cfg.loaded.read() {
                    p { class: "src-hint",
                        if research_takes == "about a minute" { "Quick research" } else { "Standard research" }
                        " takes {research_takes}. "
                        SettingsLink { tab: "defaults", text: "Change in Settings › Generation defaults".to_string() }
                    }
                }
                input {
                    id: "src-files",
                    r#type: "file",
                    multiple: true,
                    accept: UPLOAD_ACCEPT,
                    hidden: true,
                    tabindex: "-1",
                    aria_hidden: "true",
                    onchange: move |_| {
                        let Some(input) = web_sys::window()
                            .and_then(|w| w.document())
                            .and_then(|d| d.get_element_by_id("src-files"))
                            .and_then(|el| el.dyn_into::<web_sys::HtmlInputElement>().ok())
                        else {
                            return;
                        };
                        let files = files_of(input.files());
                        // Emptied, so picking the same file again is a change.
                        input.set_value("");
                        spawn(upload_files(files));
                    },
                }
                div { class: "srclist", aria_live: "polite",
                    for (n, name) in uploading.read().iter().cloned() {
                        div { key: "u-{n}", class: "src run",
                            span { class: "src-i spin", title: "Reading…" }
                            div { class: "src-t",
                                div { class: "src-n", title: "{name}", "Reading {name}…" }
                                div { class: "src-d", "Uploading and reading the file" }
                            }
                        }
                    }
                    for (n, topic) in researching.read().iter().cloned() {
                        div { key: "r-{n}", class: "src run",
                            span { class: "src-i spin", title: "Researching…" }
                            div { class: "src-t",
                                div { class: "src-n", "Researching: {topic}" }
                                div { class: "src-d", "Reading the web · {research_takes}" }
                            }
                        }
                    }
                    if !*srcs_loaded.read() {
                        div { class: "src-note", span { class: "mini-spin" } "Loading sources…" }
                    } else if !srcs_err.read().is_empty() && srcs.read().is_empty() {
                        div { class: "src-note bad", role: "alert",
                            "Sources could not be loaded: {srcs_err}"
                            button {
                                class: "link-btn",
                                onclick: move |_| { spawn(async move { load_sources().await }); },
                                "Try again"
                            }
                        }
                    } else if srcs.read().is_empty() && researching.read().is_empty() && uploading.read().is_empty() {
                        div { class: "src-empty",
                            div { class: "src-empty-m", Icon { name: "link-45deg", class: "xl" } }
                            div { class: "src-empty-t", "No sources yet" }
                            div { class: "dim small", "Paste links or text, upload files, or research a topic. Everything you make here is made from these." }
                        }
                    } else {
                        for (n, s) in srcs.read().iter().cloned().enumerate() {
                            SrcRow { key: "{n}-{s.file}-{s.icon}", s, on_remove: remove_source }
                        }
                    }
                }
            }

            // ── the Studio and Ask ──────────────────────────────────────────
            section { class: "panel studio",
                div { class: "studio-head",
                    if let Some(c) = summary.read().as_ref() {
                        crate::home::Cover { cid: c.cid.clone(), version: c.cover_version.clone(), size: "head" }
                    }
                    div { class: "sh-main",
                        input {
                            class: "make-title",
                            r#type: "text",
                            aria_label: "Collection name",
                            title: if naming {
                                "Named from its sources until you name it. Turn off in Settings › Generation defaults."
                            } else {
                                "Name this collection"
                            },
                            placeholder: "Untitled collection",
                            maxlength: "120",
                            value: "{title}",
                            oninput: move |e| {
                                typing.set(true);
                                title.set(e.value());
                            },
                            onkeydown: move |e: Event<KeyboardData>| {
                                if e.key() == Key::Enter {
                                    commit_title();
                                }
                            },
                            onblur: move |_| commit_title(),
                        }
                        div { class: "sh-sub num",
                            span { if n_src == 1 { "1 source" } else { "{n_src} sources" } }
                            span { "·" }
                            span { if n_out == 1 { "1 output" } else { "{n_out} outputs" } }
                            if naming && title.read().trim().is_empty() && n_src > 0 {
                                span { "·" }
                                span { class: "sh-auto", "Naming it from its sources…" }
                            } else if naming && !title.read().trim().is_empty() {
                                span { "·" }
                                span { class: "sh-auto", "Named from its sources" }
                            }
                        }
                    }
                    // A tablist as the ARIA pattern has it: arrow keys move
                    // between the tabs, only the selected one is in the Tab
                    // order, and each names the panel it shows.
                    div {
                        class: "seg",
                        role: "tablist",
                        aria_label: "View",
                        onkeydown: move |e: Event<KeyboardData>| {
                            let next = match e.key() {
                                Key::ArrowLeft | Key::Home => Tab::Studio,
                                Key::ArrowRight | Key::End => Tab::Ask,
                                _ => return,
                            };
                            e.prevent_default();
                            tab.set(next);
                            crate::focus_id(next.tab_id());
                        },
                        for t in [Tab::Studio, Tab::Ask] {
                            button {
                                key: "{t.tab_id()}",
                                id: t.tab_id(),
                                class: if *tab.read() == t { "on" } else { "" },
                                role: "tab",
                                tabindex: if *tab.read() == t { "0" } else { "-1" },
                                "aria-selected": if *tab.read() == t { "true" } else { "false" },
                                "aria-controls": t.panel_id(),
                                onclick: move |_| tab.set(t),
                                Icon { name: t.icon() }
                                "{t.label()}"
                            }
                        }
                    }
                }

                if *tab.read() == Tab::Studio {
                    div {
                        id: Tab::Studio.panel_id(),
                        class: "studio-body",
                        role: "tabpanel",
                        aria_labelledby: Tab::Studio.tab_id(),
                        div { class: "sec-h", span { class: "sec-t", "Create" } }
                        div { class: "tiles", role: "group", aria_label: "Create",
                            for k in Output::ALL {
                                button {
                                    key: "{k.wire()}",
                                    class: if kind_now == Some(k) { "tile on" } else { "tile" },
                                    "aria-pressed": if kind_now == Some(k) { "true" } else { "false" },
                                    disabled: n_src == 0,
                                    title: if n_src == 0 { "Add a source first".to_string() } else { k.hint().to_string() },
                                    onclick: move |_| {
                                        let now = *chosen.peek();
                                        chosen.set(if now == Some(k) { None } else { Some(k) });
                                        gen_err.set(String::new());
                                    },
                                    span { class: "tile-i", Icon { name: k.icon(), class: "lg" } }
                                    span { class: "tile-n", "{k.label()}" }
                                    span { class: "tile-d", "{k.blurb()}" }
                                }
                            }
                        }
                        if n_src == 0 && *srcs_loaded.read() {
                            p { class: "tiles-hint", "Add a source first." }
                        }

                        if let Some(k) = kind_now.filter(|_| n_src > 0) {
                            div { class: "opts", role: "region", aria_label: "{k.label()} options",
                                div { class: "opts-h",
                                    span { class: "opts-t", "{k.label()}" }
                                    span { class: "opts-d", "{k.hint()}" }
                                }
                                if let Some(lang) = language.clone() {
                                    p { class: "lang-chip",
                                        Icon { name: "translate" }
                                        if k.is_build() { "Writing in {lang} · voices are English. " } else { "Writing in {lang}. " }
                                        SettingsLink { tab: "language" }
                                    }
                                }
                                match k {
                                    Output::Session => rsx! {
                                        div { class: "opt-l", "Style" }
                                        div { class: "style-grid", role: "radiogroup", aria_label: "Style",
                                            for st in STYLES.iter() {
                                                button {
                                                    key: "{st.id}",
                                                    class: if *style.read() == st.id { "style on" } else { "style" },
                                                    role: "radio",
                                                    "aria-checked": if *style.read() == st.id { "true" } else { "false" },
                                                    title: "{st.blurb}",
                                                    onclick: move |_| { picked.set(true); style.set(st.id.to_string()); },
                                                    span { class: "sw", style: "background-image:url({thumb_url(st.id)})" }
                                                    span { class: "style-n", "{st.label}" }
                                                }
                                            }
                                        }
                                        if let Some(sum) = deck_summary(&cfg, n_src) {
                                            p { class: "opt-hint",
                                                "{sum} · "
                                                SettingsLink { tab: "defaults", text: "Change defaults in Settings".to_string() }
                                            }
                                        }
                                    },
                                    Output::Audio => rsx! {
                                        div { class: "opt-l", "Format" }
                                        div { class: "ao-formats", role: "radiogroup", aria_label: "Format",
                                            for (id, name, blurb, _) in AUDIO_FORMATS {
                                                button {
                                                    key: "{id}",
                                                    class: if audio_format.read().as_str() == id { "ao-f on" } else { "ao-f" },
                                                    role: "radio",
                                                    "aria-checked": if audio_format.read().as_str() == id { "true" } else { "false" },
                                                    onclick: move |_| {
                                                        picked.set(true);
                                                        audio_format.set(id.to_string());
                                                        fit_length(audio_format, audio_length);
                                                    },
                                                    span { class: "ao-n", "{name}" }
                                                    span { class: "ao-d", "{blurb}" }
                                                }
                                            }
                                        }
                                        {
                                            let offered = offered_lengths(&audio_format.read());
                                            rsx! {
                                                if !offered.is_empty() {
                                                    div { class: "ao-len", role: "radiogroup", aria_label: "Length",
                                                        span { class: "opt-l", "Length" }
                                                        for l in offered.iter().copied() {
                                                            button {
                                                                key: "{l}",
                                                                class: if audio_length.read().as_str() == l { "chip on" } else { "chip" },
                                                                role: "radio",
                                                                "aria-checked": if audio_length.read().as_str() == l { "true" } else { "false" },
                                                                onclick: move |_| { picked.set(true); audio_length.set(l.to_string()); },
                                                                {match l { "shorter" => "Shorter", "longer" => "Longer", _ => "Default" }}
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        if let (Some(host), Some(second)) = (cfg.get(keys::SPEAKER1_NAME), cfg.get(keys::SPEAKER2_NAME)) {
                                            p { class: "opt-hint",
                                                if audio_format.read().as_str() == "brief" {
                                                    "Brief: {host} alone, about 2 minutes. "
                                                } else {
                                                    "Voices: {host} and {second}. "
                                                }
                                                SettingsLink { tab: "voices", text: "Change in Settings › Voices".to_string() }
                                            }
                                        }
                                        label { class: "opt-l", r#for: "ao-focus", "Focus" }
                                        input {
                                            id: "ao-focus",
                                            r#type: "text",
                                            placeholder: "A topic, an audience or a level (optional)",
                                            value: "{audio_focus}",
                                            oninput: move |e| audio_focus.set(e.value()),
                                        }
                                    },
                                    Output::MindMap => rsx! {
                                        label { class: "opt-l", r#for: "mm-focus", "Focus" }
                                        input {
                                            id: "mm-focus",
                                            r#type: "text",
                                            placeholder: "Centre the map on a topic (optional)",
                                            value: "{mm.focus}",
                                            oninput: move |e| { let mut f = mm.focus; f.set(e.value()); },
                                            onkeydown: move |e: Event<KeyboardData>| {
                                                if e.key() == Key::Enter { generate(Output::MindMap); }
                                            },
                                        }
                                        if let Some(id) = fresh_map() {
                                            p { class: "opt-note",
                                                Icon { name: "check-circle-fill" }
                                                " A map of exactly these sources exists. "
                                                button { class: "link-btn", onclick: move |_| on_open.call(Some(Open::Map(id.clone()))), "Open it" }
                                            }
                                        }
                                    },
                                    Output::Notes => rsx! {
                                        label { class: "opt-l", r#for: "nt-focus", "Focus" }
                                        input {
                                            id: "nt-focus",
                                            r#type: "text",
                                            placeholder: "Centre the notes on a topic (optional)",
                                            value: "{nt.focus}",
                                            oninput: move |e| { let mut f = nt.focus; f.set(e.value()); },
                                            onkeydown: move |e: Event<KeyboardData>| {
                                                if e.key() == Key::Enter { generate(Output::Notes); }
                                            },
                                        }
                                        if let Some(id) = fresh_notes() {
                                            p { class: "opt-note",
                                                Icon { name: "check-circle-fill" }
                                                " Notes of exactly these sources exist. "
                                                button { class: "link-btn", onclick: move |_| on_open.call(Some(Open::Notes(id.clone()))), "Open them" }
                                            }
                                        }
                                    },
                                }

                                // What it costs, said before the click.
                                // Off in Settings, it is not said at all; over
                                // the limit is said below either way.
                                if k.is_build() {
                                    if show_cost && !over_limit {
                                        div { class: "est-banner", role: "status", aria_label: "Estimated cost",
                                            span { class: "est-i", aria_hidden: "true", Icon { name: "info-circle" } }
                                            div { class: "est-bt",
                                                if *est_loading.read() && est.read().is_none() {
                                                    span { class: "dim", "Working out the cost…" }
                                                } else if let Some(e) = est.read().as_ref() {
                                                    span { "Estimated " }
                                                    strong { aria_label: "{usd_range_spoken(e.total_low_usd, e.total_high_usd)} US dollars",
                                                        "{usd_range(e.total_low_usd, e.total_high_usd)}"
                                                    }
                                                    if any_unpriced(e) {
                                                        ", not counting a model with no price, so the real cost is unknown."
                                                    } else if e.limit_usd > 0.0 {
                                                        " · within your {usd(e.limit_usd)} limit."
                                                    } else {
                                                        "."
                                                    }
                                                } else if !est_err.read().is_empty() {
                                                    span { class: "dim", "The cost could not be estimated." }
                                                }
                                            }
                                        }
                                    }
                                } else {
                                    {
                                        let (loading, priced) = if k == Output::MindMap {
                                            (*mm.est_loading.read(), mm.est.read().as_ref().filter(|e| e.priced).map(|e| (e.cost_usd, e.model.clone(), e.input_tokens)))
                                        } else {
                                            (*nt.est_loading.read(), nt.est.read().as_ref().filter(|e| e.priced).map(|e| (e.cost_usd, e.model.clone(), e.input_tokens)))
                                        };
                                        rsx! {
                                            p { class: "opt-cost",
                                                if loading && priced.is_none() {
                                                    span { class: "dim", "Working out the cost…" }
                                                } else if let Some((cost, model, tokens)) = priced {
                                                    span { title: "{model}, one call over about {count_short(tokens as i64)} tokens of your sources",
                                                        "About "
                                                        strong { "{usd(cost)}" }
                                                        if k == Output::MindMap { ", a few seconds." } else { ", under a minute." }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                                // Over the limit is said whether or not costs
                                // are shown: it is not a note about cost but
                                // the reason Generate is off.
                                if let Some(e) = est.read().as_ref().filter(|e| k.is_build() && e.over_limit) {
                                    LimitNote { e: e.clone(), audio: k == Output::Audio, class: "opt-err" }
                                }
                                if !gen_err.read().is_empty() {
                                    div { class: "opt-err", role: "alert", "{gen_err}" }
                                }
                                div { class: "opts-a",
                                    if k.is_build() {
                                        button {
                                            title: "Every step and what it costs, before you start",
                                            onclick: move |_| {
                                                est_open.set(true);
                                                if est.read().is_none() && !*est_loading.read() {
                                                    spawn(fetch_estimate(()));
                                                }
                                            },
                                            "Estimate cost"
                                        }
                                    }
                                    span { class: "grow" }
                                    button { class: "ghost", onclick: move |_| chosen.set(None), "Cancel" }
                                    button {
                                        class: "primary",
                                        disabled: *generating.read()
                                            || (k.is_build() && over_limit)
                                            || (k == Output::MindMap && *mm.making.read())
                                            || (k == Output::Notes && *nt.making.read()),
                                        onclick: move |_| generate(k),
                                        if *generating.read() { "Starting…" } else { "Generate {k.label().to_lowercase()}" }
                                    }
                                }
                            }
                        }

                        div { class: "sec-h out-h", span { class: "sec-t", "In this collection" } }
                        div { class: "outs-list", aria_live: "polite",
                            if *mm.making.read() {
                                PendingRow { icon: "diagram-3", text: "Making a mind map…" }
                            }
                            if *nt.making.read() {
                                PendingRow { icon: "journal-text", text: "Writing study notes…" }
                            }
                            if !mm.err.read().is_empty() {
                                ErrRow {
                                    icon: "diagram-3",
                                    title: "The mind map could not be made",
                                    detail: mm.err.read().clone(),
                                    on_dismiss: move |_| { let mut e = mm.err; e.set(String::new()); },
                                }
                            }
                            if !nt.err.read().is_empty() {
                                ErrRow {
                                    icon: "journal-text",
                                    title: "The study notes could not be written",
                                    detail: nt.err.read().clone(),
                                    on_dismiss: move |_| { let mut e = nt.err; e.set(String::new()); },
                                }
                            }
                            if !*loaded.read() {
                                for i in 0..2 {
                                    div { key: "{i}", class: "out-row skel-row",
                                        div { class: "skel-line" }
                                    }
                                }
                            } else if !load_err.read().is_empty() && made.is_empty() {
                                ErrRow {
                                    title: "What is in this collection could not be loaded",
                                    detail: load_err.read().clone(),
                                    on_retry: reload_all,
                                }
                            } else if made.is_empty() && !*mm.making.read() && !*nt.making.read() {
                                div { class: "outs-empty",
                                    "Pick a format above. What you make appears here."
                                }
                            }
                            for m in made {
                                match m.clone() {
                                    Made::Session(s) => rsx! {
                                        SessionRow {
                                            key: "{m.key()}",
                                            live: live.contains(&s.sid),
                                            s: s.clone(),
                                            on_changed: move |_| { spawn(async move { load().await }); },
                                            on_rename: { let sid = s.sid.clone(); move |t: String| rename((Target::Session(sid.clone()), t)) },
                                            on_delete: { let sid = s.sid.clone(); move |_| remove(Target::Session(sid.clone())) },
                                        }
                                    },
                                    Made::Map(x) => rsx! {
                                        ItemRow {
                                            key: "{m.key()}",
                                            icon: "diagram-3",
                                            what: "mind map",
                                            title: x.title.clone(),
                                            facts: {
                                                let mut f = format!("{} topics", x.node_count);
                                                if !x.focus.is_empty() { f.push_str(&format!(" · {}", x.focus)); }
                                                f
                                            },
                                            when_ms: x.created_ms,
                                            on: open == Some(Open::Map(x.id.clone())),
                                            on_open: { let id = x.id.clone(); move |_| on_open.call(Some(Open::Map(id.clone()))) },
                                            on_rename: { let id = x.id.clone(); move |t: String| rename((Target::Map(id.clone()), t)) },
                                            on_delete: { let id = x.id.clone(); move |_| remove(Target::Map(id.clone())) },
                                        }
                                    },
                                    Made::Notes(x) => rsx! {
                                        ItemRow {
                                            key: "{m.key()}",
                                            icon: "journal-text",
                                            what: "study notes",
                                            title: x.title.clone(),
                                            facts: {
                                                let mut f = notes::counts(x.ideas, x.questions, x.terms);
                                                if !x.focus.is_empty() { f.push_str(&format!(" · {}", x.focus)); }
                                                f
                                            },
                                            when_ms: x.created_ms,
                                            on: open == Some(Open::Notes(x.id.clone())),
                                            on_open: { let id = x.id.clone(); move |_| on_open.call(Some(Open::Notes(id.clone()))) },
                                            on_rename: { let id = x.id.clone(); move |t: String| rename((Target::Notes(id.clone()), t)) },
                                            on_delete: { let id = x.id.clone(); move |_| remove(Target::Notes(id.clone())) },
                                        }
                                    },
                                }
                            }
                        }
                    }
                } else {
                    chat::AskTab { st: chat, on_send: move |t: String| { spawn(send_chat(t)); } }
                }
            }

            // ── the viewer ──────────────────────────────────────────────────
            match open.clone() {
                Some(Open::Notes(nid)) => rsx! {
                    aside { class: "panel apps",
                        notes::NotesView {
                            sid: cid_s.read().clone(),
                            title: nt.notes.read().iter().find(|n| n.id == nid).map(|n| n.title.clone()).unwrap_or_default(),
                            id: nid,
                            on_close: move |_| { on_open.call(None); src_open.set(false); },
                        }
                    }
                },
                Some(Open::Map(mid)) => rsx! {
                    aside { class: "panel apps",
                        MindMapView {
                            sid: cid_s.read().clone(),
                            title: mm.maps.read().iter().find(|m| m.id == mid).map(|m| m.title.clone()).unwrap_or_default(),
                            id: mid,
                            on_close: move |_| { on_open.call(None); src_open.set(false); },
                            on_ask: move |q: String| { spawn(ask_sources(q)); },
                        }
                    }
                },
                None => rsx! {},
            }
        }

        if let Some(o) = cited.read().clone() {
            crate::source::SourceDrawer {
                cid: cid_s.read().clone(),
                opened: o,
                on_close: move |_| cited.set(None),
            }
        }

        if *est_open.read() {
            CostDialog {
                est: est.read().clone(),
                audio: (*chosen.read() == Some(Output::Audio))
                    .then(|| audio_desc(&audio_format.read(), &audio_length.read())),
                loading: *est_loading.read(),
                err: est_err.read().clone(),
                on_close: move |_| est_open.set(false),
                on_retry: move |_| { spawn(fetch_estimate(())); },
                on_build: move |_| {
                    est_open.set(false);
                    if let Some(k) = *chosen.peek() { generate(k); }
                },
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{UPLOAD_ACCEPT, UPLOAD_EXTS, audio_desc, upload_problem};

    #[test]
    fn an_audio_overview_is_named_by_its_format_and_length() {
        assert_eq!(audio_desc("brief", "default"), "Brief");
        assert_eq!(audio_desc("brief", "shorter"), "Brief");
        assert_eq!(audio_desc("deep_dive", "shorter"), "Deep Dive · shorter");
        assert_eq!(audio_desc("debate", "default"), "Debate");
    }

    /// The picker offers exactly what the check lets through.
    #[test]
    fn the_picker_and_the_check_agree() {
        let accept: Vec<&str> = UPLOAD_ACCEPT.split(',').collect();
        let exts: Vec<String> = UPLOAD_EXTS.iter().map(|e| format!(".{e}")).collect();
        assert_eq!(accept, exts);
    }

    #[test]
    fn a_file_is_refused_before_it_is_sent_and_says_why() {
        assert_eq!(upload_problem("Report.PDF", 1000.0), None);
        assert_eq!(upload_problem("notes.markdown", 10.0), None);
        let kind = upload_problem("photo.jpg", 1000.0).unwrap();
        assert!(kind.contains("PDF"), "{kind}");
        assert!(upload_problem("no-extension", 1000.0).is_some());
        let big = upload_problem("big.pdf", 30.0 * 1024.0 * 1024.0).unwrap();
        assert!(big.contains("30 MB") && big.contains("25 MB"), "{big}");
        assert_eq!(upload_problem("max.pdf", 25.0 * 1024.0 * 1024.0), None);
        assert!(upload_problem("empty.txt", 0.0).is_some());
    }
}
