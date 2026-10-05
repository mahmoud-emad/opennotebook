//! opennotebook, the end-user app.
//!
//! What a person does here: keep collections, each one set of sources and
//! everything made from them (narrated slides, audio overviews, mind maps,
//! study notes); add sources to one, make as many outputs of any kind as they
//! like, watch them prepare without being kept waiting, and open a ready one.
//!
//! Two things this app deliberately does not do. It does not hand-write
//! JSON-RPC: every call goes through `opennotebook_sdk`, the client generated
//! from the server's own oschema, so a schema change breaks the build rather
//! than the page. And it does not hard-code its own mount: the API base is
//! derived from `location.pathname`, because hard-coding `/api/session` is
//! exactly what broke the player when it was first served behind a routing
//! prefix, and only a real browser found it.

use dioxus::prelude::*;
use opennotebook_sdk::session::{CollectionListInput, CollectionSummary};
use opennotebook_sdk::styles::STYLES;

mod api;
mod chat;
mod collection;
mod dialogs;
mod errors;
mod home;
mod mindmap;
mod notes;
mod outputs;
mod pick;
mod routes;
mod settings;
mod source;

use api::{clean_rpc_error, client, gloo_sleep, sources_client};
use collection::CollectionPage;
use dialogs::{Ask, AskDialog};
use home::{
    CollectionCard, HOME_N, ListError, NewCard, SkelGrid, Sort, coll_title, create_collection,
    ordered,
};
use pick::{Pick, PickBar};
use routes::{Open, View, route_from_location, route_url, set_route};
use settings::{SettingsDialog, use_settings_provider};

fn main() {
    dioxus::launch(App);
}

/// The thumbnail URL for a style, relative to the bundle's mount.
///
/// `asset!` renders a ROOT-absolute `/assets/…`, which is the one form that
/// cannot be relied on: the studio serves the bundle at `/ui/`, a proxy in
/// front may add a prefix, and a leading slash ignores both. Measured in a
/// real browser on 2026-09-22 — the swatches resolved to the origin's
/// `/assets/…` and 404'd, while the same file under the mount answered 200. dx's own script and wasm tags are relative for this reason;
/// this puts `asset!` back on the same footing.
fn thumb_url(id: &str) -> String {
    let url = style_thumb(id).to_string();
    url.trim_start_matches('/').to_string()
}

/// The display name of a style id, falling back to the id itself.
fn style_label(id: &str) -> &str {
    STYLES.iter().find(|s| s.id == id).map_or(id, |s| s.label)
}

/// The sample slide rendered in each style, for the picker.
///
/// One sample slide per style, drawn with the same kit a session's slides
/// get and served by the studio at `/api/session/style_sample`, screenshotted
/// and committed (`scripts/style-thumbnails.py`). `asset!` needs a literal
/// path, so this is a match rather than a formatted lookup; an id with no
/// thumbnail falls back to the default's, which is wrong-looking rather than
/// blank and shows up the moment a style is added without one.
fn style_thumb(id: &str) -> Asset {
    match id {
        "professional" => asset!("/assets/styles/professional.jpg"),
        "bento" => asset!("/assets/styles/bento.jpg"),
        "instructional" => asset!("/assets/styles/instructional.jpg"),
        "scientific" => asset!("/assets/styles/scientific.jpg"),
        "sketchnote" => asset!("/assets/styles/sketchnote.jpg"),
        "clay" => asset!("/assets/styles/clay.jpg"),
        "bricks" => asset!("/assets/styles/bricks.jpg"),
        _ => asset!("/assets/styles/editorial.jpg"),
    }
}

/// What the server holds as a collection's sources, as the rows the sources
/// panel shows.
async fn server_sources(cid: &str) -> Result<Vec<Src>, String> {
    let out = sources_client()?
        .source_list(opennotebook_sdk::sources::SourceListInput {
            sid: cid.to_string(),
        })
        .await
        .map_err(|e| clean_rpc_error(&e.to_string()))?;
    Ok(out
        .sources
        .into_iter()
        .map(|s| Src {
            detail: if s.url.is_empty() {
                format!("{} · {} words", file_kind(&s.name), s.chars / 6)
            } else {
                format!("{} · {} words", short_host(&s.url), s.chars / 6)
            },
            name: if s.title.is_empty() {
                s.name.clone()
            } else {
                s.title
            },
            file: s.name,
            ok: true,
            url: s.url,
            icon: String::new(),
        })
        .collect())
}

/// What a source without a page is, from its stored name: a typed note or a
/// report is Markdown; an uploaded file says its own type, "PDF", "DOCX".
fn file_kind(name: &str) -> String {
    match name.rsplit_once('.').map(|(_, e)| e.to_ascii_lowercase()) {
        Some(e) if e != "md" && !e.is_empty() => e.to_ascii_uppercase(),
        _ => "note".into(),
    }
}

/// One of the studio's icons (`opennotebook_sdk::icons`, Bootstrap Icons),
/// drawn inline so the app needs no icon font and nothing from the network.
/// Decorative: the control it sits in carries the accessible name.
#[component]
fn Icon(name: &'static str, #[props(default)] class: &'static str) -> Element {
    let inner = opennotebook_sdk::icons::icon(name).unwrap_or_default();
    rsx! {
        svg {
            class: "ic {class}",
            view_box: "0 0 16 16",
            "aria-hidden": "true",
            "focusable": "false",
            dangerous_inner_html: "{inner}",
        }
    }
}

/// Move keyboard focus to the element with this id, if there is one.
fn focus_id(id: &str) {
    use wasm_bindgen::JsCast;
    if let Some(el) = web_sys::window()
        .and_then(|w| w.document())
        .and_then(|d| d.get_element_by_id(id))
        .and_then(|el| el.dyn_into::<web_sys::HtmlElement>().ok())
    {
        let _ = el.focus();
    }
}

/// What a collection can make: the four tiles of its Studio.
#[derive(Clone, Copy, PartialEq, Debug)]
enum Output {
    /// Narrated slides: a build, minutes long, priced per deck.
    Session,
    /// A mind map of the sources: one model call, a few seconds, a fraction of
    /// a cent. Not a build, so none of the build's options apply to it.
    MindMap,
    /// Study notes of the sources: one model call, under a minute, about a
    /// cent. Not a build either.
    Notes,
    /// An audio overview: a build like a deck, minutes long, but heard and
    /// not seen. Its parts are chapters and no slides are drawn.
    Audio,
}

impl Output {
    /// The four, in the order the tiles show them.
    const ALL: [Output; 4] = [
        Output::Session,
        Output::Audio,
        Output::MindMap,
        Output::Notes,
    ];
    /// How the chat agent is told what the person is making.
    fn wire(self) -> &'static str {
        match self {
            Output::Session => "session",
            Output::MindMap => "mindmap",
            Output::Notes => "notes",
            Output::Audio => "audio",
        }
    }
    fn label(self) -> &'static str {
        match self {
            Output::Session => "Narrated slides",
            Output::MindMap => "Mind map",
            Output::Notes => "Study notes",
            Output::Audio => "Audio overview",
        }
    }
    fn icon(self) -> &'static str {
        match self {
            Output::Session => "easel",
            Output::MindMap => "diagram-3",
            Output::Notes => "journal-text",
            Output::Audio => "soundwave",
        }
    }
    /// One line on its tile: what it is.
    fn blurb(self) -> &'static str {
        match self {
            Output::Session => "Slides with a spoken script you can interrupt.",
            Output::MindMap => "The topics as a tree. Click one to ask about it.",
            Output::Notes => "Key ideas, a quiz and a glossary, all cited.",
            Output::Audio => "A conversation about your sources, to listen to.",
        }
    }
    /// Time and cost, said plainly before the choice: the kinds differ about
    /// a hundredfold, and a person should not find that out after the click.
    fn hint(self) -> &'static str {
        match self {
            // From the live estimates on 2026-10-03: slides $0.12 to $0.17
            // typical, audio $0.09 to $0.13, a map $0.0005 to $0.005, notes
            // $0.002 to $0.007, over sources from a note to a 190k-char paper.
            Output::Session => "A few minutes · tens of cents",
            Output::MindMap => "Seconds · under a cent",
            Output::Notes => "Under a minute · about a cent or less",
            Output::Audio => "A few minutes · tens of cents",
        }
    }
    /// A build in the background, as opposed to one call that answers.
    fn is_build(self) -> bool {
        matches!(self, Output::Session | Output::Audio)
    }
}

/// A length of audio as m:ss.
fn mmss(ms: i64) -> String {
    let s = (ms.max(0) + 500) / 1000;
    format!("{}:{:02}", s / 60, s % 60)
}

/// The page's one error banner, under the top bar. Every action that can fail
/// without a pane of its own to say so (a rename, a pin, a delete, a list that
/// did not load) reports here, so nothing fails silently.
#[derive(Clone, Copy)]
struct Flash(Signal<String>);

/// Say that something failed, in the shared banner.
fn report(msg: impl Into<String>) {
    if let Some(Flash(mut f)) = try_consume_context::<Flash>() {
        f.set(msg.into());
    }
}

/// A short message at the bottom of the screen that goes on its own: for a
/// refusal the person can act on at once, like a seventh empty collection,
/// where a banner that stays until dismissed would outlast its point. Each
/// message has a number, so the timer of an older one never clears a newer.
#[derive(Clone, Copy)]
struct Snack(Signal<Option<(u32, String)>>);

/// How long a snackbar stays, in ms.
const SNACK_MS: i32 = 6000;

/// Say something in the snackbar.
fn snack(msg: impl Into<String>) {
    if let Some(Snack(mut s)) = try_consume_context::<Snack>() {
        let n = s.peek().as_ref().map_or(0, |(n, _)| n + 1);
        s.set(Some((n, msg.into())));
    }
}

#[component]
fn App() -> Element {
    let mut view = use_signal(route_from_location);
    let mut collections = use_signal(Vec::<CollectionSummary>::new);
    // Whether the first `collection_list` has answered: without it the page
    // could not tell "you have none" from "we have not asked yet", and showed
    // the empty state on first paint before flashing into a full grid.
    let mut loaded = use_signal(|| false);
    let mut load_err = use_signal(String::new);
    let mut grid = use_signal(|| true);
    let mut sort = use_signal(|| Sort::Recent);
    let mut sort_open = use_signal(|| false);
    // The studio's settings, read once for the page's hints, and the dialog.
    let settings = use_settings_provider();
    let mut settings_open = settings.open;
    // A collection is being made for the New button.
    let mut creating = use_signal(|| false);
    // Something that failed and is not tied to one pane: a collection that
    // could not be made, a rename or a delete that was refused. Shown under
    // the top bar until dismissed; any screen reports through `report`.
    let mut flash = use_context_provider(|| Flash(Signal::new(String::new()))).0;
    let mut snack_s = use_context_provider(|| Snack(Signal::new(None))).0;
    use_effect(move || {
        if let Some((n, _)) = *snack_s.read() {
            spawn(async move {
                gloo_sleep(SNACK_MS).await;
                if snack_s.peek().as_ref().is_some_and(|(m, _)| *m == n) {
                    snack_s.set(None);
                }
            });
        }
    });
    // The kind to open a new collection on, from an address that asked for one.
    // Kept with the collection it was asked for, so it opens that one on the
    // kind and no other: Back to a collection never reopens a stale tile.
    let mut preselect = use_signal(|| None::<(String, Output)>);
    // The open collection's name, for the breadcrumb; the page keeps it.
    let crumb = use_signal(String::new);
    // The one confirm/prompt dialog every screen asks through. Never the
    // browser's own: those cannot be styled and block the page.
    let ask_state = use_context_provider(|| Signal::new(None::<Ask>));
    // What is picked on All collections, to delete several at once. See `pick`.
    let mut picks = use_context_provider(|| Signal::new(pick::Picks::default()));
    // What that page shows, in its order: what a shift-click's range runs over
    // and what "Select all" means.
    let order = use_memo(move || {
        ordered(&collections.read(), *sort.read())
            .into_iter()
            .map(|c| c.cid)
            .collect::<Vec<Pick>>()
    });
    use_context_provider(|| order);
    use_effect(move || {
        let o = order.read();
        if picks.peek().set.iter().any(|p| !o.contains(p)) {
            picks.write().keep(&o);
        }
    });

    let reload = move || async move {
        let got = async {
            client()?
                .collection_list(CollectionListInput {})
                .await
                .map_err(|e| clean_rpc_error(&e.to_string()))
        }
        .await;
        match got {
            Ok(out) => {
                collections.set(out.collections);
                load_err.set(String::new());
            }
            Err(e) => load_err.set(e),
        }
        // Set even when the call failed: the page has its answer, and leaving
        // it false would spin skeletons forever against a service that is down.
        loaded.set(true);
    };

    // The address bar follows the screen, and the browser's Back button works.
    //
    // Two halves. The effect writes the URL whenever `view` changes, but only
    // when it disagrees with the address bar, or the popstate handler below
    // would push the very state it was told about and Back would never leave.
    // The listener is the other direction: it reads the screen back out of
    // whatever URL the browser just restored.
    use_effect(move || {
        let v = view.read().clone();
        if matches!(v, View::New(_)) {
            return;
        }
        let here = route_from_location();
        if here != v {
            // Opening a map beside the same collection replaces; anything
            // else is a page of its own.
            let beside = matches!((&here, &v),
                (View::Collection { cid: a, .. }, View::Collection { cid: b, .. }) if a == b);
            set_route(&v, beside);
        } else {
            let path = web_sys::window()
                .and_then(|w| w.location().pathname().ok())
                .unwrap_or_default();
            if path != route_url(&v) {
                // The same screen under an old address: correct it in place.
                set_route(&v, true);
            }
        }
        // A tile asked for by an old address belongs to the collection it
        // made; anywhere else, it is forgotten.
        let stale = match (&v, preselect.peek().as_ref()) {
            (View::Collection { cid, .. }, Some((p, _))) => cid != p,
            (_, Some(_)) => true,
            _ => false,
        };
        if stale {
            preselect.set(None);
        }
        // Selecting is a mode of All collections; leaving it ends it.
        if v != View::All && picks.peek().on {
            picks.write().stop();
        }
        // Back on a list: it may have changed under the collection page.
        if matches!(v, View::Home | View::All) {
            spawn(async move { reload().await });
        }
    });
    use_future(move || async move {
        use wasm_bindgen::JsCast;
        let Some(w) = web_sys::window() else { return };
        let cb = wasm_bindgen::closure::Closure::<dyn FnMut()>::new(move || {
            view.set(route_from_location());
        });
        let _ = w.add_event_listener_with_callback("popstate", cb.as_ref().unchecked_ref());
        // Leaked deliberately: the listener has to outlive this future, and the
        // app is the page — there is no unmount to clean up after.
        cb.forget();
    });

    // Files dropped anywhere but the sources panel are refused, not opened:
    // the browser's default for a dropped file is to navigate to it, which
    // throws away the page and whatever was being typed. The panel prevents
    // the default itself first, so this only catches what it did not take.
    use_hook(|| {
        use wasm_bindgen::JsCast;
        let cb = wasm_bindgen::closure::Closure::<dyn FnMut(web_sys::DragEvent)>::new(
            |e: web_sys::DragEvent| {
                let Some(dt) = e.data_transfer() else { return };
                let files = dt
                    .types()
                    .iter()
                    .any(|t| t.as_string().as_deref() == Some("Files"));
                if files && !e.default_prevented() {
                    e.prevent_default();
                    dt.set_drop_effect("none");
                }
            },
        );
        if let Some(w) = web_sys::window() {
            for ev in ["dragover", "drop"] {
                let _ = w.add_event_listener_with_callback(ev, cb.as_ref().unchecked_ref());
            }
        }
        // Leaked deliberately, as the popstate listener is: the app is the page.
        cb.forget();
    });

    // An old `new-…` address: make the collection, then open it on that kind.
    // The address is replaced rather than pushed, so Back does not land on it
    // and make another.
    use_effect(move || {
        if let View::New(kind) = view.read().clone() {
            spawn(async move {
                match create_collection().await {
                    Ok(cid) => {
                        preselect.set(Some((cid.clone(), kind)));
                        let v = View::Collection { cid, open: None };
                        set_route(&v, true);
                        view.set(v);
                    }
                    Err(e) => {
                        snack(e);
                        set_route(&View::Home, true);
                        view.set(View::Home);
                    }
                }
            });
        }
    });

    let mut new_collection = move || {
        if *creating.peek() {
            return;
        }
        creating.set(true);
        spawn(async move {
            match create_collection().await {
                Ok(cid) => {
                    preselect.set(None);
                    view.set(View::Collection { cid, open: None });
                }
                // The studio's own words: the empty-collection limit is
                // decided there, for every caller, and says what to do.
                Err(e) => snack(e),
            }
            creating.set(false);
        });
    };

    // While a list is on screen and something on it is still changing (an
    // output preparing, a name the studio has not given yet, a cover still being
    // designed), look again every
    // few seconds, so a card catches up on its own. Idle otherwise.
    use_future(move || async move {
        loop {
            gloo_sleep(5000).await;
            let on_list = matches!(*view.peek(), View::Home | View::All);
            // A name is only on its way while naming is on in Settings.
            let auto_name = settings
                .doc
                .peek()
                .value(settings::keys::AUTO_NAME)
                .as_deref()
                != Some("off");
            let covers_on =
                settings.doc.peek().value(settings::keys::COVERS).as_deref() != Some("off");
            let moving = collections.peek().iter().any(|c| {
                c.preparing > 0
                    || (auto_name && c.title_auto && c.title.trim().is_empty() && c.sources > 0)
                    || home::cover_pending(c, covers_on)
            });
            if on_list && moving {
                reload().await;
            }
        }
    });

    let open_coll = move |cid: String| {
        preselect.set(None);
        view.set(View::Collection { cid, open: None });
    };
    let v = view.read().clone();
    let list = ordered(&collections.read(), *sort.read());
    let n = list.len();
    let on_list = matches!(v, View::Home | View::All);

    rsx! {
        style { {CSS} }
        header {
            // The logo is the way home, as on every site.
            button {
                class: "brand",
                title: "Home",
                aria_label: "Studio home",
                onclick: move |_| view.set(View::Home),
                span { class: "mark", Icon { name: "collection-play" } }
                h1 { "Studio" }
            }
            // Where you are.
            match &v {
                View::All => rsx! {
                    span { class: "crumb",
                        span { class: "sep", aria_hidden: "true", "/" }
                        span { class: "here", "All collections" }
                    }
                },
                View::Collection { .. } => rsx! {
                    span { class: "crumb",
                        span { class: "sep", aria_hidden: "true", "/" }
                        span { class: if crumb.read().trim().is_empty() { "here untitled" } else { "here" },
                            "{coll_title(&crumb.read())}"
                        }
                    }
                },
                _ => rsx! {},
            }
            span { class: "grow" }
            if v == View::All && n > 0 {
                // Picking several to delete at once. The checkbox on a card
                // starts it too, but only where there is a pointer to show it;
                // this is the way in on a touch screen.
                button {
                    class: if picks.read().on { "ghost on" } else { "ghost" },
                    title: if picks.read().on { "Stop selecting (Esc)" } else { "Select several" },
                    aria_label: "Select",
                    "aria-pressed": if picks.read().on { "true" } else { "false" },
                    onclick: move |_| {
                        let on = picks.peek().on;
                        if on { picks.write().stop() } else { picks.write().start() }
                    },
                    Icon { name: "check2-square" }
                    span { class: "hide-sm", "Select" }
                }
                div { class: "seg",
                    button {
                        class: if *grid.read() { "on" } else { "" },
                        onclick: move |_| grid.set(true),
                        title: "Grid",
                        aria_label: "Show as a grid",
                        "aria-pressed": if *grid.read() { "true" } else { "false" },
                        Icon { name: "grid-3x3-gap" }
                    }
                    button {
                        class: if *grid.read() { "" } else { "on" },
                        onclick: move |_| grid.set(false),
                        title: "List",
                        aria_label: "Show as a list",
                        "aria-pressed": if *grid.read() { "false" } else { "true" },
                        Icon { name: "list-ul" }
                    }
                }
                div { class: "menu",
                    button {
                        class: "ghost",
                        title: "Sort: {sort.read().label()}",
                        aria_label: "Sort: {sort.read().label()}",
                        "aria-haspopup": "menu",
                        "aria-expanded": if *sort_open.read() { "true" } else { "false" },
                        onclick: move |_| { let o = *sort_open.read(); sort_open.set(!o); },
                        Icon { name: "sort-down" }
                        span { class: "hide-sm", "{sort.read().label()}" }
                    }
                    if *sort_open.read() {
                        // Same dismissal as the card menu; see `dots-veil`.
                        div {
                            class: "dots-veil",
                            onclick: move |e: Event<MouseData>| {
                                e.stop_propagation();
                                e.prevent_default();
                                sort_open.set(false);
                            },
                        }
                        div { class: "menu-pop", role: "menu", aria_label: "Sort by",
                            for opt in [Sort::Recent, Sort::Title] {
                                button {
                                    key: "{opt.label()}",
                                    class: if *sort.read() == opt { "on" } else { "" },
                                    role: "menuitemradio",
                                    "aria-checked": if *sort.read() == opt { "true" } else { "false" },
                                    onclick: move |_| { sort.set(opt); sort_open.set(false); },
                                    "{opt.label()}"
                                }
                            }
                        }
                    }
                }
            }
            // The one primary of a list page. Only there: on a collection,
            // where the person is already filling one, it made another empty
            // collection per click.
            if on_list {
                button {
                    class: "primary",
                    title: "Start a new collection of sources",
                    aria_label: "New collection",
                    disabled: *creating.read(),
                    onclick: move |_| new_collection(),
                    Icon { name: "plus-lg" }
                    span { class: "hide-sm", "New collection" }
                }
            }
            button {
                class: "icon-btn",
                title: "Settings",
                aria_label: "Settings",
                onclick: move |_| settings_open.set(Some("defaults".to_string())),
                Icon { name: "gear", class: "lg" }
            }
        }
        if !flash.read().is_empty() {
            div { class: "flash", role: "alert",
                Icon { name: "exclamation-triangle-fill" }
                span { class: "grow", "{flash}" }
                button {
                    class: "icon-btn",
                    title: "Dismiss",
                    aria_label: "Dismiss",
                    onclick: move |_| flash.set(String::new()),
                    Icon { name: "x-lg" }
                }
            }
        }
        if let Some((n, msg)) = snack_s.read().clone() {
            div { key: "{n}", class: "snack", role: "alert",
                Icon { name: "exclamation-triangle-fill" }
                span { class: "grow", "{msg}" }
                button {
                    class: "icon-btn",
                    title: "Dismiss",
                    aria_label: "Dismiss",
                    onclick: move |_| snack_s.set(None),
                    Icon { name: "x-lg" }
                }
            }
        }
        if settings_open.read().is_some() {
            SettingsDialog { on_close: move |_| { spawn(settings::reload(settings)); } }
        }
        if ask_state.read().is_some() {
            AskDialog {}
        }

        match v.clone() {
            View::Collection { cid, open } => rsx! {
                // Keyed on the collection, so opening another one mounts a
                // fresh page rather than reusing this one's state.
                CollectionPage {
                    key: "{cid}",
                    cid: cid.clone(),
                    open,
                    start: preselect.peek().as_ref().filter(|(p, _)| *p == cid).map(|(_, k)| *k),
                    crumb,
                    on_open: {
                        let cid = cid.clone();
                        move |o: Option<Open>| view.set(View::Collection { cid: cid.clone(), open: o })
                    },
                    on_gone: move |_| view.set(View::Home),
                }
            },
            View::New(_) => rsx! {
                main {
                    div { class: "empty",
                        div { class: "spinner" }
                        div { class: "empty-t", "Starting a collection…" }
                    }
                }
            },
            View::Home => rsx! {
                main {
                    div { class: "home-h",
                        h2 { class: "page-t", "Recent collections" }
                        if n > 0 {
                            button {
                                class: "more-link",
                                onclick: move |_| view.set(View::All),
                                "View all ({n})"
                                Icon { name: "chevron-right" }
                            }
                        }
                    }
                    if !*loaded.read() {
                        SkelGrid { n: 4 }
                    } else if !load_err.read().is_empty() && n == 0 {
                        ListError { err: load_err.read().clone(), on_retry: move |_| { spawn(async move { reload().await }); } }
                    } else {
                        div { class: "grid",
                            for c in list.iter().take(HOME_N).cloned() {
                                CollectionCard {
                                    key: "{c.cid}",
                                    c,
                                    list: false,
                                    on_open: open_coll,
                                    on_changed: move |_| { spawn(async move { reload().await }); },
                                }
                            }
                            NewCard { busy: *creating.read(), on_new: move |_| new_collection() }
                        }
                        if n == 0 {
                            p { class: "home-lead", "A collection holds your sources and everything you make from them." }
                        }
                    }
                }
            },
            View::All => rsx! {
                PickBar { on_deleted: move |_| { spawn(async move { reload().await }); } }
                main { class: if picks.read().on { "selecting" } else { "" },
                    div { class: "page-h",
                        div {
                            h2 { class: "page-t", "All collections" }
                            if *loaded.read() && n > 0 {
                                p { class: "page-d num", if n == 1 { "1 collection" } else { "{n} collections" } }
                            }
                        }
                    }
                    if !*loaded.read() {
                        SkelGrid { n: 8 }
                    } else if !load_err.read().is_empty() && n == 0 {
                        ListError { err: load_err.read().clone(), on_retry: move |_| { spawn(async move { reload().await }); } }
                    } else if n == 0 {
                        div { class: "empty",
                            div { class: "empty-mark", Icon { name: "collection", class: "xl" } }
                            div { class: "empty-t", "No collections yet" }
                            div { class: "empty-d",
                                "A collection holds your sources and everything you make from them: narrated slides, audio, mind maps and notes."
                            }
                        }
                    } else {
                        div { class: if *grid.read() { "grid" } else { "grid list" },
                            for c in list.iter().cloned() {
                                CollectionCard {
                                    key: "{c.cid}",
                                    c,
                                    list: !*grid.read(),
                                    pickable: true,
                                    on_open: open_coll,
                                    on_changed: move |_| { spawn(async move { reload().await }); },
                                }
                            }
                        }
                    }
                }
            },
        }
    }
}

/// A card's ⋯ menu: the button in its corner and the list it opens. One for
/// every collection card and every output row, so all of them are acted on
/// the same way.
///
/// A sibling of the card's link or button, never inside it. Nesting a button in
/// an `<a>` is invalid HTML and behaves like it: the click reaches the link too
/// and the page navigates instead of opening the menu.
#[component]
fn CardMenu(
    /// The name of what it acts on, so its button is "More actions for X"
    /// rather than one of a dozen identical "More actions".
    label: String,
    /// Each line: what it says, and whether it destroys something.
    items: Vec<(String, bool)>,
    /// The line chosen, by its index in `items`.
    on_pick: EventHandler<usize>,
    /// Lines that cannot be chosen right now (their work is under way), by index.
    #[props(default)]
    off: Vec<usize>,
) -> Element {
    let mut open = use_signal(|| false);
    rsx! {
        div { class: "dots-wrap",
            button {
                class: "dots",
                title: "More",
                aria_label: "More actions for {label}",
                "aria-expanded": if *open.read() { "true" } else { "false" },
                onclick: move |e: Event<MouseData>| {
                    e.stop_propagation();
                    e.prevent_default();
                    let v = *open.read();
                    open.set(!v);
                },
                Icon { name: "three-dots" }
            }
            if *open.read() {
                // Clicking anywhere else closes it.
                //
                // A full-viewport backdrop rather than a document-level
                // listener. The listener version has to be added on open and
                // removed on close, survive the component unmounting while open,
                // and not fire for the very click that opened the menu — three
                // things to get wrong. A sibling element that swallows one click
                // has none of that state, and it also means the first click
                // outside only dismisses rather than activating whatever was
                // under it, which is what a menu should do.
                div {
                    class: "dots-veil",
                    onclick: move |e: Event<MouseData>| {
                        e.stop_propagation();
                        e.prevent_default();
                        open.set(false);
                    },
                }
                div { class: "dots-pop", role: "menu",
                    for (i, (label, bad)) in items.into_iter().enumerate() {
                        button {
                            key: "{i}",
                            class: if bad { "bad" } else { "" },
                            role: "menuitem",
                            disabled: off.contains(&i),
                            onclick: move |e: Event<MouseData>| {
                                e.stop_propagation();
                                e.prevent_default();
                                open.set(false);
                                on_pick.call(i);
                            },
                            "{label}"
                        }
                    }
                }
            }
        }
    }
}

/// One source on the sources panel.
#[derive(Clone, PartialEq)]
struct Src {
    /// What it is called: the page's title, or a note's first words.
    name: String,
    detail: String,
    ok: bool,
    /// The page it came from; empty for a typed note. Drives the row's icon.
    url: String,
    /// The icon the page declares for itself, found by the server while it
    /// read the page. Empty until then, or when there was no page.
    icon: String,
    /// Its stored file name on the server, what `source_remove` takes. Empty
    /// for a row that is not on the server: one being read, or one that
    /// failed.
    file: String,
}

/// The row a source shows while its page is still being read.
const FETCHING: &str = "fetching…";

/// A date somebody reads, not a timestamp.
fn when(ms: i64) -> String {
    if ms <= 0 {
        // No timestamp in the sid — a hand-named session has one nowhere. An
        // empty string, not the words "no date": the row already reads
        // "ready · 3 slides · 1 voice", and a label announcing a missing field
        // is noise in a line whose whole job is to be skimmed.
        return String::new();
    }
    let now = js_sys::Date::now();
    let mins = ((now - ms as f64) / 60_000.0).floor().max(0.0) as i64;
    match mins {
        0 => "just now".into(),
        1 => "a minute ago".into(),
        2..=59 => format!("{mins} minutes ago"),
        60..=119 => "an hour ago".into(),
        120..=1439 => format!("{} hours ago", mins / 60),
        1440..=2879 => "yesterday".into(),
        2880..=10079 => format!("{} days ago", mins / 1440),
        _ => {
            let d = js_sys::Date::new(&wasm_bindgen::JsValue::from_f64(ms as f64));
            d.to_locale_date_string("en-GB", &wasm_bindgen::JsValue::UNDEFINED)
                .into()
        }
    }
}

/// One source on the sources panel, led by the icon of where it came from.
///
/// A link shows the icon its page declares (the server reads it off the page
/// while fetching it), loaded from the site directly rather than through a
/// third-party favicon service, which would learn every page a person reads.
/// While the page is still being read the row shows a spinner; if the page
/// declared no icon, `/favicon.ico` stands in; a site that has neither falls
/// back to its initial, and a typed note gets a note mark, so every row still
/// has an icon.
///
/// The remove button takes it out of the collection, or clears a row that
/// failed and never reached the server.
#[component]
fn SrcRow(s: Src, on_remove: EventHandler<Src>) -> Element {
    let reading = s.detail == FETCHING;
    let removable = !reading && (!s.file.is_empty() || !s.ok);
    // A row that never reached the server is only dismissed, like any other
    // error row; one that did is removed from the collection.
    let verb = if s.file.is_empty() {
        "Dismiss"
    } else {
        "Remove"
    };
    rsx! {
        div { class: if s.ok { "src" } else { "src bad" },
            SrcIcon { s: s.clone() }
            div { class: "src-t",
                div { class: "src-n", title: "{s.name}", "{s.name}" }
                div { class: "src-d", "{s.detail}" }
            }
            if removable {
                button {
                    class: "icon-btn src-x",
                    title: "{verb}",
                    aria_label: "{verb} {s.name}",
                    onclick: {
                        let s = s.clone();
                        move |_| on_remove.call(s.clone())
                    },
                    Icon { name: "x-lg" }
                }
            }
        }
    }
}

/// The leading icon of a source: a spinner while it is read, then its logo.
#[component]
fn SrcIcon(s: Src) -> Element {
    let mut broken = use_signal(|| false);
    let host = short_host(&s.url);
    if s.detail == FETCHING {
        rsx! { span { class: "src-i spin", title: "Reading the page…" } }
    } else if s.url.is_empty() {
        rsx! { span { class: "src-i note", Icon { name: "file-earmark-text" } } }
    } else if *broken.read() || !s.ok {
        let first = host
            .chars()
            .next()
            .unwrap_or('?')
            .to_uppercase()
            .to_string();
        rsx! { span { class: "src-i", "{first}" } }
    } else {
        let fav = if s.icon.is_empty() {
            format!("{}/favicon.ico", origin(&s.url))
        } else {
            s.icon.clone()
        };
        rsx! {
            img {
                class: "src-i",
                src: "{fav}",
                alt: "",
                onerror: move |_| broken.set(true),
            }
        }
    }
}

/// `scheme://host` of a link, the root a favicon is served from.
fn origin(url: &str) -> String {
    match url.split_once("://") {
        Some((scheme, rest)) => format!("{scheme}://{}", rest.split('/').next().unwrap_or(rest)),
        None => url.to_string(),
    }
}

fn short_host(url: &str) -> String {
    url.split("://")
        .nth(1)
        .unwrap_or(url)
        .split('/')
        .next()
        .unwrap_or(url)
        .trim_start_matches("www.")
        .to_string()
}

/// A source row from the server's `Fetched` shape.
fn src_from(g: &serde_json::Value) -> Src {
    let ok = g["ok"].as_bool().unwrap_or(false);
    let url = g["url"].as_str().unwrap_or_default().to_string();
    let name = g["title"].as_str().unwrap_or_default().to_string();
    let chars = g["chars"].as_u64().unwrap_or(0);
    Src {
        icon: g["icon"].as_str().unwrap_or_default().to_string(),
        name: if name.is_empty() { url.clone() } else { name },
        detail: if !ok {
            errors::readable(g["error"].as_str().unwrap_or("could not read it"))
        } else if url.is_empty() {
            format!("note · {} words", chars / 6)
        } else {
            format!("{} · {} words", short_host(&url), chars / 6)
        },
        ok,
        url,
        // The stored name, when the reply carries it; the list read back from
        // the server after an add fills it in either way.
        file: g["name"].as_str().unwrap_or_default().to_string(),
    }
}

/// The page's styles: the design tokens both pages share, then the app's own
/// rules, which use nothing but those tokens. See docs/design.md.
const CSS: &str = concat!(
    include_str!("../../opennotebook_sdk/assets/theme.css"),
    include_str!("style.css"),
);

#[cfg(test)]
mod source_kind_tests {
    use super::file_kind;

    /// A typed note or a report is a note; an uploaded file says its type.
    #[test]
    fn a_source_without_a_page_says_what_it_is() {
        assert_eq!(file_kind("my-note.md"), "note");
        assert_eq!(file_kind("Report.pdf"), "PDF");
        assert_eq!(file_kind("deck.pptx"), "PPTX");
        assert_eq!(file_kind("plain"), "note");
    }
}

#[cfg(test)]
mod look_tests {
    /// Every icon the app names is one the SDK vendors. A name that is not
    /// there draws nothing at all, an invisible button.
    #[test]
    fn every_icon_named_exists() {
        let src = [
            include_str!("main.rs"),
            include_str!("api.rs"),
            include_str!("chat.rs"),
            include_str!("collection.rs"),
            include_str!("dialogs.rs"),
            include_str!("home.rs"),
            include_str!("outputs.rs"),
            include_str!("routes.rs"),
            include_str!("settings.rs"),
            include_str!("mindmap.rs"),
            include_str!("notes.rs"),
            include_str!("pick.rs"),
        ]
        .concat();
        let mut named: Vec<String> = Vec::new();
        for part in src.split("Icon { name: \"").skip(1) {
            named.push(part.split('"').next().unwrap().to_string());
        }
        for k in super::Output::ALL {
            named.push(k.icon().to_string());
        }
        for t in [
            super::settings::ThemePref::Light,
            super::settings::ThemePref::Dark,
        ] {
            named.push(t.icon().to_string());
        }
        for tab in [
            "appearance",
            "defaults",
            "voices",
            "language",
            "conversation",
            "costs",
            "models",
        ] {
            named.push(super::settings::tab_glyph(tab).to_string());
        }
        let missing: Vec<&String> = named
            .iter()
            .filter(|n| opennotebook_sdk::icons::icon(n).is_none())
            .collect();
        assert!(named.len() > 30, "found only {} icon uses", named.len());
        assert!(missing.is_empty(), "not vendored: {missing:?}");
    }

    /// The index page's boot script is the SDK's, byte for byte, so the theme
    /// the page paints first is the one the app then keeps.
    #[test]
    fn the_index_page_applies_the_theme_before_it_paints() {
        let index = include_str!("../index.html");
        assert!(index.contains(opennotebook_sdk::theme::BOOT_SCRIPT));
        assert!(index.contains(r#"<div id="main"></div>"#));
    }

    /// The stylesheet uses the theme's tokens, not colours of its own: a raw
    /// colour would be right in one theme and wrong in the other. Pure black
    /// and white veils over media are the exception, and say so.
    #[test]
    fn the_stylesheet_has_no_colours_of_its_own() {
        let css = include_str!("style.css");
        let mut raw = Vec::new();
        for (i, line) in css.lines().enumerate() {
            let code = line.split("/*").next().unwrap_or("");
            for tok in code.split(|c: char| !(c.is_ascii_hexdigit() || c == '#')) {
                if tok.starts_with('#')
                    && (tok.len() == 4 || tok.len() == 7)
                    && tok != "#fff"
                    && tok != "#f3efe7"
                {
                    raw.push(format!("{}: {tok}", i + 1));
                }
            }
        }
        assert!(raw.is_empty(), "raw colours in style.css: {raw:?}");
    }

    /// The same for functional colours. A veil over media is the one place a
    /// fixed colour is right, because the media under it does not change with
    /// the theme; each one allowed is listed with what it is for.
    #[test]
    fn the_stylesheet_has_no_functional_colours_but_the_veils() {
        const ALLOWED: [&str; 7] = [
            // The ⋯ button and the pick box over a card's cover, at rest,
            // hovered and pressed; the third also veils a cover being redrawn.
            "rgba(10, 12, 16, .62)",
            "rgba(10, 12, 16, .82)",
            "rgba(10, 12, 16, .45)",
            "rgba(10, 12, 16, .7)",
            "rgba(255, 255, 255, .85)",
            "rgba(255, 255, 255, .75)",
            // A source's initial on its always-white icon tile.
            "rgba(0, 0, 0, .72)",
        ];
        let css = include_str!("style.css");
        let mut raw = Vec::new();
        for (i, line) in css.lines().enumerate() {
            let code = line.split("/*").next().unwrap_or("");
            for f in ["rgb(", "rgba(", "hsl(", "hsla("] {
                for (at, _) in code.match_indices(f) {
                    // Not the tail of a longer name, as in `var(--x-rgb(`.
                    if code[..at].ends_with(|c: char| c.is_ascii_alphanumeric() || c == '-') {
                        continue;
                    }
                    let end = code[at..].find(')').map_or(code.len(), |e| at + e + 1);
                    let colour = &code[at..end];
                    if !ALLOWED.contains(&colour) {
                        raw.push(format!("{}: {colour}", i + 1));
                    }
                }
            }
        }
        assert!(raw.is_empty(), "raw colours in style.css: {raw:?}");
    }
}
