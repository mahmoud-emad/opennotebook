//! Mind maps on the collection page: making one, and the map itself, drawn as
//! SVG in the viewer beside the Studio.
//!
//! Where the boxes go is `opennotebook_sdk::mindmap_layout`, tested natively;
//! this file only draws what it returns and handles the person's input. A click
//! on a label asks the chat NotebookLM's own question about that topic, through
//! `on_ask`. See `docs/mindmap-spec.md` section 6.

use dioxus::html::Key;
use dioxus::prelude::*;
use opennotebook_sdk::mindmap::{
    MindMap, MindMapCreateReq, MindMapRef, MindMapRetitleReq, MindMapSummary, MindmapCreateInput,
    MindmapDeleteInput, MindmapEstimateInput, MindmapEstimateOutput, MindmapGetInput,
    MindmapListInput, MindmapRetitleInput,
};
use opennotebook_sdk::mindmap_layout::{self as ml, Layout, NodePath};

use crate::api::mindmap_client as client;

/// Delete one map. `Err` when the service could not be reached or refused, so
/// a batch delete can say what it could not remove.
pub(crate) async fn delete(sid: String, id: String) -> Result<(), String> {
    client()?
        .mindmap_delete(MindmapDeleteInput {
            req: MindMapRef { sid, id },
        })
        .await
        .map_err(clean_err)
        .and_then(|o| crate::api::still_there(o.value))
}

/// Rename one map. `Err` says why it was not renamed, including a map that
/// is no longer there.
pub(crate) async fn retitle(sid: String, id: String, title: String) -> Result<(), String> {
    let done = client()?
        .mindmap_retitle(MindmapRetitleInput {
            req: MindMapRetitleReq { sid, id, title },
        })
        .await
        .map_err(clean_err)?;
    if done.value {
        Ok(())
    } else {
        Err("the mind map is no longer there".into())
    }
}

/// A create or get output as the map it is, field for field. The macro
/// flattens a returned struct into the output type; the server does the same
/// conversion the other way (`mindmap_impl::flat`).
fn as_map<T: serde::Serialize>(out: &T) -> Result<MindMap, String> {
    serde_json::to_value(out)
        .and_then(serde_json::from_value)
        .map_err(|e| e.to_string())
}

fn clean_err(e: impl std::fmt::Display) -> String {
    crate::api::clean_rpc_error(&e.to_string())
}

// ── making one ───────────────────────────────────────────────────────────────

/// The collection page's mind maps: the list, the options' focus, and a map
/// being made, shared by the Studio's options and its outputs list.
#[derive(Clone, Copy, PartialEq)]
pub struct MapState {
    pub maps: Signal<Vec<MindMapSummary>>,
    pub focus: Signal<String>,
    pub making: Signal<bool>,
    pub err: Signal<String>,
    pub est: Signal<Option<MindmapEstimateOutput>>,
    pub est_loading: Signal<bool>,
}

/// The state, as hooks of the calling component.
pub fn use_map_state() -> MapState {
    MapState {
        maps: use_signal(Vec::new),
        focus: use_signal(String::new),
        making: use_signal(|| false),
        err: use_signal(String::new),
        est: use_signal(|| None),
        est_loading: use_signal(|| false),
    }
}

/// The collection's maps, newest first.
pub async fn load(sid: String, mut st: MapState) {
    let got = async {
        client()?
            .mindmap_list(MindmapListInput { sid })
            .await
            .map_err(clean_err)
    }
    .await;
    match got {
        Ok(out) => st.maps.set(out.maps),
        Err(e) => crate::report(format!("The mind maps could not be loaded. {e}")),
    }
}

/// What a map of the collection would cost. Free: no model is called.
pub async fn estimate(sid: String, mut st: MapState) {
    st.est_loading.set(true);
    let got = async {
        client()?
            .mindmap_estimate(MindmapEstimateInput { sid })
            .await
            .map_err(clean_err)
    }
    .await;
    st.est.set(got.ok());
    st.est_loading.set(false);
}

/// Make a map of the collection with the focus typed in its options, and return its
/// id to open. `None` when it failed; the reason is in `st.err`.
pub async fn make(sid: String, mut st: MapState) -> Option<String> {
    if *st.making.peek() {
        return None;
    }
    st.making.set(true);
    st.err.set(String::new());
    let f = st.focus.peek().trim().to_string();
    let made = async {
        let c = client()?;
        let out = c
            .mindmap_create(MindmapCreateInput {
                req: MindMapCreateReq {
                    sid: sid.clone(),
                    focus: (!f.is_empty()).then_some(f),
                    sources: None,
                },
            })
            .await
            .map_err(clean_err)?;
        let map = as_map(&out)?;
        let list = c
            .mindmap_list(MindmapListInput { sid })
            .await
            .map_err(clean_err)?;
        Ok::<_, String>((map, list.maps))
    }
    .await;
    st.making.set(false);
    match made {
        Ok((map, list)) => {
            st.maps.set(list);
            st.focus.set(String::new());
            Some(map.id)
        }
        Err(e) => {
            st.err.set(e);
            None
        }
    }
}

/// The newest map made from exactly `sources`, if one is: the map that is
/// already up to date, so making another would draw the same tree again.
/// Order does not matter; a source added or removed since does.
pub fn covering_map(maps: &[MindMapSummary], sources: &[String]) -> Option<String> {
    if sources.is_empty() {
        return None;
    }
    let want: std::collections::BTreeSet<&str> = sources.iter().map(String::as_str).collect();
    maps.iter()
        .filter(|m| m.focus.trim().is_empty())
        .find(|m| {
            m.sources
                .iter()
                .map(String::as_str)
                .collect::<std::collections::BTreeSet<_>>()
                == want
        })
        .map(|m| m.id.clone())
}

// ── the map ──────────────────────────────────────────────────────────────────

/// The pan and zoom of the drawing: translate, then scale.
#[derive(Clone, Copy, PartialEq)]
struct View {
    tx: f64,
    ty: f64,
    k: f64,
}

const K_MIN: f64 = 0.1;
const K_MAX: f64 = 50.0;
/// Room left round a fitted map, in px.
const FIT_PAD: f64 = 24.0;

/// A label's width as the browser draws it, from a canvas set to the font the
/// map uses. Falls back to the SDK's estimate where there is no canvas.
fn measure(label: &str) -> f32 {
    thread_local! {
        static CTX: Option<web_sys::CanvasRenderingContext2d> = {
            use wasm_bindgen::JsCast;
            web_sys::window()
                .and_then(|w| w.document())
                .and_then(|d| d.create_element("canvas").ok())
                .and_then(|c| c.dyn_into::<web_sys::HtmlCanvasElement>().ok())
                .and_then(|c| c.get_context("2d").ok().flatten())
                .and_then(|c| c.dyn_into::<web_sys::CanvasRenderingContext2d>().ok())
                .inspect(|c| c.set_font(&format!("500 {}px system-ui, sans-serif", ml::FONT_PX)))
        };
    }
    CTX.with(|c| {
        c.as_ref()
            .and_then(|c| c.measure_text(label).ok())
            .map_or_else(|| ml::estimate_width(label), |m| m.width() as f32)
    })
}

/// The drawing pane's size and its position on screen.
fn pane() -> Option<(f64, f64, f64, f64)> {
    let el = web_sys::window()?
        .document()?
        .get_element_by_id("mm-canvas")?;
    let r = el.get_bounding_client_rect();
    Some((r.left(), r.top(), r.width(), r.height()))
}

/// The view that shows the box (x, y, w, h) whole and centred, no larger than
/// life-size times 1.25, so a small map is not blown up.
fn fit_to(b: (f32, f32, f32, f32)) -> Option<View> {
    let (_, _, pw, ph) = pane()?;
    let (x, y, w, h) = (b.0 as f64, b.1 as f64, b.2 as f64, b.3 as f64);
    let k = ((pw - 2.0 * FIT_PAD) / w.max(1.0))
        .min((ph - 2.0 * FIT_PAD) / h.max(1.0))
        .clamp(K_MIN, 1.25);
    Some(View {
        tx: (pw - w * k) / 2.0 - x * k,
        ty: (ph - h * k) / 2.0 - y * k,
        k,
    })
}

fn dom_id(path: &[usize]) -> String {
    let mut s = String::from("mm-n");
    for i in path {
        s.push('-');
        s.push_str(&i.to_string());
    }
    s
}

/// A map, drawn. `on_ask` gets the question a click asks.
#[component]
pub fn MindMapView(
    sid: String,
    id: String,
    /// Its name as the outputs list has it, which a rename changes while the
    /// map is open. Empty until the list has loaded.
    title: String,
    on_close: EventHandler<()>,
    on_ask: EventHandler<String>,
) -> Element {
    let mut map = use_signal(|| None::<MindMap>);
    let mut err = use_signal(String::new);
    let mut open = use_signal(ml::initial_open);
    let mut view = use_signal(|| View {
        tx: FIT_PAD,
        ty: FIT_PAD,
        k: 1.0,
    });
    let mut focus = use_signal(NodePath::new);
    // Pointer down: where, and the view then. Moved: it became a drag, so the
    // click that ends it is not a click on a node.
    let mut drag = use_signal(|| None::<(f64, f64, View)>);
    let mut moved = use_signal(|| false);
    let mut menu = use_signal(|| false);

    let layout = use_memo(move || match map.read().as_ref() {
        Some(m) => ml::layout(&m.root, &open.read(), &measure),
        None => Layout::default(),
    });

    // A rename while it is open shows here, and names its downloads.
    use_effect(use_reactive((&title,), move |(t,)| {
        let stale = map.peek().as_ref().is_some_and(|m| m.title != t);
        if stale
            && !t.is_empty()
            && let Some(m) = map.write().as_mut()
        {
            m.title = t;
        }
    }));

    // Load, then fit the whole map once the pane has drawn.
    use_effect(use_reactive!(|(sid, id)| {
        spawn(async move {
            map.set(None);
            err.set(String::new());
            open.set(ml::initial_open());
            focus.set(Vec::new());
            let got = async {
                let c = client()?;
                let out = c
                    .mindmap_get(MindmapGetInput {
                        req: MindMapRef { sid, id },
                    })
                    .await
                    .map_err(clean_err)?;
                as_map(&out)
            }
            .await;
            match got {
                Ok(m) => {
                    let l = ml::layout(&m.root, &ml::initial_open(), &measure);
                    map.set(Some(m));
                    // Fit once the pane has drawn and has a size.
                    crate::api::gloo_sleep(0).await;
                    if let Some(v) = fit_to((0.0, 0.0, l.width, l.height)) {
                        view.set(v);
                    }
                }
                Err(e) => err.set(e),
            }
        });
    }));

    // Open or close a branch.
    let mut toggle = move |path: NodePath| {
        let mut o = open.write();
        if !o.remove(&path) {
            o.insert(path);
        }
    };

    // Ask about a node: NotebookLM's question to the chat, the node opened if
    // it was closed, and the view moved to it and its children.
    let mut ask = move |path: NodePath| {
        let Some(root) = map.peek().as_ref().map(|m| m.root.clone()) else {
            return;
        };
        let Some(q) = ml::question_for(&root, &path) else {
            return;
        };
        focus.set(path.clone());
        let branch = ml::node_at(&root, &path).is_some_and(|n| !n.children.is_empty());
        if branch {
            open.write().insert(path.clone());
        }
        let l = ml::layout(&root, &open.peek(), &measure);
        if let Some(v) = l.bounds_of(&path).and_then(fit_to) {
            view.set(v);
        }
        on_ask.call(q);
    };

    let mut zoom = move |factor: f64, at: Option<(f64, f64)>| {
        let v = *view.peek();
        let k = (v.k * factor).clamp(K_MIN, K_MAX);
        let (cx, cy) = at
            .or_else(|| pane().map(|(_, _, w, h)| (w / 2.0, h / 2.0)))
            .unwrap_or((0.0, 0.0));
        // Keep the point under (cx, cy) where it is.
        view.set(View {
            tx: cx - (cx - v.tx) * k / v.k,
            ty: cy - (cy - v.ty) * k / v.k,
            k,
        });
    };

    let keydown = move |e: KeyboardEvent| {
        let l = layout.peek();
        let Some(root) = map.peek().as_ref().map(|m| m.root.clone()) else {
            return;
        };
        let cur = focus.peek().clone();
        let at = l.nodes.iter().position(|n| n.path == cur).unwrap_or(0);
        let node = l.nodes.get(at).cloned();
        let go = |i: usize, mut focus: Signal<NodePath>| {
            if let Some(n) = l.nodes.get(i) {
                focus.set(n.path.clone());
            }
        };
        match e.key() {
            Key::ArrowDown => go((at + 1).min(l.nodes.len().saturating_sub(1)), focus),
            Key::ArrowUp => go(at.saturating_sub(1), focus),
            Key::Home => go(0, focus),
            Key::End => go(l.nodes.len().saturating_sub(1), focus),
            Key::ArrowRight => match node {
                Some(n) if n.children > 0 && !n.open => {
                    drop(l);
                    open.write().insert(n.path);
                }
                Some(n) if n.children > 0 => {
                    let mut p = n.path;
                    p.push(0);
                    focus.set(p);
                }
                _ => {}
            },
            Key::ArrowLeft => match node {
                Some(n) if n.open => {
                    drop(l);
                    open.write().remove(&n.path);
                }
                Some(n) if !n.path.is_empty() => {
                    focus.set(n.path[..n.path.len() - 1].to_vec());
                }
                _ => {}
            },
            Key::Enter => {
                drop(l);
                ask(cur);
            }
            Key::Character(c) if c == " " => {
                drop(l);
                if ml::node_at(&root, &cur).is_some_and(|n| !n.children.is_empty()) {
                    toggle(cur);
                }
            }
            _ => return,
        }
        e.prevent_default();
    };

    // The list's name for it first: the row and the frame around the map then
    // always say the same, a rename included, before and after it loads.
    let title = if title.trim().is_empty() {
        map.read()
            .as_ref()
            .map(|m| m.title.clone())
            .unwrap_or_default()
    } else {
        title.clone()
    };
    let note = map.read().as_ref().map(|m| {
        let mut parts = vec![format!("{} topics", m.node_count)];
        if m.dropped > 0 {
            parts.push(format!("{} left out, not found in the sources", m.dropped));
        }
        if m.excerpted {
            parts.push("built from excerpts of long sources".to_string());
        }
        if !m.focus.is_empty() {
            parts.push(format!("focus: {}", m.focus));
        }
        parts.join(" · ")
    });
    let v = *view.read();
    let dragging = drag.read().is_some();
    let l = layout.read();
    let active = dom_id(&focus.read());

    rsx! {
        div { class: "mm-view",
            div { class: "mm-bar",
                div { class: "mm-title",
                    div { class: "mm-h", "{title}" }
                    if let Some(n) = note { div { class: "mm-note", "{n}" } }
                }
                div { class: "mm-tools", role: "toolbar", aria_label: "Mind map",
                    button { class: "icon-btn", title: "Expand all", aria_label: "Expand all",
                        onclick: move |_| { if let Some(m) = map.peek().as_ref() { open.set(ml::all_open(&m.root)); } },
                        crate::Icon { name: "arrows-expand" }
                    }
                    button { class: "icon-btn", title: "Collapse all", aria_label: "Collapse all",
                        onclick: move |_| open.set(ml::initial_open()),
                        crate::Icon { name: "arrows-collapse" }
                    }
                    button { class: "icon-btn", title: "Zoom in", aria_label: "Zoom in",
                        onclick: move |_| zoom(1.2, None), crate::Icon { name: "zoom-in" }
                    }
                    button { class: "icon-btn", title: "Zoom out", aria_label: "Zoom out",
                        onclick: move |_| zoom(0.8, None), crate::Icon { name: "zoom-out" }
                    }
                    button { class: "icon-btn", title: "Fit to the pane", aria_label: "Fit to the pane",
                        onclick: move |_| {
                            let l = layout.peek();
                            if let Some(v) = fit_to((0.0, 0.0, l.width, l.height)) { view.set(v); }
                        },
                        crate::Icon { name: "arrows-fullscreen" }
                    }
                    div { class: "mm-menu-wrap",
                        button { class: "icon-btn", title: "Download", aria_label: "Download",
                            aria_expanded: "{menu}",
                            onclick: move |_| { let m = *menu.read(); menu.set(!m); },
                            crate::Icon { name: "download" }
                        }
                        if *menu.read() {
                            div { class: "mm-menu", role: "menu",
                                button { role: "menuitem", onclick: move |_| { menu.set(false); export_png(&layout.peek(), &title_of(&map)); }, "Image (PNG)" }
                                button { role: "menuitem", onclick: move |_| {
                                    menu.set(false);
                                    if let Some(m) = map.peek().as_ref() { download(&format!("{}.md", file_stem(&m.title)), "text/markdown", &ml::to_markdown(&m.root)); }
                                }, "Outline (Markdown)" }
                                button { role: "menuitem", onclick: move |_| {
                                    menu.set(false);
                                    if let Some(m) = map.peek().as_ref() { download(&format!("{}.opml", file_stem(&m.title)), "text/x-opml", &ml::to_opml(&m.root)); }
                                }, "Outline (OPML)" }
                            }
                        }
                    }
                    button { class: "icon-btn", title: "Close the map", aria_label: "Close the map",
                        onclick: move |_| on_close.call(()), crate::Icon { name: "x-lg" }
                    }
                }
            }
            div { id: "mm-canvas", class: if dragging { "mm-canvas dragging" } else { "mm-canvas" },
                if !err.read().is_empty() {
                    div { class: "mm-msg bad", role: "alert", "{err}" }
                } else if map.read().is_none() {
                    div { class: "mm-msg", span { class: "mini-spin" } " Opening the map…" }
                }
                svg {
                    id: "mm-svg",
                    width: "100%",
                    height: "100%",
                    tabindex: "0",
                    role: "tree",
                    "aria-label": "Mind map: {title}. Arrow keys move, Enter asks about a topic, Space opens or closes it.",
                    "aria-activedescendant": "{active}",
                    onkeydown: keydown,
                    onpointerdown: move |e: PointerEvent| {
                        let p = e.client_coordinates();
                        drag.set(Some((p.x, p.y, *view.peek())));
                        moved.set(false);
                    },
                    onpointermove: move |e: PointerEvent| {
                        let Some((x0, y0, v0)) = *drag.peek() else { return };
                        let p = e.client_coordinates();
                        let (dx, dy) = (p.x - x0, p.y - y0);
                        if dx.abs() + dy.abs() > 4.0 { moved.set(true); }
                        if *moved.peek() {
                            view.set(View { tx: v0.tx + dx, ty: v0.ty + dy, k: v0.k });
                        }
                    },
                    onpointerup: move |_| drag.set(None),
                    onpointerleave: move |_| drag.set(None),
                    onwheel: move |e: WheelEvent| {
                        e.prevent_default();
                        let d = e.delta().strip_units().y;
                        let p = e.client_coordinates();
                        let at = pane().map(|(left, top, _, _)| (p.x - left, p.y - top));
                        zoom(if d < 0.0 { 1.1 } else { 1.0 / 1.1 }, at);
                    },
                    g {
                        class: "mm-root",
                        style: "transform: translate({v.tx}px, {v.ty}px) scale({v.k})",
                        for link in l.links.iter() {
                            path { key: "l{dom_id(&link.to)}", d: "{link.d()}", style: "fill:none;stroke:{ml::LINK_COLOUR};stroke-width:1.5" }
                        }
                        for n in l.nodes.iter().cloned() {
                            {
                                let (fill, ink) = ml::colours(n.depth);
                                let is_focus = *focus.read() == n.path;
                                let ty = n.cy();
                                let tx = n.x + ml::PAD_X;
                                let label = if n.children > 0 {
                                    format!("{}, {} subtopics", n.name, n.children)
                                } else {
                                    n.name.clone()
                                };
                                let p_ask = n.path.clone();
                                let p_tog = n.path.clone();
                                let cx = n.x + n.w - ml::TOGGLE_W / 2.0 - 4.0;
                                rsx! {
                                    g {
                                        key: "{dom_id(&n.path)}",
                                        id: "{dom_id(&n.path)}",
                                        role: "treeitem",
                                        "aria-level": "{n.depth + 1}",
                                        "aria-expanded": (n.children > 0).then_some(if n.open { "true" } else { "false" }),
                                        "aria-selected": if is_focus { "true" } else { "false" },
                                        "aria-label": "{label}",
                                        class: "mm-node",
                                        rect {
                                            x: "{n.x}", y: "{n.y}", width: "{n.w}", height: "{n.h}", rx: "8",
                                            style: if is_focus { format!("fill:{fill};stroke:var(--st-focus);stroke-width:2.5") } else { format!("fill:{fill};stroke:none") },
                                            onclick: move |_| { if !*moved.peek() { ask(p_ask.clone()); } },
                                        }
                                        text {
                                            x: "{tx}", y: "{ty}",
                                            dominant_baseline: "central",
                                            style: "fill:{ink}",
                                            font_size: "{ml::FONT_PX}",
                                            font_weight: "500",
                                            font_family: "system-ui, sans-serif",
                                            pointer_events: "none",
                                            "{n.name}"
                                        }
                                        if n.children > 0 {
                                            circle {
                                                cx: "{cx}", cy: "{ty}", r: "8",
                                                style: "fill:{ink};fill-opacity:.16",
                                                class: "mm-tog",
                                                onclick: move |_| { if !*moved.peek() { toggle(p_tog.clone()); } },
                                            }
                                            text {
                                                x: "{cx}", y: "{ty}",
                                                dominant_baseline: "central",
                                                text_anchor: "middle",
                                                style: "fill:{ink}",
                                                font_size: "12",
                                                font_family: "system-ui, sans-serif",
                                                pointer_events: "none",
                                                if n.open { "‹" } else { "›" }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

fn title_of(map: &Signal<Option<MindMap>>) -> String {
    map.peek()
        .as_ref()
        .map(|m| m.title.clone())
        .unwrap_or_default()
}

/// A title as a file name: letters, digits, spaces and dashes, at most 60.
pub(crate) fn file_stem(title: &str) -> String {
    let s: String = title
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == ' ' || c == '-' {
                c
            } else {
                ' '
            }
        })
        .collect();
    let s = s.split_whitespace().collect::<Vec<_>>().join(" ");
    let s: String = s.chars().take(60).collect();
    if s.is_empty() {
        "Mind map".to_string()
    } else {
        s
    }
}

/// Save text as a file, through a link the browser downloads.
pub(crate) fn download(name: &str, mime: &str, body: &str) {
    let js = format!(
        "const b=new Blob([{}],{{type:{}}});const a=document.createElement('a');\
         a.href=URL.createObjectURL(b);a.download={};document.body.appendChild(a);a.click();\
         setTimeout(()=>{{URL.revokeObjectURL(a.href);a.remove();}},1000);",
        serde_json::to_string(body).unwrap_or_default(),
        serde_json::to_string(mime).unwrap_or_default(),
        serde_json::to_string(name).unwrap_or_default(),
    );
    let _ = document::eval(&js);
}

/// The drawn map as a PNG at twice its size, on the page's background.
///
/// The live map takes its colours from the theme's CSS variables, which mean
/// nothing to an SVG drawn as an image. So every shape of the copy is given
/// the colours the browser actually computed for it, and the background is
/// the page's own: a PNG made in light mode is a light map. Only the pan and
/// zoom are taken off.
fn export_png(l: &Layout, title: &str) {
    let pad = 24.0;
    let (w, h) = (l.width as f64 + 2.0 * pad, l.height as f64 + 2.0 * pad);
    let js = format!(
        "const src=document.getElementById('mm-svg');if(!src)return;\
         const svg=src.cloneNode(true);\
         svg.setAttribute('xmlns','http://www.w3.org/2000/svg');\
         svg.setAttribute('width',{w});svg.setAttribute('height',{h});\
         svg.setAttribute('viewBox','0 0 {w} {h}');\
         const o=src.querySelectorAll('path,rect,text,circle'),k=svg.querySelectorAll('path,rect,text,circle');\
         o.forEach((e,i)=>{{const cs=getComputedStyle(e);k[i].setAttribute('style','fill:'+cs.fill+';fill-opacity:'+cs.fillOpacity+';stroke:'+cs.stroke+';stroke-width:'+cs.strokeWidth);}});\
         const g=svg.querySelector('.mm-root');g.removeAttribute('style');g.setAttribute('transform','translate({pad},{pad})');\
         const bg=getComputedStyle(document.body).backgroundColor;\
         const url='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(new XMLSerializer().serializeToString(svg));\
         const img=new Image();img.onload=()=>{{const c=document.createElement('canvas');c.width={w}*2;c.height={h}*2;\
         const x=c.getContext('2d');x.fillStyle=bg;x.fillRect(0,0,c.width,c.height);x.scale(2,2);x.drawImage(img,0,0);\
         c.toBlob(b=>{{const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download={name};\
         document.body.appendChild(a);a.click();setTimeout(()=>{{URL.revokeObjectURL(a.href);a.remove();}},1000);}},'image/png');}};\
         img.src=url;",
        name = serde_json::to_string(&format!("{}.png", file_stem(title))).unwrap_or_default(),
    );
    let _ = document::eval(&js);
}

// ── citations in the chat ────────────────────────────────────────────────────

/// One passage an answer cites, as the chat keeps it.
#[derive(Clone, PartialEq, Default, serde::Serialize, serde::Deserialize)]
pub struct Cite {
    pub n: u32,
    pub title: String,
    #[serde(default)]
    pub url: String,
    /// The source's file name in the collection: what a click opens.
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub excerpt: String,
}

impl Cite {
    pub fn from_json(v: &serde_json::Value) -> Option<Self> {
        Some(Cite {
            n: v["n"].as_u64()? as u32,
            title: v["title"].as_str().unwrap_or_default().to_string(),
            url: v["url"].as_str().unwrap_or_default().to_string(),
            name: v["name"].as_str().unwrap_or_default().to_string(),
            excerpt: v["excerpt"].as_str().unwrap_or_default().to_string(),
        })
    }
}

/// The sources an answer cites, each once, in order of first citation, with
/// the numbers of its passages: (title, url, "1, 3, 5"). Seven passages from
/// one paper are one source, not seven.
pub fn cite_groups(cites: &[Cite]) -> Vec<(String, String, String)> {
    let mut out: Vec<(String, String, Vec<u32>)> = Vec::new();
    for c in cites {
        match out
            .iter_mut()
            .find(|(t, u, _)| *t == c.title && *u == c.url)
        {
            Some((_, _, ns)) => ns.push(c.n),
            None => out.push((c.title.clone(), c.url.clone(), vec![c.n])),
        }
    }
    out.into_iter()
        .map(|(t, u, ns)| {
            let ns = ns.iter().map(u32::to_string).collect::<Vec<_>>().join(", ");
            (t, u, ns)
        })
        .collect()
}

fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

/// A passage as prose to read in a popover.
///
/// Passages are cut from the sources' Markdown as they are, so a research
/// report's came out as "Web research: https://… | Severity | Category |
/// Finding | |---|---|---| | info | other | …": its header, a bare link and a
/// table, run together. Read here line by line: a table row becomes its cells
/// joined by " · ", its rule line and any bare link go, and headings, list
/// marks, emphasis and inline links are reduced to their words.
pub fn plain_excerpt(md: &str) -> String {
    let mut lines: Vec<String> = Vec::new();
    for raw in md.lines() {
        let mut l = raw.trim();
        if l.is_empty() {
            continue;
        }
        // A table's rule: only pipes, dashes, colons and spaces.
        if l.contains('-') && l.chars().all(|c| matches!(c, '|' | '-' | ':' | ' ')) {
            continue;
        }
        let row;
        if l.starts_with('|') {
            row = l
                .split('|')
                .map(str::trim)
                .filter(|c| !c.is_empty())
                .collect::<Vec<_>>()
                .join(" · ");
            l = &row;
        }
        let l = l.trim_start_matches(['#', '>']).trim_start();
        let l = l
            .strip_prefix("- ")
            .or_else(|| l.strip_prefix("* "))
            .or_else(|| l.strip_prefix("+ "))
            .unwrap_or(l);
        let words: Vec<String> = l
            .split_whitespace()
            .filter(|w| !is_link(w))
            .map(|w| w.to_string())
            .collect();
        let text = inline_links(&words.join(" "))
            .replace("**", "")
            .replace("__", "")
            .replace('`', "");
        let text = text.trim();
        // What is left of "Web research: https://…" says nothing.
        if text.is_empty() || (text.ends_with(':') && text.split_whitespace().count() <= 3) {
            continue;
        }
        lines.push(text.to_string());
    }
    lines.join(" ")
}

/// A bare web address, maybe in brackets or ending a sentence.
fn is_link(w: &str) -> bool {
    let w = w.trim_start_matches(['(', '<', '[']);
    w.starts_with("http://") || w.starts_with("https://") || w.starts_with("www.")
}

/// `[words](url)` as its words.
fn inline_links(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find("](") {
        let Some(open) = rest[..i].rfind('[') else {
            break;
        };
        let Some(close) = rest[i + 2..].find(')') else {
            break;
        };
        out.push_str(&rest[..open]);
        out.push_str(&rest[open + 1..i]);
        rest = &rest[i + 2 + close + 1..];
    }
    out.push_str(rest);
    out
}

/// An answer's HTML with each `[n]` turned into a numbered chip. Hovering or
/// focusing a chip shows the source's title and the passage; clicking it, or
/// Enter on it, opens the source at that passage (`source::install_cite_open`).
/// A chip saved before chips knew their source links to the page instead.
///
/// One pass over the text: a chip carries its passage, and a passage can hold
/// a "[2]" of its own, which replacing one number after another would turn
/// into a chip inside a chip.
pub fn with_chips(html: &str, cites: &[Cite]) -> String {
    let chip = |c: &Cite| {
        let short = plain_excerpt(&c.excerpt);
        let short = if short.chars().count() > 320 {
            format!(
                "{}…",
                short.chars().take(320).collect::<String>().trim_end()
            )
        } else {
            short
        };
        let (num, open) = if !c.name.is_empty() {
            (
                c.n.to_string(),
                format!(
                    " role=\"button\" data-src=\"{}\" data-x=\"{}\"",
                    esc(&c.name),
                    esc(&c.excerpt)
                ),
            )
        } else if !c.url.is_empty() {
            (
                format!(
                    "<a href=\"{}\" target=\"_blank\" rel=\"noopener noreferrer\">{}</a>",
                    esc(&c.url),
                    c.n
                ),
                String::new(),
            )
        } else {
            (c.n.to_string(), String::new())
        };
        format!(
            "<span class=\"cite\" tabindex=\"0\"{open} aria-label=\"Source {n}: {t}\">{num}<span class=\"cite-pop\" role=\"tooltip\"><b>{t}</b><span>{x}</span></span></span>",
            n = c.n,
            t = esc(&c.title),
            x = esc(&short),
        )
    };
    let mut out = String::with_capacity(html.len());
    let mut rest = html;
    while let Some(i) = rest.find('[') {
        out.push_str(&rest[..i]);
        let after = &rest[i + 1..];
        let digits = after.chars().take_while(char::is_ascii_digit).count();
        let cite = (digits > 0 && after[digits..].starts_with(']'))
            .then(|| after[..digits].parse::<u32>().ok())
            .flatten()
            .and_then(|n| cites.iter().find(|c| c.n == n));
        match cite {
            Some(c) => {
                out.push_str(&chip(c));
                rest = &after[digits + 1..];
            }
            None => {
                out.push('[');
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_markdown_passage_reads_as_prose() {
        let md = "Web research: https://en.wikipedia.org/wiki/Construction_of_the_Egyptian_pyramids\n\
                  | Severity | Category | Finding |\n|---|---|---|\n\
                  | info | other | The counterweight theory lacks confirmation. |\n\
                  - **info / other** — See [the page](https://x.org/a) for more.";
        assert_eq!(
            plain_excerpt(md),
            "Severity · Category · Finding info · other · The counterweight theory lacks \
             confirmation. info / other — See the page for more."
        );
        assert_eq!(
            plain_excerpt("## Heading\nplain   text"),
            "Heading plain text"
        );
    }

    #[test]
    fn chips_replace_their_markers_and_escape_the_source() {
        let cites = vec![
            Cite {
                n: 1,
                title: "A <b>".into(),
                url: "https://x.org/?a=1&b=2".into(),
                excerpt: "one   two".into(),
                ..Default::default()
            },
            Cite {
                n: 2,
                title: "Note".into(),
                url: String::new(),
                excerpt: "e".into(),
                ..Default::default()
            },
        ];
        let h = with_chips("<p>X [1]. Y [2][1]. Not [10].</p>", &cites);
        assert_eq!(h.matches("class=\"cite\"").count(), 3);
        assert!(h.contains("Not [10]."));
        assert!(h.contains("A &lt;b&gt;"));
        assert!(h.contains("href=\"https://x.org/?a=1&amp;b=2\""));
        assert!(h.contains("<span>one two</span>"));
        // A note has no link to follow.
        assert!(h.contains(">2<span class=\"cite-pop\""));
    }

    #[test]
    fn a_chip_of_a_source_opens_it_and_a_passage_never_grows_chips() {
        let cites = vec![
            Cite {
                n: 1,
                title: "Report".into(),
                name: "research_1.md".into(),
                excerpt: "As [2] says, \"ramps\".".into(),
                ..Default::default()
            },
            Cite {
                n: 2,
                title: "Wiki".into(),
                name: "wiki.md".into(),
                url: "https://w.org".into(),
                excerpt: "e".into(),
            },
        ];
        let h = with_chips("<p>A [1] B [2]</p>", &cites);
        assert_eq!(h.matches("class=\"cite\"").count(), 2, "{h}");
        assert!(h.contains("data-src=\"research_1.md\""), "{h}");
        assert!(
            h.contains("data-x=\"As [2] says, &quot;ramps&quot;.\""),
            "{h}"
        );
        // A source opens in the viewer, which links the page itself.
        assert!(!h.contains("href="), "{h}");
    }

    #[test]
    fn one_source_cited_many_times_is_listed_once() {
        let c = |n: u32, t: &str| Cite {
            n,
            title: t.into(),
            url: String::new(),
            excerpt: String::new(),
            ..Default::default()
        };
        let g = cite_groups(&[c(1, "Moshi"), c(2, "Mimi"), c(3, "Moshi"), c(4, "Moshi")]);
        assert_eq!(
            g,
            vec![
                ("Moshi".into(), String::new(), "1, 3, 4".into()),
                ("Mimi".into(), String::new(), "2".into())
            ]
        );
    }

    #[test]
    fn a_map_is_up_to_date_only_for_exactly_its_sources() {
        let map = |id: &str, focus: &str, srcs: &[&str]| MindMapSummary {
            id: id.into(),
            focus: focus.into(),
            sources: srcs.iter().map(|s| s.to_string()).collect(),
            ..Default::default()
        };
        let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        let maps = vec![map("new", "", &["a.md", "b.md"]), map("old", "", &["a.md"])];
        // Same sources, any order: the newest such map.
        assert_eq!(
            covering_map(&maps, &s(&["b.md", "a.md"])),
            Some("new".into())
        );
        assert_eq!(covering_map(&maps, &s(&["a.md"])), Some("old".into()));
        // A source added since, or none at all: nothing is up to date.
        assert_eq!(covering_map(&maps, &s(&["a.md", "b.md", "c.md"])), None);
        assert_eq!(covering_map(&maps, &[]), None);
        // A focused map covers its focus, not the sources as a whole.
        assert_eq!(
            covering_map(&[map("f", "latency", &["a.md"])], &s(&["a.md"])),
            None
        );
    }

    #[test]
    fn a_title_becomes_a_safe_file_name() {
        assert_eq!(
            file_stem("[2410.00037] Moshi: a model"),
            "2410 00037 Moshi a model"
        );
        assert_eq!(file_stem("///"), "Mind map");
    }
}
