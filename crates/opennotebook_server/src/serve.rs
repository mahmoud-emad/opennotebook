//! The HTTP server: the five RPC domains, the byte routes, and the web app,
//! on one address.
//!
//! ```text
//! POST /api/<domain>/rpc           JSON-RPC 2.0 for mindmap, notes, session, settings, sources
//! GET  /api/<domain>/openrpc.json  that domain's OpenRPC document
//! GET  /api/domains.json           the domains and their methods
//! GET  /health.json, /api/ping     liveness
//! GET  /ui/...                     the web app, from OPENNOTEBOOK_UI_DIR
//! ...                              the byte routes `main` adds (audio, slides, events, ...)
//! ```
//!
//! Everything is served from the origin root, so the urls a session stores
//! (`/api/session/slide?...`) work as they are, and the web app finds the API
//! beside itself.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use axum::{
    Json, Router,
    body::Bytes,
    extract::DefaultBodyLimit,
    http::{StatusCode, header},
    response::{IntoResponse, Redirect, Response},
    routing::{get, post},
};
use opennotebook_api::RequestContext;
use serde_json::json;
use tower_http::services::{ServeDir, ServeFile};

/// Where it listens unless told otherwise. Loopback only: the studio holds an
/// API key and has no login, so reaching it from another machine is a choice
/// to make on purpose (`OPENNOTEBOOK_LISTEN=0.0.0.0:7878`).
pub const LISTEN_DEFAULT: &str = "127.0.0.1:7878";
pub const LISTEN_KEY: &str = "OPENNOTEBOOK_LISTEN";

/// Where the built web app is. `<data dir>/ui` unless set.
pub const UI_DIR_KEY: &str = "OPENNOTEBOOK_UI_DIR";

/// The largest request body. Uploads check their own, smaller, limit first.
const BODY_LIMIT: usize = 64 * 1024 * 1024;

/// One domain's two routes: `POST rpc` through its dispatcher, `GET
/// openrpc.json`.
macro_rules! domain {
    ($router:expr, $name:literal, $module:ident, $api:expr) => {{
        let api = Arc::new($api);
        $router
            .route(
                concat!("/api/", $name, "/rpc"),
                post(move |body: Bytes| {
                    let api = api.clone();
                    async move {
                        let out = opennotebook_api::handle(
                            &body,
                            opennotebook_api::$module::SERVICE,
                            opennotebook_api::$module::VERSION,
                            opennotebook_api::$module::OPENRPC_JSON,
                            |method, params| {
                                let api = api.clone();
                                async move {
                                    opennotebook_api::$module::dispatch(
                                        &*api,
                                        &RequestContext::default(),
                                        &method,
                                        params,
                                    )
                                    .await
                                }
                            },
                        )
                        .await;
                        rpc_response(out)
                    }
                }),
            )
            .route(
                concat!("/api/", $name, "/openrpc.json"),
                get(|| async {
                    (
                        [(header::CONTENT_TYPE, "application/json")],
                        opennotebook_api::$module::OPENRPC_JSON,
                    )
                }),
            )
    }};
}

fn rpc_response(out: Option<serde_json::Value>) -> Response {
    match out {
        Some(v) => Json(v).into_response(),
        // Only notifications: JSON-RPC sends nothing back.
        None => StatusCode::NO_CONTENT.into_response(),
    }
}

fn domains() -> serde_json::Value {
    let one = |name: &str, service: &str, methods: &[(&str, &str)]| {
        json!({
            "name": name,
            "service": service,
            "rpc": format!("/api/{name}/rpc"),
            "openrpc": format!("/api/{name}/openrpc.json"),
            "methods": methods.iter().map(|m| m.0).collect::<Vec<_>>(),
        })
    };
    use opennotebook_api::*;
    json!([
        one("mindmap", mindmap::SERVICE, mindmap::METHODS),
        one("notes", notes::SERVICE, notes::METHODS),
        one("session", session::SERVICE, session::METHODS),
        one("settings", settings::SERVICE, settings::METHODS),
        one("sources", sources::SERVICE, sources::METHODS),
    ])
}

/// The whole router: the domains, liveness, the web app, and `extra`.
pub fn router(extra: Router, ui_dir: PathBuf) -> Router {
    let mut r = Router::new();
    r = domain!(r, "mindmap", mindmap, crate::mindmap_impl::MindMapService);
    r = domain!(r, "notes", notes, crate::notes_impl::NotesService);
    r = domain!(r, "session", session, crate::session_impl::SessionService);
    r = domain!(
        r,
        "settings",
        settings,
        crate::settings_impl::SettingsService
    );
    r = domain!(r, "sources", sources, crate::sources_impl::SourcesService);

    // The web app is a single-page app: a deep link (`/ui/c/<cid>`) is served
    // its index and routes itself.
    let ui = ServeDir::new(&ui_dir).fallback(ServeFile::new(ui_dir.join("index.html")));
    r.route(
        "/health.json",
        get(|| async {
            Json(json!({
                "status": "ok",
                "service": "opennotebook",
                "version": env!("CARGO_PKG_VERSION"),
            }))
        }),
    )
    .route("/api/ping", get(|| async { "pong" }))
    .route("/api/domains.json", get(|| async { Json(domains()) }))
    .route("/", get(|| async { Redirect::temporary("/ui/") }))
    .route("/ui", get(|| async { Redirect::permanent("/ui/") }))
    .nest_service("/ui/", ui)
    .merge(extra)
    .layer(DefaultBodyLimit::max(BODY_LIMIT))
}

/// Bind and serve until interrupted.
pub async fn serve(extra: Router) -> anyhow::Result<()> {
    let listen = opennotebook_session::settings::setting(LISTEN_KEY, LISTEN_DEFAULT).await;
    let addr: SocketAddr = listen
        .parse()
        .map_err(|e| anyhow::anyhow!("{LISTEN_KEY}={listen:?} is not an address: {e}"))?;
    let ui_dir = match opennotebook_session::settings::setting(UI_DIR_KEY, "").await {
        d if d.is_empty() => opennotebook_session::paths::data_dir().join("ui"),
        d => PathBuf::from(d),
    };
    if !ui_dir.join("index.html").exists() {
        eprintln!(
            "opennotebook: no web app at {} (build it with `make build-ui`); the API still serves",
            ui_dir.display()
        );
    }
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| anyhow::anyhow!("could not listen on {addr}: {e}"))?;
    eprintln!(
        "opennotebook {} listening on http://{addr}/ (data in {})",
        env!("CARGO_PKG_VERSION"),
        opennotebook_session::paths::data_dir().display()
    );
    axum::serve(listener, router(extra, ui_dir))
        .with_graceful_shutdown(shutdown())
        .await?;
    Ok(())
}

async fn shutdown() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {}
        _ = term => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    async fn send(r: &Router, req: Request<Body>) -> (StatusCode, String) {
        let resp = r.clone().oneshot(req).await.unwrap();
        let status = resp.status();
        let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, String::from_utf8_lossy(&body).into_owned())
    }

    fn app() -> (Router, PathBuf) {
        let dir = std::env::temp_dir().join(format!("on_ui_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("assets")).unwrap();
        std::fs::write(dir.join("index.html"), "<html>app</html>").unwrap();
        std::fs::write(dir.join("assets/a.js"), "js").unwrap();
        (router(Router::new(), dir.clone()), dir)
    }

    #[tokio::test]
    async fn each_domain_answers_json_rpc_and_describes_itself() {
        let (r, _) = app();
        for d in ["mindmap", "notes", "session", "settings", "sources"] {
            let (s, body) = send(
                &r,
                Request::post(format!("/api/{d}/rpc"))
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"jsonrpc":"2.0","id":1,"method":"rpc.health"}"#,
                    ))
                    .unwrap(),
            )
            .await;
            assert_eq!(s, StatusCode::OK, "{d}");
            assert!(body.contains(r#""status":"ok""#), "{d}: {body}");
            let (s, doc) = send(
                &r,
                Request::get(format!("/api/{d}/openrpc.json"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await;
            assert_eq!(s, StatusCode::OK);
            assert!(
                doc.starts_with(r#"{"#) && doc.contains(r#""methods""#),
                "{d}"
            );
        }
        let (_, unknown) = send(
            &r,
            Request::post("/api/notes/rpc")
                .body(Body::from(r#"{"jsonrpc":"2.0","id":1,"method":"nope"}"#))
                .unwrap(),
        )
        .await;
        assert!(unknown.contains("-32601"), "{unknown}");
    }

    #[tokio::test]
    async fn the_web_app_is_served_with_deep_links_and_the_root_redirects() {
        let (r, _) = app();
        let (s, body) = send(&r, Request::get("/ui/").body(Body::empty()).unwrap()).await;
        assert_eq!((s, body.as_str()), (StatusCode::OK, "<html>app</html>"));
        let (_, body) = send(&r, Request::get("/ui/c/s123").body(Body::empty()).unwrap()).await;
        assert_eq!(body, "<html>app</html>", "a deep link gets the app");
        let (_, body) = send(
            &r,
            Request::get("/ui/assets/a.js").body(Body::empty()).unwrap(),
        )
        .await;
        assert_eq!(body, "js");
        let (s, _) = send(&r, Request::get("/").body(Body::empty()).unwrap()).await;
        assert_eq!(s, StatusCode::TEMPORARY_REDIRECT);
        let (s, body) = send(
            &r,
            Request::get("/health.json").body(Body::empty()).unwrap(),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        assert!(body.contains("opennotebook"));
    }
}
