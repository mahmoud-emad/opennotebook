//! Picking several collections on the All collections page and deleting them
//! at once.
//!
//! The pattern is the one people already know from Google Photos, Drive and
//! Gmail, not a new one:
//!
//! - A checkbox on every card, shown when the card is pointed at, so a gallery
//!   nobody is selecting in stays clean. Ticking one starts selecting.
//! - A "Select" button in the top bar starts it too. It is the way in on a
//!   touch screen, where nothing is pointed at and no checkbox shows.
//! - While selecting, every checkbox shows, a click anywhere on a card ticks it
//!   instead of opening it, and the card's own controls step aside.
//! - Shift-click ticks everything between the last tick and this one, in the
//!   order the page shows them. Ctrl/Cmd-A ticks all, Delete asks to delete,
//!   Esc stops.
//! - What can be done to the selection floats at the foot of the page (Linear,
//!   Notion, Airtable), not in the top bar: the top bar's grid/list switch and
//!   sort keep working mid-selection, and the bar never scrolls away.
//! - Deleting asks once, naming how many and what they lose.
//!   What could not be deleted stays selected, with a line saying so.
//!
//! The selection is only ever of what is on screen, so a delete can never reach
//! something the person cannot see.

use dioxus::prelude::*;

/// One thing that can be picked: a collection, by its cid. Collections are
/// the only thing the All collections page lists, so the only thing picked.
pub type Pick = String;

/// The selection, and whether the page is selecting at all.
#[derive(Clone, Default, PartialEq, Debug)]
pub struct Picks {
    /// Selecting: every checkbox shows and a click on a card ticks it.
    pub on: bool,
    /// Started with the Select button, so it stays on with nothing ticked.
    /// Started from a card's checkbox, it ends when the last tick goes.
    sticky: bool,
    /// What is ticked, in the order it was ticked.
    pub set: Vec<Pick>,
    /// The last thing clicked, where a shift-click's range starts.
    anchor: Option<Pick>,
    /// A delete is running.
    pub busy: bool,
    /// What the last delete could not do, until the next click.
    pub note: String,
}

impl Picks {
    pub fn has(&self, p: &Pick) -> bool {
        self.set.contains(p)
    }

    /// The Select button.
    pub fn start(&mut self) {
        self.on = true;
        self.sticky = true;
        self.note.clear();
    }

    pub fn stop(&mut self) {
        *self = Picks::default();
    }

    /// A tick, or with shift everything from the last tick to this one.
    pub fn click(&mut self, p: Pick, shift: bool, order: &[Pick]) {
        self.note.clear();
        let range = if shift {
            let a = self
                .anchor
                .as_ref()
                .and_then(|a| order.iter().position(|q| q == a));
            let b = order.iter().position(|q| *q == p);
            a.zip(b).map(|(a, b)| (a.min(b), a.max(b)))
        } else {
            None
        };
        match range {
            Some((a, b)) => {
                for q in &order[a..=b] {
                    if !self.has(q) {
                        self.set.push(q.clone());
                    }
                }
            }
            None if self.has(&p) => self.set.retain(|q| *q != p),
            None => self.set.push(p.clone()),
        }
        self.anchor = Some(p);
        self.on = true;
        self.settle();
    }

    /// Everything on screen, or nothing when everything already is.
    pub fn toggle_all(&mut self, order: &[Pick]) {
        self.note.clear();
        if !order.is_empty() && order.iter().all(|p| self.has(p)) {
            self.set.clear();
            self.anchor = None;
            // Emptied on purpose from the bar: still selecting.
            self.sticky = true;
        } else {
            self.set = order.to_vec();
            self.on = true;
        }
    }

    /// Drop what is no longer on screen: filtered away, or deleted.
    pub fn keep(&mut self, order: &[Pick]) {
        self.set.retain(|p| order.contains(p));
        if self.anchor.as_ref().is_some_and(|a| !order.contains(a)) {
            self.anchor = None;
        }
        self.settle();
    }

    /// Selecting that began with a checkbox ends with the last tick.
    fn settle(&mut self) {
        if self.on && self.set.is_empty() && !self.sticky && !self.busy {
            self.stop();
        }
    }
}

/// "1 collection", "3 collections".
fn summary(set: &[Pick]) -> String {
    match set.len() {
        1 => "1 collection".to_string(),
        n => format!("{n} collections"),
    }
}

/// What deleting this selection costs, in the words the single delete uses.
const CONSEQUENCES: &str =
    "Their sources and everything made from them are removed. This cannot be undone.";

/// Delete each, and return the ones that could not be.
async fn delete_all(set: Vec<Pick>) -> Vec<Pick> {
    let mut failed = Vec::new();
    for cid in set {
        if crate::home::collection_delete(&cid).await.is_err() {
            failed.push(cid);
        }
    }
    failed
}

/// Ask, then delete what is ticked.
fn confirm_delete(mut picks: Signal<Picks>, on_deleted: EventHandler<()>) {
    let set = picks.peek().set.clone();
    if set.is_empty() || picks.peek().busy {
        return;
    }
    let title = format!("Delete {}?", summary(&set));
    let body = format!("{}. {CONSEQUENCES}", summary(&set));
    crate::dialogs::ask(crate::dialogs::Ask::confirm(
        &title,
        &body,
        "Delete",
        move |_| {
            let set = set.clone();
            picks.write().busy = true;
            spawn(async move {
                let failed = delete_all(set).await;
                {
                    let mut w = picks.write();
                    w.busy = false;
                    if failed.is_empty() {
                        w.stop();
                    } else {
                        let k = failed.len();
                        w.set = failed;
                        w.anchor = None;
                        w.sticky = true;
                        w.note = if k == 1 {
                            "1 could not be deleted".into()
                        } else {
                            format!("{k} could not be deleted")
                        };
                    }
                }
                on_deleted.call(());
            });
        },
    ));
}

/// A card's view of the selection: whether the page is selecting, whether this
/// card is ticked, and the shared state to tick it with.
pub fn ctx() -> (Signal<Picks>, Memo<Vec<Pick>>) {
    (use_context(), use_context())
}

/// The checkbox on a card. A sibling of the card's link or button, never
/// inside it, for the same reason the card menu is: a control inside a link is
/// invalid, and the link would take the click.
#[component]
pub fn PickBox(pick: Pick, label: String) -> Element {
    let (mut picks, order) = ctx();
    let on = picks.read().has(&pick);
    rsx! {
        button {
            class: if on { "pick on" } else { "pick" },
            role: "checkbox",
            "aria-checked": if on { "true" } else { "false" },
            aria_label: "Select {label}",
            title: if on { "Deselect" } else { "Select" },
            onclick: move |e: Event<MouseData>| {
                e.stop_propagation();
                e.prevent_default();
                let shift = e.modifiers().shift();
                picks.write().click(pick.clone(), shift, &order.peek());
            },
            crate::Icon { name: "check-lg" }
        }
    }
}

/// The bar at the foot of the page while selecting: how many, select all,
/// delete, done. Also owns the keys, so they exist only where it is shown.
#[component]
pub fn PickBar(on_deleted: EventHandler<()>) -> Element {
    let (mut picks, order) = ctx();
    let ask_state = use_context::<Signal<Option<crate::dialogs::Ask>>>();
    // The Delete key's request for the confirm. The key listener is a plain
    // browser callback, outside Dioxus's runtime: it may write a signal, but
    // opening the dialog there (a context lookup, a new Callback) panicked the
    // app and froze the page. So it raises this, and the effect asks.
    let mut want_delete = use_signal(|| false);
    use_effect(move || {
        if *want_delete.read() {
            want_delete.set(false);
            confirm_delete(picks, on_deleted);
        }
    });

    // Esc, Ctrl/Cmd-A and Delete, page-wide, because a card's checkbox is not
    // where focus usually is. Out of the way of the dialog (which has its own
    // Esc and Enter) and of anything being typed in.
    let listener = use_hook(move || {
        use wasm_bindgen::JsCast;
        let cb = wasm_bindgen::closure::Closure::<dyn FnMut(web_sys::KeyboardEvent)>::new(
            move |e: web_sys::KeyboardEvent| {
                if ask_state.peek().is_some() {
                    return;
                }
                let typing = web_sys::window()
                    .and_then(|w| w.document())
                    .and_then(|d| d.active_element())
                    .is_some_and(|el| {
                        matches!(el.tag_name().as_str(), "INPUT" | "TEXTAREA" | "SELECT")
                    });
                if typing {
                    return;
                }
                let on = picks.peek().on;
                match e.key().as_str() {
                    "Escape" if on => {
                        e.prevent_default();
                        picks.write().stop();
                    }
                    "a" | "A" if (e.ctrl_key() || e.meta_key()) && on => {
                        e.prevent_default();
                        let o = order.peek().clone();
                        let mut w = picks.write();
                        w.note.clear();
                        w.set = o;
                    }
                    "Delete" | "Backspace" if on && !picks.peek().set.is_empty() => {
                        e.prevent_default();
                        want_delete.set(true);
                    }
                    _ => {}
                }
            },
        );
        if let Some(w) = web_sys::window() {
            let _ = w.add_event_listener_with_callback("keydown", cb.as_ref().unchecked_ref());
        }
        std::rc::Rc::new(cb)
    });
    use_drop(move || {
        use wasm_bindgen::JsCast;
        if let Some(w) = web_sys::window() {
            let _ = w.remove_event_listener_with_callback(
                "keydown",
                listener.as_ref().as_ref().unchecked_ref(),
            );
        }
    });

    let p = picks.read().clone();
    if !p.on {
        return rsx! {};
    }
    let n = p.set.len();
    let total = order.read().len();
    let all = total > 0 && order.read().iter().all(|q| p.has(q));
    rsx! {
        div { class: "pickbar", role: "toolbar", aria_label: "Selection",
            button {
                class: "icon-btn",
                title: "Done (Esc)",
                aria_label: "Stop selecting",
                onclick: move |_| picks.write().stop(),
                crate::Icon { name: "x-lg" }
            }
            span { class: "pick-n", "aria-live": "polite",
                if p.busy { "Deleting…" } else if n == 0 { "Select items" } else { "{n} selected" }
            }
            if !p.note.is_empty() {
                span { class: "pick-note", "{p.note}" }
            }
            span { class: "pick-sep", aria_hidden: "true" }
            button {
                class: "ghost",
                disabled: p.busy || total == 0,
                title: if all { "Deselect all" } else { "Select all (Ctrl+A)" },
                onclick: move |_| {
                    let o = order.peek().clone();
                    picks.write().toggle_all(&o);
                },
                if all { "Deselect all" } else { "Select all" }
            }
            button {
                class: "bad",
                disabled: p.busy || n == 0,
                title: "Delete (Del)",
                onclick: move |_| confirm_delete(picks, on_deleted),
                crate::Icon { name: "trash" }
                "Delete"
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(n: &str) -> Pick {
        n.to_string()
    }

    fn order() -> Vec<Pick> {
        vec![s("a"), s("b"), s("c"), s("d"), s("e")]
    }

    #[test]
    fn a_tick_starts_selecting_and_the_last_untick_ends_it() {
        let mut p = Picks::default();
        p.click(s("b"), false, &order());
        assert!(p.on && p.has(&s("b")));
        p.click(s("b"), false, &order());
        assert!(!p.on && p.set.is_empty());
    }

    #[test]
    fn the_select_button_survives_an_empty_selection() {
        let mut p = Picks::default();
        p.start();
        p.click(s("a"), false, &order());
        p.click(s("a"), false, &order());
        assert!(p.on && p.set.is_empty());
    }

    #[test]
    fn shift_ticks_the_range_in_page_order_either_way() {
        let mut p = Picks::default();
        p.click(s("d"), false, &order());
        p.click(s("b"), true, &order());
        assert_eq!(p.set.len(), 3);
        assert!(p.has(&s("b")) && p.has(&s("c")) && p.has(&s("d")));
        assert!(!p.has(&s("a")));
    }

    #[test]
    fn shift_with_nothing_before_it_is_a_plain_tick() {
        let mut p = Picks::default();
        p.click(s("c"), true, &order());
        assert_eq!(p.set, vec![s("c")]);
    }

    #[test]
    fn select_all_then_again_clears_but_stays_selecting() {
        let mut p = Picks::default();
        p.click(s("a"), false, &order());
        p.toggle_all(&order());
        assert_eq!(p.set.len(), 5);
        p.toggle_all(&order());
        assert!(p.on && p.set.is_empty());
    }

    #[test]
    fn what_leaves_the_screen_leaves_the_selection() {
        let mut p = Picks::default();
        p.click(s("a"), false, &order());
        p.click(s("b"), false, &order());
        p.keep(&[s("b")]);
        assert_eq!(p.set, vec![s("b")]);
        p.keep(&[]);
        assert!(!p.on);
    }

    #[test]
    fn the_confirm_counts_what_it_deletes() {
        assert_eq!(summary(&[s("a"), s("b")]), "2 collections");
        assert_eq!(summary(&[s("a")]), "1 collection");
        assert!(CONSEQUENCES.contains("cannot be undone"));
    }
}
