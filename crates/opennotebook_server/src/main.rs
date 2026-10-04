//! OpenNotebook as a service.
//!
//! One process, one address (`127.0.0.1:7878` unless `OPENNOTEBOOK_LISTEN`
//! says otherwise), serving:
//!
//!   POST /api/<domain>/rpc             JSON-RPC 2.0: mindmap, notes, session,
//!                                      settings, sources (see `serve`)
//!   GET  /api/session/audio            a narration line's WAV bytes
//!   GET  /api/session/slide            a slide's rendered HTML
//!   GET  /api/session/events           the player's event stream (SSE)
//!   GET  /api/session/player           the player page
//!   GET  /ui/                          the web app
//!
//! The byte routes ride `/api/` beside the JSON-RPC rather than on a server
//! of their own: a session stores root-relative urls to its audio and slides,
//! and the player and the web app find the API beside themselves.
//!
//! The same binary is also the prep job: `opennotebook_server prep --spec
//! <file> --job <id>` builds one session and exits (see `prep_cmd`).

use std::path::{Component, Path};

use axum::{
    Router,
    extract::Query,
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::get,
};
use serde::Deserialize;

use opennotebook_session::{Session, SessionStore};

// The five domains' types and traits, generated from opennotebook_api's
// oschema/, under the names the impl modules use (`crate::session::...`).
pub use opennotebook_api::{mindmap, notes, session, settings, sources};

mod agent;
mod ask;
mod banter;
mod collection;
mod cover;
mod create;
mod dispatch;
mod estimate;
mod estimate_live;
mod events;
mod filestore;
mod mindmap_impl;
mod notes_impl;
mod pipeline;
mod playback;
mod player;
mod prep_cmd;
mod research;
mod serve;
mod session_impl;
mod settings_api;
mod settings_impl;
mod sources_impl;

/// Route prefix for everything this service serves. Kept in one place because
/// the byte routes and the urls embedded in a `Session` must agree, and a
/// mismatch there is a 404 a player cannot diagnose.
const AUDIO_ROUTE: &str = "/api/session/audio";
/// The whole narration as one WAV, to download: an audio overview's episode.
const EPISODE_ROUTE: &str = "/api/session/episode";
const SLIDE_ROUTE: &str = "/api/session/slide";
/// A collection's cover as a self-contained page (`?collection=<cid>&v=`).
const COVER_ROUTE: &str = "/api/session/cover";
/// A sample slide in a style, drawn with its kit (`?style=<id>`).
const STYLE_SAMPLE_ROUTE: &str = "/api/session/style_sample";
/// §4's event stream. Declared in the manifest as `sse` **together with** this
/// route: the previous slice dropped the declaration rather than leave a
/// protocol nothing answered, and it comes back only now that it does.
const EVENTS_ROUTE: &str = "/api/session/events";
/// The player itself. A plain page, per §4.
const PLAYER_ROUTE: &str = "/api/session/player";

/// The session index and the prep form. The player plays one session; this is
/// where you find out which sessions there are and make another.
const CONSOLE_ROUTE: &str = "/api/session/console";

/// The voice turn. POST, because the body is the question's audio.
const ASK_ROUTE: &str = "/api/session/ask";

/// The narrator noticing a hand is up. GET: it carries no body.
const ACK_ROUTE: &str = "/api/session/ack";

/// The narrator taking the question. GET: it carries no body either.
const THANKS_ROUTE: &str = "/api/session/thanks";

/// Which build is serving. Plain text, so a page can compare one string.
const BUILD_ROUTE: &str = "/api/session/build";

/// Staging sources for a session that does not exist yet.
const SRC_TEXT_ROUTE: &str = "/api/session/source/text";
const SRC_FETCH_ROUTE: &str = "/api/session/source/fetch";
const SRC_LIST_ROUTE: &str = "/api/session/source/list";
/// A file handed over whole: the body is its bytes, `?name=` its name.
const SRC_UPLOAD_ROUTE: &str = "/api/session/source/upload";

/// The create page's conversation. Text in, text out.
const CHAT_ROUTE: &str = "/api/session/chat";

/// The settings dialog: GET every setting, POST one. Stored in settings.toml.
const SETTINGS_ROUTE: &str = "/api/session/settings";

/// The voice detector the page runs in its `AudioWorklet`.
///
/// Served as bytes read at request time, not `include_bytes!`. The wasm is a
/// build artifact and a build artifact does not belong in git, which rules out
/// compiling it in. A 404 here is a supported state: `player.html` falls back
/// to its peak threshold rather than losing the microphone, so a box that never
/// ran `scripts/build-vad-wasm.sh` behaves exactly as it did before this crate
/// existed.
const VAD_WASM_ROUTE: &str = "/api/session/vad.wasm";

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().nth(1).as_deref() == Some("--version") {
        println!("opennotebook {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }

    // `prep` is the dispatched half: the server runs this same binary with
    // this subcommand for each prep. It must be decided BEFORE anything binds,
    // because a prep run that fell through to serving would try to bind the
    // address the live server already holds.
    if prep_cmd::is_prep_invocation() {
        return run_prep().await;
    }

    // Close what a previous server left live: its watchers died with it.
    match opennotebook_build::job::recover().await {
        Ok(0) => {}
        Ok(n) => eprintln!("opennotebook: closed {n} prep job(s) left by the last run"),
        Err(e) => eprintln!("opennotebook: could not check for interrupted preps: {e}"),
    }

    let extra = Router::new()
        .route(AUDIO_ROUTE, get(serve_audio))
        .route(EPISODE_ROUTE, get(serve_episode))
        .route(SLIDE_ROUTE, get(serve_slide))
        .route(STYLE_SAMPLE_ROUTE, get(serve_style_sample))
        .route(COVER_ROUTE, get(serve_cover))
        .route(EVENTS_ROUTE, get(player::serve_events))
        .route(PLAYER_ROUTE, get(player::serve_page))
        .route(CONSOLE_ROUTE, get(player::serve_console))
        .route(ASK_ROUTE, axum::routing::post(ask::serve_ask))
        .route(ACK_ROUTE, get(ask::serve_ack))
        .route(THANKS_ROUTE, get(ask::serve_thanks))
        .route(BUILD_ROUTE, get(player::serve_build))
        .route(SRC_TEXT_ROUTE, axum::routing::post(create::stage_text))
        .route(SRC_FETCH_ROUTE, axum::routing::post(create::fetch_sources))
        // The handler reads the raw body against its own 25 MB limit, so the
        // 64 MiB the listener allows is never what a person hits first.
        .route(SRC_UPLOAD_ROUTE, axum::routing::post(create::upload_source))
        .route(SRC_LIST_ROUTE, get(create::list_sources))
        .route(CHAT_ROUTE, axum::routing::post(agent::chat))
        .route(
            SETTINGS_ROUTE,
            get(settings_api::serve_get).post(settings_api::serve_set),
        )
        .route(VAD_WASM_ROUTE, get(ask::serve_vad_wasm));

    serve::serve(extra).await
}

/// Leave a `Failed` session behind when the prep dies before the pipeline makes
/// one.
///
/// `build_session` writes a failure row for everything it owns — deck, measure,
/// narrate, validate. Nothing owned the two stages IN FRONT of it. A prep that
/// died during ingest or script generation exited non-zero with the reason on
/// stderr and wrote no session at all, and `session_get` answers `found: false`
/// for a session that does not exist, which is indistinguishable from one whose
/// prep has not got there yet. So the create page sat on "starting up…"
/// forever, with a failed prep job two clicks away that it never looked at.
/// Observed on a job that died building its retrieval index and span the page
/// until it was closed.
///
/// Written only over the `Preparing` placeholder: a failure inside
/// `build_session` has a better one, naming the phase that failed, and this must
/// not flatten it, and a row that is gone was deleted.
///
/// Every step is best-effort. This runs on a path that is already failing and
/// is about to return that failure, so a store that cannot be reached costs the
/// message and nothing else — the exit code and stderr are unchanged.
async fn record_prep_failure(
    req: &session::SessionPrepareReq,
    job_sid: &str,
    err: &anyhow::Error,
    spent_usd: Option<f64>,
) {
    use opennotebook_session::{Session, SessionState, SessionStore};

    let Ok(store) = SessionStore::open() else {
        return;
    };

    // A row that already says why it failed is left alone. A `Preparing` one is
    // the placeholder `session_prepare` writes before dispatch, and is exactly
    // what this replaces. No row at all means it was deleted — the placeholder
    // is written before any prep is dispatched — and writing one would
    // undelete it. An error reading is not a reason to skip writing.
    match store.get(&req.sid).await {
        Ok(Some(row)) if row.state != SessionState::Preparing => return,
        Ok(None) => return,
        _ => {}
    }
    // Nor onto a collection that was deleted while this ran: a `Failed` row
    // naming it would bring it back to the list. See `collection::still_wanted`.
    if !collection::still_wanted(&store, req.collection.as_deref()).await {
        return;
    }

    let session = Session {
        sid: req.sid.clone(),
        title: req.title.clone(),
        collection_name: req.sid.clone(),
        collection_sid: None,
        deck_ref: None,
        // The request's, so a retry of the failed row has voices to reuse.
        speakers: pipeline::speakers(req),
        slides: Vec::new(),
        state: SessionState::Failed,
        prep_job_sid: Some(job_sid.to_string()),
        failure: Some(format!("{err} (prep job {job_sid})")),
        pinned: false,
        // Both from the request, so a failed output still shows as the kind it
        // was meant to be, in the collection it was meant for.
        audio: session_impl::audio_spec(
            req.audio_format.as_deref(),
            req.audio_length.as_deref(),
            req.focus.as_deref(),
        ),
        collection: req.collection.clone().filter(|c| !c.is_empty()),
        spent_usd,
        style: session_impl::deck_style(req),
    };
    if store.put(&session).await.is_ok() {
        collection::output_finished(&store, &session).await;
    }
}

/// One dispatched prep run: the whole of §3, start to finish.
///
/// The job row this runs as is named by `--job` (or found by
/// [`opennotebook_build::job::find_own`]) and passed down, so progress lands on
/// that row and `Session.prep_job_sid` names it. There is no second row — see
/// `opennotebook_build::job`.
async fn run_prep() -> anyhow::Result<()> {
    let spec = prep_cmd::spec_path();
    let req = prep_cmd::load_spec(&spec)?;

    // Refuse rather than fabricate. A prep with no row to report against would
    // run the full pipeline — minutes of rendering and synthesis — writing
    // progress nowhere. The job id is the link between the two halves and its
    // absence is a real failure.
    let job_sid = match prep_cmd::job_id() {
        Some(id) => id,
        None => opennotebook_build::job::find_own(&req.sid)
            .await?
            .ok_or_else(|| {
                anyhow::anyhow!(
                    "no job row for session `{}`: a prep is queued through job::submit, \
                     which creates the row this process adopts",
                    req.sid
                )
            })?,
    };

    eprintln!(
        "opennotebook prep: session `{}` on job {} from {}",
        req.sid,
        job_sid,
        spec.display()
    );

    let layout = pipeline::Layout::in_data_dir();
    // Every model call the prep makes is recorded on this, and its total is
    // stored on the session row. The catalog prices a call whose provider
    // reported no cost; without the catalog such a call leaves it unknown.
    let ledger = opennotebook_session::spend::Ledger::new(estimate_live::spend_prices().await);
    let ran =
        opennotebook_session::spend::scope(ledger.clone(), pipeline::run(&req, &job_sid, &layout))
            .await;
    let spent = ledger.totals();
    eprintln!("opennotebook prep: {}", spent.log_line());
    let session = match ran {
        Ok(s) => s,
        // What this was being made for was deleted. Nothing to record and
        // nothing failed: exit clean, so the job row does not show a failure
        // for a job somebody meant to throw away.
        Err(e)
            if matches!(
                e.downcast_ref::<opennotebook_build::BuildError>(),
                Some(opennotebook_build::BuildError::Abandoned { .. })
            ) =>
        {
            eprintln!("opennotebook prep: {e}");
            return Ok(());
        }
        Err(e) => {
            record_prep_failure(&req, &job_sid, &e, spent.known_usd()).await;
            return Err(e);
        }
    };

    // `build_session` writes `Ready` and nothing else does. Reporting anything
    // other than what the store ended up holding would be the success-shaped
    // failure this project keeps meeting, so the state is read off the session
    // rather than inferred from having reached this line.
    eprintln!(
        "opennotebook prep: session `{}` finished state={:?} slides={} job={}",
        session.sid,
        session.state,
        session.slides.len(),
        job_sid
    );
    anyhow::ensure!(
        session.state == opennotebook_session::SessionState::Ready,
        "session `{}` finished in state {:?} rather than Ready",
        session.sid,
        session.state
    );
    if let Ok(store) = collection::open_store().await {
        collection::output_finished(&store, &session).await;
    }
    Ok(())
}

// ── byte routes ──────────────────────────────────────────────────────────────

/// `GET /api/session/audio?session=<sid>&line=<line_id>`
///
/// Query parameters rather than path segments, the same shape as every other
/// byte route here: a missing parameter is refused by name (`Failed to
/// deserialize query string: missing field ...`).
#[derive(Deserialize)]
struct AudioQuery {
    session: String,
    line: String,
}

#[derive(Deserialize)]
struct EpisodeQuery {
    session: String,
}

/// Silence before a line when the speaker changes, when the same speaker goes
/// on, and when a new chapter starts. People leave about 200 ms between turns
/// in conversation (Stivers et al., 2009); a chapter is a breath longer. None
/// of the open podcast generators read for the spec leaves any gap at all.
const GAP_TURN_MS: u32 = 220;
const GAP_SAME_MS: u32 = 320;
const GAP_CHAPTER_MS: u32 = 650;

/// The pause in front of a line: a new chapter, the same speaker going on, or
/// the other speaker answering.
fn gap_before(first_of_chapter: bool, same_speaker: bool) -> u32 {
    if first_of_chapter {
        GAP_CHAPTER_MS
    } else if same_speaker {
        GAP_SAME_MS
    } else {
        GAP_TURN_MS
    }
}

/// `GET /api/session/episode?session=` — every line, in order, as one WAV with
/// natural pauses between them. NotebookLM's download is a WAV too.
async fn serve_episode(Query(q): Query<EpisodeQuery>) -> Response {
    let session = match load(&q.session).await {
        Ok(Some(s)) => s,
        Ok(None) => return not_found("no such session"),
        Err(e) => return upstream(e),
    };
    let mut clips: Vec<(String, Vec<u8>, u32)> = Vec::new();
    let mut prev: Option<String> = None;
    for (ci, slide) in session.slides_in_order().into_iter().enumerate() {
        for (li, line) in slide.lines_in_order().into_iter().enumerate() {
            let Some(path) = line.audio_path.as_ref() else {
                return not_found("the narration is not synthesised yet");
            };
            let bytes = match std::fs::read(path) {
                Ok(b) => b,
                Err(e) => {
                    return (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        format!("audio recorded at {path} but unreadable: {e}"),
                    )
                        .into_response();
                }
            };
            let same = prev.as_deref() == Some(line.speaker_id.0.as_str());
            let gap = gap_before(ci > 0 && li == 0, same);
            prev = Some(line.speaker_id.0.clone());
            clips.push((line.line_id.0.clone(), bytes, gap));
        }
    }
    match opennotebook_build::wav::join(&clips) {
        Ok(wav) => {
            let name = file_name_of(&session.title);
            (
                StatusCode::OK,
                [
                    (header::CONTENT_TYPE, "audio/wav".to_string()),
                    (
                        header::CONTENT_DISPOSITION,
                        format!("attachment; filename=\"{name}.wav\""),
                    ),
                    (header::CACHE_CONTROL, "private, max-age=3600".to_string()),
                ],
                wav,
            )
                .into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

/// A title as a download's file name: letters, digits, spaces and dashes only,
/// so it can sit inside a quoted header value.
fn file_name_of(title: &str) -> String {
    let clean: String = title
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == ' ' || c == '-' {
                c
            } else {
                ' '
            }
        })
        .collect();
    let name = clean.split_whitespace().collect::<Vec<_>>().join(" ");
    if name.is_empty() {
        "Audio overview".to_string()
    } else {
        name
    }
}

#[derive(Deserialize)]
struct SlideQuery {
    session: String,
    ordinal: u32,
    /// Ask for a THUMBNAIL rather than the slide itself.
    ///
    /// Same bytes on success. The difference is what a failure means: a slide
    /// the player cannot fetch is a broken session and must say so loudly, but
    /// a thumbnail is decorative, and a gallery of a dozen sessions should not
    /// show a browser error page because one old deck was deleted upstream.
    /// With this set, an upstream failure renders a small "unavailable" card at
    /// 200 instead.
    /// Any non-empty value means yes. NOT a `bool`: serde's bool reader accepts
    /// only "true"/"false" from a query string, so `thumb=1` was rejected with a
    /// 400 — the gallery asked for a forgiving thumbnail and got a hard failure
    /// from the parser before the route ever ran.
    #[serde(default)]
    thumb: String,
}

/// Serve one narration line's WAV.
///
/// The path is read out of the stored session rather than taken from the
/// caller, which is what keeps this from being a file-read primitive pointed at
/// an arbitrary path. `containment` is a second check on top of that, for the
/// case where a stored path is itself wrong.
async fn serve_audio(Query(q): Query<AudioQuery>) -> Response {
    let session = match load(&q.session).await {
        Ok(Some(s)) => s,
        Ok(None) => return not_found("no such session"),
        Err(e) => return upstream(e),
    };

    let line = session
        .slides
        .iter()
        .flat_map(|s| s.lines.iter())
        .find(|l| l.line_id.0 == q.line);

    let Some(line) = line else {
        return not_found("no such line in that session");
    };

    // `audio_path` is absent until the line is synthesised. That is a real
    // state, not an error: a session still preparing has lines with no audio
    // yet, and 404 says so more honestly than 500.
    let Some(path) = line.audio_path.as_ref() else {
        return not_found("line has no audio yet");
    };

    match std::fs::read(path) {
        Ok(bytes) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "audio/wav"),
                // The bytes are immutable once written: a re-render writes a new
                // line id rather than overwriting, because Kokoro is not
                // reproducible and a cached duration would go stale.
                (header::CACHE_CONTROL, "private, max-age=3600"),
            ],
            bytes,
        )
            .into_response(),
        // The store said there is audio and the filesystem disagrees. That is a
        // broken session rather than a missing one, so it is not a 404.
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("audio recorded at {path} but unreadable: {e}"),
        )
            .into_response(),
    }
}

// Serve one slide's render, read from the session's deck on disk.
//
// The format is the deck's own, read per slide, so an html deck gets HTML and
// a png deck (built by an earlier slide service) gets a PNG. This route used to ask for HTML unconditionally, which
// on a png deck returned an empty string with a SUCCESSFUL result: the wrong
// door looked like a working one, and the liveness check below was the only
// thing that made it visible.
//
// A slide's HTML is self-contained (its kit inlines fonts and styles), so the
// html arm is a passthrough.

/// Each session's slide refs, by sid, with when they were read.
type SlideRefs = std::collections::HashMap<
    String,
    (
        std::time::Instant,
        Vec<(u32, opennotebook_session::SlideRef)>,
    ),
>;

/// Where a session's slides live, so thirty-five covers cost one lookup.
///
/// A gallery load issues 35 requests to `/api/session/slide`, and every one of
/// them used to `load()` the whole session document from the store — the same
/// document, 35 times, megabytes each, to read one `slide_ref`. The store keeps
/// a session as a single JSON value, so there is no way to fetch one slide's
/// ref on its own; the fix is to stop asking 35 times.
///
/// Keyed by sid and holding only what the slide route needs. Same TTL as the
/// render cache and for the same reason: Retry rebuilds under the same sid.
fn slide_refs_cache() -> &'static std::sync::Mutex<SlideRefs> {
    static C: std::sync::OnceLock<std::sync::Mutex<SlideRefs>> = std::sync::OnceLock::new();
    C.get_or_init(Default::default)
}

/// The slide at `ordinal`, from cache when we have it.
///
/// `Ok(None)` means the session exists and has no slide there — a real answer,
/// not a failure. `Err` is the store being unreadable, which is a different thing
/// and must not be cached.
async fn slide_ref_for(
    sid: &str,
    ordinal: u32,
) -> anyhow::Result<Option<Option<opennotebook_session::SlideRef>>> {
    if let Ok(c) = slide_refs_cache().lock()
        && let Some((at, refs)) = c.get(sid)
        && at.elapsed() <= SLIDE_TTL
    {
        return Ok(Some(
            refs.iter()
                .find(|(o, _)| *o == ordinal)
                .map(|(_, r)| r.clone()),
        ));
    }

    let Some(session) = load(sid).await? else {
        return Ok(None);
    };
    let refs: Vec<(u32, opennotebook_session::SlideRef)> = session
        .slides
        .iter()
        .map(|s| (s.ordinal, s.slide_ref.clone()))
        .collect();
    let found = refs
        .iter()
        .find(|(o, _)| *o == ordinal)
        .map(|(_, r)| r.clone());
    if let Ok(mut c) = slide_refs_cache().lock() {
        // Bounded for the same reason the render cache is: a daemon's map with
        // no ceiling leaks. Refs are small, so this holds more of them.
        while c.len() >= SLIDE_CACHE_MAX * 4 {
            let Some(oldest) = c
                .iter()
                .min_by_key(|(_, (at, _))| *at)
                .map(|(k, _)| k.clone())
            else {
                break;
            };
            c.remove(&oldest);
        }
        c.insert(sid.to_string(), (std::time::Instant::now(), refs));
    }
    Ok(Some(found))
}

/// One rendered slide, kept so the next request for it costs nothing.
///
/// # Why this exists
///
/// Measured on this box: opening the gallery issues **35** requests to
/// `/api/session/slide`, at 426 to 530 KB each — roughly 15 MB — and every one
/// of them used to cost a full `load()` of the session document from the store
/// (megabytes, to read one `slide_ref`), a fetch of the render, and a
/// `strip_scripts` pass over ~450 KB. None of it was cached at
/// any layer: the route sent no `ETag` and no `Cache-Control`, so a refresh paid
/// the whole bill again.
///
/// The PNG branch of `serve_slide` already said the important thing — "a slide
/// is immutable once rendered" — and set `max-age` on that basis. It was simply
/// never applied to the HTML branch, which is the one every deck in this product
/// actually uses.
///
/// Two levels, because they solve different halves:
///
/// * the `ETag` and `Cache-Control` let the BROWSER skip the request entirely,
///   which is what makes a second visit fast;
/// * this memo lets the SERVER skip the store and the deck when the browser
///   does ask — a cold reload, a second viewer, a different tab.
struct CachedSlide {
    etag: String,
    content_type: &'static str,
    body: Vec<u8>,
    at: std::time::Instant,
}

/// How long a cached render is trusted.
///
/// Not forever, because a sid is reusable: Retry rebuilds a failed session under
/// the SAME sid, so a slide that was cached from the previous attempt would be
/// served for the new one. An hour of browser `max-age` has the same exposure
/// and is the number the PNG branch already chose; this bounds the server's own
/// copy more tightly so a retry is visible within a minute even to a client that
/// never revalidates.
const SLIDE_TTL: std::time::Duration = std::time::Duration::from_secs(60);

/// How many renders to hold. A daemon's map with no ceiling is a slow leak: at
/// ~450 KB a render, 64 is about 29 MB, which is a gallery's worth and bounded.
const SLIDE_CACHE_MAX: usize = 64;

fn slide_cache() -> &'static std::sync::Mutex<std::collections::HashMap<String, CachedSlide>> {
    static C: std::sync::OnceLock<
        std::sync::Mutex<std::collections::HashMap<String, CachedSlide>>,
    > = std::sync::OnceLock::new();
    C.get_or_init(Default::default)
}

/// FNV-1a over the body. Not a security hash — an ETag only has to change when
/// the bytes do.
fn etag_for(bytes: &[u8]) -> String {
    let h = bytes.iter().fold(1469598103934665603u64, |a, b| {
        (a ^ *b as u64).wrapping_mul(1099511628211)
    });
    format!("\"{h:016x}\"")
}

/// The cached render for `key`, if it is still young enough.
fn slide_cached(key: &str) -> Option<(String, &'static str, Vec<u8>)> {
    let mut c = slide_cache().lock().ok()?;
    let hit = c.get(key)?;
    if hit.at.elapsed() > SLIDE_TTL {
        c.remove(key);
        return None;
    }
    Some((hit.etag.clone(), hit.content_type, hit.body.clone()))
}

fn slide_store(key: String, etag: String, content_type: &'static str, body: &[u8]) {
    let Ok(mut c) = slide_cache().lock() else {
        return;
    };
    // Evict the oldest rather than clearing: a gallery scroll would otherwise
    // throw away the covers it is still showing.
    while c.len() >= SLIDE_CACHE_MAX {
        let Some(oldest) = c.iter().min_by_key(|(_, v)| v.at).map(|(k, _)| k.clone()) else {
            break;
        };
        c.remove(&oldest);
    }
    c.insert(
        key,
        CachedSlide {
            etag,
            content_type,
            body: body.to_vec(),
            at: std::time::Instant::now(),
        },
    );
}

/// The response for a render we have, honouring `If-None-Match`.
///
/// A 304 carries no body, which is the whole point: a revalidated cover costs a
/// few hundred bytes instead of 450 KB.
fn slide_response(
    headers: &HeaderMap,
    etag: &str,
    content_type: &'static str,
    body: Vec<u8>,
) -> Response {
    let fresh = headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.split(',').any(|t| t.trim() == etag));
    if fresh {
        return (
            StatusCode::NOT_MODIFIED,
            [
                (header::ETAG, etag),
                (header::CACHE_CONTROL, "private, max-age=3600"),
            ],
        )
            .into_response();
    }
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, content_type),
            (header::ETAG, etag),
            (header::CACHE_CONTROL, "private, max-age=3600"),
        ],
        body,
    )
        .into_response()
}

#[derive(serde::Deserialize)]
struct StyleQuery {
    style: String,
}

/// A sample slide in a style, as a whole HTML document. What the picker's
/// thumbnails are rendered from (`scripts/style-thumbnails.py`), so a
/// thumbnail is always this build's own kit.
async fn serve_style_sample(Query(q): Query<StyleQuery>) -> Response {
    match opennotebook_build::kits::kit(&q.style) {
        Some(k) => (
            [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
            opennotebook_build::kits::sample(k),
        )
            .into_response(),
        None => (StatusCode::NOT_FOUND, format!("no style `{}`", q.style)).into_response(),
    }
}

async fn serve_slide(headers: HeaderMap, Query(q): Query<SlideQuery>) -> Response {
    // Answered before the store and the deck are touched at all. See
    // `CachedSlide` for what that saves.
    let key = format!("{}|{}|{}", q.session, q.ordinal, want_thumb(&q));
    if let Some((etag, ct, body)) = slide_cached(&key) {
        return slide_response(&headers, &etag, ct, body);
    }

    let slide_ref = match slide_ref_for(&q.session, q.ordinal).await {
        Ok(Some(Some(r))) => r,
        Ok(Some(None)) => {
            return if want_thumb(&q) {
                thumb_placeholder("no slide")
            } else {
                not_found("no slide at that ordinal")
            };
        }
        Ok(None) => return not_found("no such session"),
        Err(e) => return upstream(e),
    };

    match session_impl::slide_render(&slide_ref).await {
        // Non-empty is the liveness check §3 specifies and the ONLY thing it
        // catches. A slide with an overflowing title and a literal SUBHEAD
        // placeholder is 131,541 characters of well-formed HTML and passes
        // this. Quality rests on the character budgets applied upstream.
        Ok(session_impl::Render::Html(html)) if !html.is_empty() => {
            // A thumbnail is a picture of a slide, not a running one. The frames
            // are sandboxed without `allow-scripts`, so every script in every
            // slide was being refused by the browser and logged — a dozen
            // console errors for a gallery of five sessions, none of them
            // actionable. Granting `allow-scripts` beside `allow-same-origin`
            // would un-sandbox it entirely, which is the wrong direction for
            // markup this service did not write. Removing the scripts instead
            // gives the same picture with nothing left to block.
            let html = if want_thumb(&q) {
                strip_scripts(&html)
            } else {
                html
            };
            let etag = etag_for(html.as_bytes());
            slide_store(
                key,
                etag.clone(),
                "text/html; charset=utf-8",
                html.as_bytes(),
            );
            slide_response(
                &headers,
                &etag,
                "text/html; charset=utf-8",
                html.into_bytes(),
            )
        }
        // A slide is immutable once rendered; a re-render makes a new deck
        // rather than replacing these bytes. That was already true here and is
        // now true of the html branch too.
        Ok(session_impl::Render::Png(bytes)) if !bytes.is_empty() => {
            let etag = etag_for(&bytes);
            slide_store(key, etag.clone(), "image/png", &bytes);
            slide_response(&headers, &etag, "image/png", bytes)
        }
        Ok(_) if want_thumb(&q) => thumb_placeholder("empty render"),
        Ok(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            "the deck has an empty render for a slide the session records",
        )
            .into_response(),
        // A session can be `ready` and still have no deck behind it: the `e2e`
        // fixture records collection "c", presentation "p", slide "1", and no
        // such deck exists on disk. So
        // `state == "ready"` is not a promise that the slides resolve, and a
        // gallery that assumes it shows a wall of error pages.
        Err(e) if want_thumb(&q) => {
            let _ = e;
            thumb_placeholder("slides unavailable")
        }
        Err(e) => upstream(e),
    }
}

/// Drop every `<script>` element, contents and all.
///
/// A state machine rather than a regex or a parser: the only structure that
/// matters is where a script starts and where its matching close tag is, and an
/// HTML parser to answer that is a lot of dependency for one question. Keeps
/// everything else byte for byte, so the slide still looks like itself.
fn strip_scripts(html: &str) -> String {
    let lower = html.to_ascii_lowercase();
    let mut out = String::with_capacity(html.len());
    let mut i = 0usize;
    while let Some(rel) = lower[i..].find("<script") {
        let start = i + rel;
        out.push_str(&html[i..start]);
        match lower[start..].find("</script>") {
            Some(end_rel) => i = start + end_rel + "</script>".len(),
            // Unclosed: drop the rest rather than emit a half tag that the
            // browser will repair into something unpredictable.
            None => return out,
        }
    }
    out.push_str(&html[i..]);
    out
}

fn want_thumb(q: &SlideQuery) -> bool {
    !q.thumb.is_empty() && q.thumb != "0" && q.thumb != "false"
}

/// A slide-shaped "nothing here", for a gallery tile that cannot render.
fn thumb_placeholder(why: &str) -> Response {
    let html = format!(
        "<!doctype html><meta charset=utf-8>\
         <div style=\"position:fixed;inset:0;display:grid;place-items:center;\
         background:#0d1219;color:#8b98a9;font:34px ui-sans-serif,system-ui,sans-serif\">{}</div>",
        html_escape_min(why)
    );
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        html,
    )
        .into_response()
}

fn html_escape_min(s: &str) -> String {
    s.chars()
        .map(|c| match c {
            '<' => "&lt;".to_string(),
            '>' => "&gt;".to_string(),
            '&' => "&amp;".to_string(),
            c => c.to_string(),
        })
        .collect()
}

// ── helpers ──────────────────────────────────────────────────────────────────

async fn load(sid: &str) -> anyhow::Result<Option<Session>> {
    Ok(SessionStore::open()?.get(sid).await?)
}

#[derive(Deserialize)]
struct CoverQuery {
    collection: String,
    #[serde(default)]
    v: String,
    /// `light` for the light theme; anything else is the studio's dark.
    #[serde(default)]
    theme: String,
}

/// `GET /api/session/cover?collection=<cid>&v=<cover_version>&theme=dark|light`:
/// the cover the collection has now, designed or drawn from its title, in the
/// viewer's theme. Cached for good when `v` names the version drawn, since a
/// version is never drawn two ways in one theme and the theme is part of the
/// address; any other `v` gets the current cover, uncached, so a stale link
/// still shows the right one.
async fn serve_cover(Query(q): Query<CoverQuery>) -> Response {
    let unknown = || {
        (
            StatusCode::NOT_FOUND,
            axum::Json(serde_json::json!({ "error": "no such collection" })),
        )
            .into_response()
    };
    let Ok(cid) = create::safe_sid(&q.collection) else {
        return unknown();
    };
    let store = match collection::open_store().await {
        Ok(s) => s,
        Err(e) => return upstream(e),
    };
    let g = match collection::gather_one(&store, cid).await {
        Ok(g) => g,
        Err(e) => return upstream(e),
    };
    let Some(summary) = collection::synthesize(&g)
        .into_iter()
        .find(|c| c.cid == cid)
    else {
        return unknown();
    };
    let html = cover::render(
        cid,
        &collection::cover_spec(cid, &summary, &g),
        cover::render::Theme::parse(&q.theme),
    );
    let cache = if q.v == summary.cover_version {
        "public, max-age=31536000, immutable"
    } else {
        "no-cache"
    };
    (
        [
            (header::CONTENT_TYPE, "text/html; charset=utf-8"),
            (header::CACHE_CONTROL, cache),
        ],
        html,
    )
        .into_response()
}

fn not_found(why: &str) -> Response {
    (StatusCode::NOT_FOUND, why.to_string()).into_response()
}

fn upstream(e: impl std::fmt::Display) -> Response {
    (StatusCode::BAD_GATEWAY, format!("upstream: {e}")).into_response()
}

/// True when `path` stays inside `root` after normalisation.
///
/// Unused by the routes above, which read the path out of the store rather than
/// from the caller, and kept because the store is written by a job whose inputs
/// include user-supplied filenames.
#[allow(dead_code)]
fn contained(root: &Path, path: &Path) -> bool {
    let mut depth = 0i32;
    for c in path.components() {
        match c {
            Component::ParentDir => depth -= 1,
            Component::Normal(_) => depth += 1,
            Component::CurDir => {}
            Component::RootDir | Component::Prefix(_) => return false,
        }
        if depth < 0 {
            return false;
        }
    }
    let _ = root;
    true
}

#[cfg(test)]
mod slide_cache_tests {
    use super::*;

    /// An ETag has to change when the bytes do, and only then — that is the
    /// whole contract a revalidating browser relies on.
    #[test]
    fn the_etag_tracks_the_body() {
        assert_eq!(etag_for(b"same"), etag_for(b"same"));
        assert_ne!(etag_for(b"same"), etag_for(b"different"));
        // Quoted, as an entity-tag must be.
        assert!(etag_for(b"x").starts_with('"') && etag_for(b"x").ends_with('"'));
    }

    /// A matching `If-None-Match` answers 304 with no body. This is the saving:
    /// a revalidated cover costs a few hundred bytes instead of ~450 KB.
    #[test]
    fn a_matching_etag_is_answered_304() {
        let etag = etag_for(b"body");
        let mut h = HeaderMap::new();
        h.insert(header::IF_NONE_MATCH, etag.parse().unwrap());
        let r = slide_response(&h, &etag, "text/html; charset=utf-8", b"body".to_vec());
        assert_eq!(r.status(), StatusCode::NOT_MODIFIED);
    }

    /// A stale or absent validator gets the bytes, with the headers that let the
    /// next request revalidate.
    #[test]
    fn a_stale_or_missing_etag_gets_the_body() {
        let etag = etag_for(b"body");
        for h in [HeaderMap::new(), {
            let mut h = HeaderMap::new();
            h.insert(header::IF_NONE_MATCH, "\"old\"".parse().unwrap());
            h
        }] {
            let r = slide_response(&h, &etag, "text/html; charset=utf-8", b"body".to_vec());
            assert_eq!(r.status(), StatusCode::OK);
            assert_eq!(r.headers().get(header::ETAG).unwrap(), etag.as_str());
            assert!(r.headers().get(header::CACHE_CONTROL).is_some());
        }
    }

    /// A browser sends several tags at once after a few visits; any of them
    /// matching is a hit.
    #[test]
    fn one_of_several_offered_tags_matches() {
        let etag = etag_for(b"body");
        let mut h = HeaderMap::new();
        h.insert(
            header::IF_NONE_MATCH,
            format!("\"other\", {etag}").parse().unwrap(),
        );
        assert_eq!(
            slide_response(&h, &etag, "image/png", b"body".to_vec()).status(),
            StatusCode::NOT_MODIFIED
        );
    }

    /// The map is bounded. A daemon that caches without a ceiling leaks, slowly
    /// and invisibly, until the box notices.
    #[test]
    fn the_cache_never_grows_past_its_ceiling() {
        for i in 0..(SLIDE_CACHE_MAX + 20) {
            slide_store(format!("k{i}"), etag_for(b"x"), "image/png", b"x");
        }
        assert!(slide_cache().lock().unwrap().len() <= SLIDE_CACHE_MAX);
    }

    /// Storing then reading back is the path a second viewer takes, and it must
    /// not touch the store or the deck to do it.
    #[test]
    fn a_stored_render_reads_back() {
        let key = "roundtrip|0|true".to_string();
        let etag = etag_for(b"hello");
        slide_store(
            key.clone(),
            etag.clone(),
            "text/html; charset=utf-8",
            b"hello",
        );
        let (got_etag, ct, body) = slide_cached(&key).expect("just stored");
        assert_eq!(got_etag, etag);
        assert_eq!(ct, "text/html; charset=utf-8");
        assert_eq!(body, b"hello");
    }
}

#[cfg(test)]
mod thumb_tests {
    #[test]
    fn scripts_do_not_survive_a_thumbnail() {
        let html = "<p>before</p><script>alert(1)</script><p>after</p>";
        let out = super::strip_scripts(html);
        assert_eq!(out, "<p>before</p><p>after</p>");
        // Case and attributes are not a way past it.
        assert_eq!(super::strip_scripts("<SCRIPT src=x>y</SCRIPT>a"), "a");
        // An unclosed script takes the remainder with it rather than leaving a
        // dangling open tag for the browser to repair.
        assert_eq!(super::strip_scripts("<p>ok</p><script>x"), "<p>ok</p>");
        // Everything else is untouched, byte for byte.
        let keep = "<div class=\"s\"><img src=\"data:image/png;base64,AA\"></div>";
        assert_eq!(super::strip_scripts(keep), keep);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_traversal_escapes_and_is_refused() {
        let root = Path::new("/srv/audio");
        assert!(contained(root, Path::new("a/b.wav")));
        assert!(contained(root, Path::new("a/../b.wav")));
        assert!(!contained(root, Path::new("../etc/passwd")));
        assert!(!contained(root, Path::new("a/../../etc/passwd")));
        assert!(!contained(root, Path::new("/etc/passwd")));
    }

    #[test]
    fn the_two_byte_routes_sit_under_api() {
        // The whole finding in one assertion: these are `/api/` routes on the
        // one address the server listens on, beside the JSON-RPC. If someone
        // moves them, this fails and they read the module note.
        assert!(AUDIO_ROUTE.starts_with("/api/"));
        assert!(SLIDE_ROUTE.starts_with("/api/"));
    }
}
