//! Home and All collections: the collections as cards or rows, and what can be
//! done to one from there (open, rename, pin, delete).

use dioxus::prelude::*;
use opennotebook_sdk::session::{
    CollectionCoverRefreshInput, CollectionCreateInput, CollectionCreateReq, CollectionDeleteInput,
    CollectionPinInput, CollectionPinReq, CollectionRetitleInput, CollectionRetitleReq,
    CollectionSummary,
};

use crate::api::{api_base, clean_rpc_error, client, still_there};
use crate::chat::chat_forget;
use crate::dialogs::{Ask, ask};
use crate::pick::{self, Pick, PickBox};
use crate::routes::{View, route_url};
use crate::settings::{THEME, ThemePref};
use crate::{CardMenu, Icon, Output, report, when};

/// The All collections page's order.
#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Sort {
    Recent,
    Title,
}

impl Sort {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Sort::Recent => "Most recent",
            Sort::Title => "Title",
        }
    }
}

/// Collections as a page lists them: what the person pinned first, then the
/// rest in the chosen order. The server's list is most recently updated first,
/// so Recent keeps its order.
///
/// One function for the page and for the selection's order, so a shift-click's
/// range is the range the person sees.
pub(crate) fn ordered(list: &[CollectionSummary], sort: Sort) -> Vec<CollectionSummary> {
    let mut v = list.to_vec();
    match sort {
        Sort::Recent => v.sort_by_key(|c| std::cmp::Reverse(c.updated_ms)),
        Sort::Title => v.sort_by_key(|c| {
            // Untitled ones last, not first: an empty string sorts before "A".
            (c.title.trim().is_empty(), c.title.to_lowercase())
        }),
    }
    let (mut pinned, rest): (Vec<_>, Vec<_>) = v.into_iter().partition(|c| c.pinned);
    pinned.extend(rest);
    pinned
}

/// How many collections home shows before "View all".
pub(crate) const HOME_N: usize = 3;

/// What a collection is called, with the stand-in for one not named yet.
pub(crate) fn coll_title(title: &str) -> String {
    if title.trim().is_empty() {
        "Untitled collection".to_string()
    } else {
        title.to_string()
    }
}

/// Rename a collection. Empty hands the naming back to the studio.
pub(crate) async fn collection_retitle(cid: String, title: String) -> Result<(), String> {
    client()?
        .collection_retitle(CollectionRetitleInput {
            req: CollectionRetitleReq { cid, title },
        })
        .await
        .map_err(|e| clean_rpc_error(&e.to_string()))
        .and_then(|o| still_there(o.value))
}

/// Start an empty collection and return its id.
pub(crate) async fn create_collection() -> Result<String, String> {
    let c = client()?;
    let made = c
        .collection_create(CollectionCreateInput {
            req: CollectionCreateReq { title: None },
        })
        .await
        .map_err(|e| clean_rpc_error(&e.to_string()))?;
    Ok(made.cid)
}

/// Delete a collection with everything in it, and its conversation in this
/// browser. `Err` says why it could not be.
pub(crate) async fn collection_delete(cid: &str) -> Result<(), String> {
    let done = client()?
        .collection_delete(CollectionDeleteInput {
            cid: cid.to_string(),
        })
        .await
        .map_err(|e| clean_rpc_error(&e.to_string()))?;
    still_there(done.value)?;
    chat_forget(cid);
    Ok(())
}

/// Cards shaped like what is coming, while the list is asked for, so nothing
/// jumps when the answer lands.
#[component]
pub(crate) fn SkelGrid(n: usize) -> Element {
    rsx! {
        div { class: "grid", aria_busy: "true",
            for i in 0..n {
                div { key: "{i}", class: "skel",
                    div { class: "skel-cover" }
                    div { class: "skel-line" }
                    div { class: "skel-line short" }
                }
            }
        }
    }
}

/// A list that could not be loaded: what failed, and another go.
#[component]
pub(crate) fn ListError(err: String, on_retry: EventHandler<()>) -> Element {
    rsx! {
        div { class: "empty", role: "alert",
            div { class: "empty-mark bad", Icon { name: "exclamation-triangle-fill", class: "xl" } }
            div { class: "empty-t", "Your collections could not be loaded" }
            div { class: "empty-d", "{err}. Check that the studio is running, then try again." }
            button { onclick: move |_| on_retry.call(()), Icon { name: "arrow-clockwise" } "Try again" }
        }
    }
}

/// "1 source", "5 sources".
pub(crate) fn n_of(n: i64, one: &str, many: &str) -> String {
    if n == 1 {
        format!("1 {one}")
    } else {
        format!("{n} {many}")
    }
}

/// The width of the canvas a cover is drawn on; it is 16:9, so 900 high.
const COVER_W: f64 = 1600.0;

/// Where a collection's cover is drawn, in the viewer's theme. The version and
/// the theme are part of the address, so the browser keeps a cover until
/// either changes and fetches it again the moment one does.
pub(crate) fn cover_url(cid: &str, version: &str, theme: ThemePref) -> String {
    format!(
        "{}/cover?collection={}&v={}&theme={}",
        api_base(),
        String::from(js_sys::encode_uri_component(cid)),
        String::from(js_sys::encode_uri_component(version)),
        theme.wire(),
    )
}

/// Whether the studio is still designing a collection's cover, so a list or a
/// page showing it should look again on its next poll.
///
/// The fallback stands in until the designed one arrives; its version says so
/// with an `f-` prefix. Only for a collection that changed a few minutes ago at
/// most: an old one may keep its fallback (covers were off, or the model could
/// not design one), and that is no reason to poll forever. Legacy collections
/// get theirs on a later visit.
pub(crate) fn cover_pending(c: &CollectionSummary, covers_on: bool) -> bool {
    let now_ms = js_sys::Date::now() as i64;
    covers_on
        && c.sources > 0
        && c.cover_version.starts_with("f-")
        && now_ms.saturating_sub(c.updated_ms) < 3 * 60_000
}

/// A collection's cover: its generated page, scaled to fit the box it sits in.
///
/// A picture, not a control. It is lazy, so a page of thirty cards loads only
/// what is on screen; sandboxed with nothing allowed, since the page is static;
/// hidden from assistive technology and out of the Tab order, because the card
/// or header around it already names the collection; and it takes no pointer
/// events, so a click lands on the card.
///
/// A cover is drawn with the app's own surfaces and type, so it follows the
/// theme: a change of theme is a new address, and the new cover paints in.
#[component]
pub(crate) fn Cover(
    cid: String,
    version: String,
    /// Where the cover sits, for its size: `card`, `row` or `head`.
    size: &'static str,
    /// While a new cover is being designed.
    #[props(default)]
    busy: bool,
) -> Element {
    // How far the 1600-wide canvas is scaled to fill this box. Measured rather
    // than fixed: a card is as wide as its grid column. The first guess is the
    // card's width on a common screen, so the first frame is close.
    let mut fit = use_signal(|| match size {
        "row" => 72.0 / COVER_W,
        "head" => 112.0 / COVER_W,
        _ => 300.0 / COVER_W,
    });
    // Which address last painted. A new version is a new page, so the box
    // shimmers again until that one paints.
    let mut painted = use_signal(String::new);
    let src = cover_url(&cid, &version, *THEME.read());
    let done = *painted.read() == src;
    let loaded = src.clone();
    rsx! {
        div {
            class: format!("cover cover-{size}{}", if done { " painted" } else { "" }),
            style: "--fit: {fit}",
            onresize: move |e: Event<ResizeData>| {
                if let Ok(size) = e.get_border_box_size() {
                    fit.set((size.width / COVER_W).max(0.01));
                }
            },
            iframe {
                src: "{src}",
                scrolling: "no",
                "loading": "lazy",
                "sandbox": "",
                tabindex: "-1",
                aria_hidden: "true",
                onload: move |_| painted.set(loaded.clone()),
            }
            if busy {
                span { class: "cover-busy", span { class: "mini-spin" } }
            }
        }
    }
}

/// What a collection holds, one badge per kind it has: the kind's icon and a
/// count, then whatever is still preparing or failed, which is worth noticing
/// in a way ready work is not.
#[component]
pub(crate) fn OutputBadges(c: CollectionSummary) -> Element {
    let kinds = [
        (
            Output::Session,
            c.decks,
            "narrated slides",
            "narrated slides",
        ),
        (Output::Audio, c.audios, "audio overview", "audio overviews"),
        (Output::MindMap, c.maps, "mind map", "mind maps"),
        (
            Output::Notes,
            c.notes,
            "set of study notes",
            "sets of study notes",
        ),
    ];
    rsx! {
        span { class: "outs",
            for (k, n, one, many) in kinds {
                if n > 0 {
                    span { key: "{k.wire()}", class: "out-b", title: "{n_of(n, one, many)}", aria_label: "{n_of(n, one, many)}",
                        Icon { name: k.icon() }
                        "{n}"
                    }
                }
            }
            if c.preparing > 0 {
                span { class: "badge preparing", "Preparing" }
            }
            if c.failed > 0 {
                span { class: "badge failed", "Failed" }
            }
        }
    }
}

/// One collection on home and on All collections: its cover, its name, how many
/// sources and when it last changed, and what it holds. The whole card opens
/// it; its ⋯ menu renames, pins, redraws the cover and deletes it.
#[component]
pub(crate) fn CollectionCard(
    c: CollectionSummary,
    list: bool,
    /// Whether it can be picked to delete with others (All collections).
    #[props(default)]
    pickable: bool,
    on_open: EventHandler<String>,
    on_changed: EventHandler<()>,
) -> Element {
    // While the ⋯ menu's Regenerate cover is at work.
    let mut redrawing = use_signal(|| false);
    let busy = *redrawing.read();
    let pick: Pick = c.cid.clone();
    let (mut picks, order) = pick::ctx();
    // Selecting, a click on the card ticks it rather than opening it.
    let selecting = pickable && picks.read().on;
    let picked = pickable && picks.read().has(&pick);
    let title = coll_title(&c.title);
    let untitled = c.title.trim().is_empty();
    let href = route_url(&View::Collection {
        cid: c.cid.clone(),
        open: None,
    });
    let click = {
        let (pick, cid) = (pick.clone(), c.cid.clone());
        move |e: Event<MouseData>| {
            // A modified click is the browser's: a new tab or window.
            let m = e.modifiers();
            if !selecting && (m.ctrl() || m.meta() || m.shift()) {
                return;
            }
            e.prevent_default();
            if selecting {
                picks.write().click(pick.clone(), m.shift(), &order.peek());
            } else {
                on_open.call(cid.clone());
            }
        }
    };
    let pick_box = pickable.then(|| rsx! { PickBox { pick: pick.clone(), label: title.clone() } });
    let sub = format!(
        "{} · {}",
        n_of(c.sources, "source", "sources"),
        when(c.updated_ms)
    );
    let sub = sub.trim_end_matches(" · ").to_string();

    let menu = {
        let (cid, current, pinned) = (c.cid.clone(), c.title.clone(), c.pinned);
        let shown = title.clone();
        rsx! {
            CardMenu {
                label: shown.clone(),
                items: vec![
                    ("Rename".to_string(), false),
                    (if pinned { "Unpin" } else { "Pin to top" }.to_string(), false),
                    (if busy { "Designing cover\u{2026}" } else { "Regenerate cover" }.to_string(), false),
                    ("Delete".to_string(), true),
                ],
                off: if busy { vec![2] } else { vec![] },
                on_pick: move |i: usize| {
                    let cid = cid.clone();
                    match i {
                        0 => {
                            let was = current.clone();
                            ask(Ask::prompt("Rename collection", &current, "Save", move |next: String| {
                                let next = next.trim().to_string();
                                if next == was { return }
                                let cid = cid.clone();
                                spawn(async move {
                                    if let Err(e) = collection_retitle(cid, next).await {
                                        report(format!("The collection could not be renamed: {e}"));
                                    }
                                    on_changed.call(());
                                });
                            }));
                        }
                        1 => {
                            spawn(async move {
                                let done = async {
                                    client()?
                                        .collection_pin(CollectionPinInput {
                                            req: CollectionPinReq { cid, pinned: !pinned },
                                        })
                                        .await
                                        .map_err(|e| clean_rpc_error(&e.to_string()))
                                        .and_then(|o| still_there(o.value))
                                }
                                .await;
                                if let Err(e) = done {
                                    let what = if pinned { "unpinned" } else { "pinned" };
                                    report(format!("The collection could not be {what}: {e}"));
                                }
                                on_changed.call(());
                            });
                        }
                        2 => {
                            if *redrawing.peek() { return }
                            redrawing.set(true);
                            spawn(async move {
                                let done = async {
                                    client()?
                                        .collection_cover_refresh(CollectionCoverRefreshInput { cid })
                                        .await
                                        .map_err(|e| clean_rpc_error(&e.to_string()))
                                        .and_then(|o| still_there(o.value))
                                }
                                .await;
                                if let Err(e) = done {
                                    report(format!("A new cover could not be designed: {e}"));
                                }
                                redrawing.set(false);
                                // The new cover has a new version; the list's
                                // next read brings it.
                                on_changed.call(());
                            });
                        }
                        _ => {
                            ask(Ask::confirm(
                                &format!("Delete \u{201c}{shown}\u{201d} and everything made from it?"),
                                "Its sources, slides, audio, mind maps and notes are removed. This cannot be undone.",
                                "Delete",
                                move |_| {
                                    let cid = cid.clone();
                                    spawn(async move {
                                        if let Err(e) = collection_delete(&cid).await {
                                            report(format!("The collection could not be deleted: {e}"));
                                        }
                                        on_changed.call(());
                                    });
                                },
                            ));
                        }
                    }
                },
            }
        }
    };

    let size = if list { "row" } else { "card" };
    let cover = rsx! {
        Cover {
            cid: c.cid.clone(),
            version: c.cover_version.clone(),
            size,
            busy,
        }
    };

    if list {
        return rsx! {
            div { class: if picked { "row-wrap picked" } else { "row-wrap" },
                {pick_box}
                a { class: "row", href: "{href}", onclick: click, aria_busy: busy,
                    {cover}
                    div { class: "row-main",
                        div { class: if untitled { "t untitled" } else { "t" },
                            if c.pinned { span { class: "pin-i", title: "Pinned", Icon { name: "pin-angle-fill" } } }
                            "{title}"
                        }
                        div { class: "row-d", "{sub}" }
                    }
                    div { class: "row-facts", OutputBadges { c: c.clone() } }
                }
                {menu}
            }
        };
    }

    rsx! {
        div { class: if picked { "card-wrap picked" } else { "card-wrap" },
            {pick_box}
            a { class: "card", href: "{href}", onclick: click, aria_busy: busy,
                {cover}
                div { class: "meta",
                    div { class: if untitled { "t untitled" } else { "t" },
                        if c.pinned { span { class: "pin-i", title: "Pinned", Icon { name: "pin-angle-fill" } } }
                        "{title}"
                    }
                    div { class: "sub", span { "{sub}" } }
                    OutputBadges { c: c.clone() }
                }
            }
            {menu}
        }
    }
}

/// The last card on home: the same shape as a collection's, on a dashed edge
/// because there is nothing in it yet. Starts a collection at once, no form.
#[component]
pub(crate) fn NewCard(busy: bool, on_new: EventHandler<()>) -> Element {
    rsx! {
        button {
            class: "card new-card",
            disabled: busy,
            onclick: move |_| on_new.call(()),
            span { class: "newc-cover",
                span { class: "dc-mark", Icon { name: "plus-lg" } }
                span { class: "newc-l", if busy { "Starting…" } else { "New collection" } }
            }
        }
    }
}
