//! Which screen is showing, and its address.
//!
//! The screen lives in the URL, so a refresh lands where you were and the
//! browser's Back button walks the screens. The bundle's mount is arbitrary,
//! so a route is always whatever follows `/ui`, never a fixed absolute path.

use crate::Output;
use crate::api::service_root;

/// The screen this URL names.
///
/// The mount is arbitrary — the bundle is served at `<prefix>/local/opennotebook/ui/`
/// and a router may put a prefix in front — so the route is whatever follows
/// `/ui`, never a fixed absolute path.
///
/// Reading the screen off the URL instead of holding it only in a signal is what
/// makes a refresh land where you were.
///
/// The addresses from before collections still work: `new-session` and its
/// siblings make a collection and open it on that kind, and a map's or notes'
/// own `mind-map/<sid>/<id>` names the same screen its `c/<sid>/…` form does,
/// so the address bar is quietly corrected to that form.
pub(crate) fn route_from_location() -> View {
    let path = web_sys::window()
        .and_then(|w| w.location().pathname().ok())
        .unwrap_or_default();
    match path.find("/ui") {
        Some(i) => parse_route(&path[i + 3..]),
        None => View::Home,
    }
}

/// [`route_from_location`] on the part of the path after `/ui`.
pub(crate) fn parse_route(rest: &str) -> View {
    let rest = rest.trim_matches('/');
    let parts: Vec<&str> = rest.split('/').collect();
    let ok = |s: &str| {
        !s.is_empty()
            && s.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    };
    let item = |kind: &str, id: &str| match kind {
        MIND_MAP_ROUTE if ok(id) => Some(Open::Map(id.to_string())),
        STUDY_NOTES_ROUTE if ok(id) => Some(Open::Notes(id.to_string())),
        _ => None,
    };
    match parts.as_slice() {
        [""] => View::Home,
        [COLLECTIONS_ROUTE] => View::All,
        ["c", cid] if ok(cid) => View::Collection {
            cid: cid.to_string(),
            open: None,
        },
        ["c", cid, kind, id] if ok(cid) => View::Collection {
            cid: cid.to_string(),
            open: item(kind, id),
        },
        // Before collections: a map's or notes' own address, on its sid.
        [kind @ (MIND_MAP_ROUTE | STUDY_NOTES_ROUTE), sid, id] if ok(sid) => View::Collection {
            cid: sid.to_string(),
            open: item(kind, id),
        },
        ["new-session"] => View::New(Output::Session),
        ["new-mind-map"] => View::New(Output::MindMap),
        ["new-study-notes"] => View::New(Output::Notes),
        ["new-audio-overview"] => View::New(Output::Audio),
        _ => View::Home,
    }
}

/// The path segment of the page listing every collection.
pub(crate) const COLLECTIONS_ROUTE: &str = "collections";

/// The path segment of one map's own address: c/<cid>/mind-map/<id>.
pub(crate) const MIND_MAP_ROUTE: &str = "mind-map";

/// The same for a set of study notes: c/<cid>/study-notes/<id>.
pub(crate) const STUDY_NOTES_ROUTE: &str = "study-notes";

/// The address of a screen, under the bundle's mount.
pub(crate) fn route_url(view: &View) -> String {
    let root = service_root();
    match view {
        View::Home | View::New(_) => format!("{root}/ui/"),
        View::All => format!("{root}/ui/{COLLECTIONS_ROUTE}"),
        View::Collection { cid, open: None } => format!("{root}/ui/c/{cid}"),
        View::Collection {
            cid,
            open: Some(Open::Map(id)),
        } => format!("{root}/ui/c/{cid}/{MIND_MAP_ROUTE}/{id}"),
        View::Collection {
            cid,
            open: Some(Open::Notes(id)),
        } => format!("{root}/ui/c/{cid}/{STUDY_NOTES_ROUTE}/{id}"),
    }
}

/// Put `view` in the address bar without reloading.
///
/// `push_state` and not `location.assign`: this is the same document, and a real
/// navigation would drop what the page holds. Pushing also means Back works —
/// the browser's own button walks the screens. `replace` swaps the current
/// entry instead: for a map opened beside the Studio, which is not a page the
/// Back button should have to walk through, and for an old address corrected
/// to its new form.
///
/// A failure is silently ignored: the screen still changed, and an address bar
/// that did not keep up is worth less than a panic.
pub(crate) fn set_route(view: &View, replace: bool) {
    let Some(w) = web_sys::window() else { return };
    let url = route_url(view);
    if let Ok(h) = w.history() {
        let _ = if replace {
            h.replace_state_with_url(&wasm_bindgen::JsValue::NULL, "", Some(&url))
        } else {
            h.push_state_with_url(&wasm_bindgen::JsValue::NULL, "", Some(&url))
        };
    }
}

/// Which screen. A signal rather than a router crate: three screens and the
/// player, which is a different page entirely. A router would be a dependency
/// and a set of URL contracts bought for one match.
#[derive(Clone, PartialEq, Debug)]
pub(crate) enum View {
    /// Home: the recent collections and the way to start one.
    Home,
    /// Every collection.
    All,
    /// One collection, with a map or notes open beside it or not.
    Collection { cid: String, open: Option<Open> },
    /// An address from before collections that asked for a new piece of work:
    /// a collection is made, and opened on that kind.
    New(Output),
}

/// What is open in the viewer beside a collection's Studio.
#[derive(Clone, PartialEq, Debug)]
pub(crate) enum Open {
    Map(String),
    Notes(String),
}

#[cfg(test)]
mod route_tests {
    use super::{Open, Output, View, parse_route};

    fn coll(cid: &str, open: Option<Open>) -> View {
        View::Collection {
            cid: cid.into(),
            open,
        }
    }

    #[test]
    fn each_screen_has_its_address() {
        assert_eq!(parse_route("/"), View::Home);
        assert_eq!(parse_route(""), View::Home);
        assert_eq!(parse_route("/collections"), View::All);
        assert_eq!(
            parse_route("/c/s1790000000000"),
            coll("s1790000000000", None)
        );
        assert_eq!(
            parse_route("/c/s1/mind-map/m2"),
            coll("s1", Some(Open::Map("m2".into())))
        );
        assert_eq!(
            parse_route("/c/s1/study-notes/n2/"),
            coll("s1", Some(Open::Notes("n2".into())))
        );
    }

    /// The addresses from before collections still land somewhere sensible.
    #[test]
    fn old_addresses_still_work() {
        assert_eq!(parse_route("/new-session"), View::New(Output::Session));
        assert_eq!(parse_route("/new-audio-overview"), View::New(Output::Audio));
        assert_eq!(parse_route("/new-mind-map"), View::New(Output::MindMap));
        assert_eq!(parse_route("/new-study-notes"), View::New(Output::Notes));
        assert_eq!(
            parse_route("/mind-map/s1/m2"),
            coll("s1", Some(Open::Map("m2".into())))
        );
        assert_eq!(
            parse_route("/study-notes/s1/n2"),
            coll("s1", Some(Open::Notes("n2".into())))
        );
    }

    /// Anything else is home, never a collection named by a stray path.
    #[test]
    fn nonsense_is_home() {
        assert_eq!(parse_route("/c/"), View::Home);
        assert_eq!(parse_route("/c/a b"), View::Home);
        assert_eq!(parse_route("/c/s1/elsewhere/x"), coll("s1", None));
        assert_eq!(parse_route("/whatever"), View::Home);
    }
}
