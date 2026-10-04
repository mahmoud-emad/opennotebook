//! Where the server is, and every way the app talks to it.
//!
//! The RPC domains go through the clients generated from the server's own
//! oschema (`opennotebook_sdk`), one constructor each, so a schema change breaks
//! the build rather than the page. The few plain HTTP routes (settings, source
//! fetch and upload, the chat stream) share one fetch underneath.

use opennotebook_sdk::mindmap::MindMapServiceClient;
use opennotebook_sdk::notes::NotesServiceClient;
use opennotebook_sdk::session::SessionServiceClient;
use opennotebook_sdk::sources::SourcesServiceClient;

/// The service root this bundle was served under, with no trailing slash.
///
/// The bundle lives at `<mount>/ui/` and the API at `<mount>/api/session`, so
/// the service root is whatever precedes `/ui`. Derived rather than declared:
/// the same bytes are meant to serve at any mount.
pub(crate) fn service_root() -> String {
    let path = web_sys::window()
        .and_then(|w| w.location().pathname().ok())
        .unwrap_or_default();
    match path.find("/ui") {
        Some(i) => path[..i].to_string(),
        // Served from somewhere that is not the bundle mount (dx serve, a test
        // harness). Fall back to the origin root rather than guessing a prefix.
        None => String::new(),
    }
}

pub(crate) fn api_base() -> String {
    format!("{}/api/session", service_root())
}

/// One browser-fetch client per RPC domain, at `<service root><path>`.
///
/// `new_at` is the browser-fetch constructor, symmetric to the native
/// `connect_http_at`: it takes the full endpoint and appends nothing. The
/// native one does not exist on wasm32 because its transport resolves socket
/// paths through the shared core library, which is not reachable here.
///
/// Each also has a host twin that only has to compile. This crate is a browser
/// bundle and `dx build --target wasm32` is the only build that produces
/// anything usable, but `lab build` resolves `crates/opennotebook_ui` as its own
/// workspace root (deliberately: a wasm crate cannot share a target directory
/// with the native crates), fails to match it against its own `[[services]]`
/// entry, and cargo-builds it for the host as an ordinary binary. That host
/// build used to fail on `new_at`, which is wasm-only, and took the whole
/// `lab build opennotebook` down with it while the server crate beside it was
/// green. There is no meaningful native behaviour to give it: the service root
/// is derived from `location.pathname`, which does not exist outside a
/// browser, so the host twin returns the reason instead.
macro_rules! browser_client {
    ($(#[$doc:meta])* $name:ident -> $ty:ty, $path:literal) => {
        $(#[$doc])*
        #[cfg(target_arch = "wasm32")]
        pub(crate) fn $name() -> Result<$ty, String> {
            <$ty>::new_at(&format!("{}{}", service_root(), $path)).map_err(|e| e.to_string())
        }

        $(#[$doc])*
        #[cfg(not(target_arch = "wasm32"))]
        pub(crate) fn $name() -> Result<$ty, String> {
            Err("opennotebook_ui is a browser bundle; it has no native client".to_string())
        }
    };
}

browser_client!(
    /// Sessions and collections: listing, preparing, estimating, deleting.
    client -> SessionServiceClient, "/api/session/rpc"
);
browser_client!(
    /// A collection's sources: listing, removing, researching, asking them.
    sources_client -> SourcesServiceClient, "/api/sources/rpc"
);
browser_client!(
    /// Mind maps.
    mindmap_client -> MindMapServiceClient, "/api/mindmap/rpc"
);
browser_client!(
    /// Study notes.
    notes_client -> NotesServiceClient, "/api/notes/rpc"
);

/// A `bool` reply of a rename, pin or delete as a result: false means the
/// thing was not there to change.
pub(crate) fn still_there(found: bool) -> Result<(), String> {
    if found {
        Ok(())
    } else {
        Err("it is no longer there".to_string())
    }
}

/// An RPC error as a sentence somebody can act on.
pub(crate) fn clean_rpc_error(e: &str) -> String {
    // The wire form is `RPC error -32602: <message>`; only the message is for a
    // person, and the commonest one here is worth rewording entirely.
    let msg = e.rsplit(": ").next().unwrap_or(e);
    if msg.contains("no sources") {
        "Add a source first: a link, a note, or a topic to research.".to_string()
    } else {
        msg.to_string()
    }
}

/// The largest file a source upload takes, in MB: the server's
/// `create::MAX_UPLOAD_BYTES`. Every hint and refusal on the page says this one.
pub(crate) const UPLOAD_MAX_MB: u32 = 25;

/// This browser's local storage, where there is one.
pub(crate) fn storage() -> Option<web_sys::Storage> {
    web_sys::window()?.local_storage().ok().flatten()
}

pub(crate) async fn gloo_sleep(ms: i32) {
    let p = js_sys::Promise::new(&mut |res, _| {
        if let Some(w) = web_sys::window() {
            let _ = w.set_timeout_with_callback_and_timeout_and_arguments_0(&res, ms);
        }
    });
    let _ = wasm_bindgen_futures::JsFuture::from(p).await;
}

// ── plain HTTP ───────────────────────────────────────────────────────────────

/// What a request carries.
enum Body<'a> {
    None,
    Json(&'a str),
    File(&'a web_sys::File),
}

/// Send one request and hand back the response, whatever its status.
async fn fetch(url: &str, body: Body<'_>) -> Result<web_sys::Response, String> {
    use wasm_bindgen::JsCast;
    let opts = web_sys::RequestInit::new();
    match &body {
        Body::None => {}
        Body::Json(b) => {
            opts.set_method("POST");
            opts.set_body(&wasm_bindgen::JsValue::from_str(b));
        }
        Body::File(f) => {
            opts.set_method("POST");
            opts.set_body(f);
        }
    }
    let req = web_sys::Request::new_with_str_and_init(url, &opts)
        .map_err(|_| "bad request".to_string())?;
    if matches!(body, Body::Json(_)) {
        req.headers().set("Content-Type", "application/json").ok();
    }
    let w = web_sys::window().ok_or("no window")?;
    let resp = wasm_bindgen_futures::JsFuture::from(w.fetch_with_request(&req))
        .await
        .map_err(|_| "network error".to_string())?;
    resp.dyn_into().map_err(|_| "not a response".to_string())
}

/// A response's whole body as text.
async fn text_of(resp: &web_sys::Response) -> Result<String, String> {
    let txt = wasm_bindgen_futures::JsFuture::from(resp.text().map_err(|_| "no body".to_string())?)
        .await
        .map_err(|_| "unreadable body".to_string())?;
    Ok(txt.as_string().unwrap_or_default())
}

/// The response, or an error naming its status when it is not a success.
fn ok_or_status(resp: web_sys::Response) -> Result<web_sys::Response, String> {
    if resp.ok() {
        Ok(resp)
    } else {
        Err(format!("HTTP {}", resp.status()))
    }
}

/// GET a URL and read the whole reply.
pub(crate) async fn get_text(url: &str) -> Result<String, String> {
    let resp = ok_or_status(fetch(url, Body::None).await?)?;
    text_of(&resp).await
}

/// POST a JSON body and read the whole reply.
pub(crate) async fn post_json(url: &str, body: &str) -> Result<String, String> {
    let resp = ok_or_status(fetch(url, Body::Json(body)).await?)?;
    text_of(&resp).await
}

/// POST a file as the raw request body and read the whole reply. A refusal
/// says why in its JSON `error`, which is what comes back as the `Err`.
pub(crate) async fn post_file(url: &str, file: &web_sys::File) -> Result<String, String> {
    let resp = fetch(url, Body::File(file)).await?;
    let txt = text_of(&resp).await.unwrap_or_default();
    if resp.ok() {
        return Ok(txt);
    }
    let why = serde_json::from_str::<serde_json::Value>(&txt)
        .ok()
        .and_then(|v| v["error"].as_str().map(str::to_string))
        .filter(|e| !e.trim().is_empty());
    Err(match (why, resp.status()) {
        (Some(e), _) => e,
        (None, 413) => format!("the file is larger than {UPLOAD_MAX_MB} MB"),
        (None, n) => format!("HTTP {n}"),
    })
}

/// POST, then hand each server-sent event's JSON to `on_event` as it arrives.
///
/// The agent's turn is seconds to a minute of visible work, so its lines have
/// to reach the page while it happens, not after. `EventSource` cannot POST,
/// so the response body is read as a stream and cut on the SSE frame boundary.
pub(crate) async fn post_stream(
    url: &str,
    body: &str,
    mut on_event: impl FnMut(serde_json::Value),
) -> Result<(), String> {
    use wasm_bindgen::JsCast;
    let resp = ok_or_status(fetch(url, Body::Json(body)).await?)?;
    let stream = resp.body().ok_or("no body")?;
    let reader: web_sys::ReadableStreamDefaultReader = stream
        .get_reader()
        .dyn_into()
        .map_err(|_| "no reader".to_string())?;
    let mut buf: Vec<u8> = Vec::new();
    loop {
        let chunk = wasm_bindgen_futures::JsFuture::from(reader.read())
            .await
            .map_err(|_| "the answer was cut off".to_string())?;
        let done = js_sys::Reflect::get(&chunk, &"done".into())
            .ok()
            .and_then(|d| d.as_bool())
            .unwrap_or(true);
        if let Ok(v) = js_sys::Reflect::get(&chunk, &"value".into())
            && !v.is_undefined()
        {
            buf.extend(js_sys::Uint8Array::new(&v).to_vec());
        }
        // Frames end in a blank line; each carries one `data:` JSON object.
        while let Some(pos) = buf.windows(2).position(|w| w == b"\n\n") {
            let frame: Vec<u8> = buf.drain(..pos + 2).collect();
            let frame = String::from_utf8_lossy(&frame);
            let data: String = frame
                .lines()
                .filter_map(|l| l.strip_prefix("data:"))
                .map(|l| l.strip_prefix(' ').unwrap_or(l))
                .collect::<Vec<_>>()
                .join("\n");
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&data) {
                on_event(v);
            }
        }
        if done {
            return Ok(());
        }
    }
}
