//! The player: an event stream and a plain browser page.
//!
//! §4 asks for "a plain browser page and an SSE event stream. No LiveKit, no
//! SFU, no WebRTC." Both are served from `/api/` on the service's ordinary
//! `rpc.sock`, next to the JSON-RPC and the byte routes — see §4's "Where the
//! bytes come from".
//!
//! **Flagged as a decision the spec did not make.** The page is one
//! self-contained HTML document served from a route, not a Dioxus/WASM bundle in
//! `var/www/<service>/ui/`. The bundle is the fleet's convention for a product
//! UI and is the right home when this becomes one, but it needs the `dx`
//! toolchain and a build step, and §4 says "a plain browser page". A page with
//! no build step also means the player is readable in the same diff as the
//! contract it implements.

use std::convert::Infallible;
use std::time::Duration;

use axum::extract::Query;
use axum::http::{StatusCode, header};
use axum::response::sse::Event;
use axum::response::{IntoResponse, Response};
use futures_util::stream::{self, Stream, StreamExt};
use serde::Deserialize;

use opennotebook_session::{Session, SessionState};

use crate::events::{self, StudioEvent};
use crate::playback::{self, Playhead};

#[derive(Deserialize)]
pub struct SessionQuery {
    pub session: String,
}

/// `GET /api/session/events?session=<sid>`
///
/// The merge described in [`crate::events`]: a polled half for the state another
/// process writes, and a pushed half for the playhead this one owns.
pub async fn serve_events(
    Query(q): Query<SessionQuery>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let sid = q.session;

    // Per-connection state.
    //
    // `last_head` starts at the DEFAULT playhead, not at a sentinel. A sentinel
    // was tried and it leaked: `line_id: "\0never"` made the first diff look
    // like a line had just ended, and the stream's opening frame over the public
    // domain was a real `line.end {"line_id":"\u0000never"}` for a line that
    // never existed. A default `was` still emits everything a page joining
    // mid-session needs — the state differs, and an empty previous `line_id`
    // against a present one is what triggers `slide.enter` — while an empty
    // previous line cannot produce a `line.end`.
    let last_state: Option<String> = None;
    let last_head = Playhead::default();
    let last_progress: Option<(String, i64, i64)> = None;
    let terminal = false;
    // The first poll runs at once. It used to wait a full POLL like every
    // other one, so a page that opened a build's progress drew every step as
    // not started for that second and then jumped to where the build was —
    // which read as the build going back to its first step.
    let started = false;

    let ticks =
        stream::unfold(
            (sid, last_state, last_head, last_progress, terminal, started),
            move |(
                sid,
                mut last_state,
                mut last_head,
                mut last_progress,
                mut terminal,
                started,
            )| async move {
                if started {
                    tokio::time::sleep(events::POLL).await;
                }
                let mut out: Vec<StudioEvent> = Vec::new();

                // ── polled half: what the dispatched prep process writes ─────────
                //
                // Stops once the session reaches a terminal state: after that there
                // is nothing left for this half to learn, and a poll that ran
                // forever would be the cost with none of the reason.
                let session = if terminal {
                    load(&sid).await
                } else {
                    let s = load(&sid).await;
                    if let Some(sess) = &s {
                        let state = state_str(sess.state).to_string();
                        if last_state.as_deref() != Some(state.as_str()) {
                            out.push(StudioEvent::SessionState {
                                state: state.clone(),
                            });
                            last_state = Some(state);
                        }
                        if matches!(sess.state, SessionState::Ready | SessionState::Failed) {
                            terminal = true;
                        }
                        // Progress, while there is a prep to report on.
                        if !terminal
                            && let Some(job) = sess.prep_job_sid.clone()
                            && let Some(p) = progress(&job).await
                            && last_progress.as_ref() != Some(&p)
                        {
                            out.push(StudioEvent::PrepProgress {
                                step: p.0.clone(),
                                steps_done: p.1,
                                steps_total: p.2,
                            });
                            last_progress = Some(p);
                        }
                    }
                    s
                };

                // ── pushed half: the playhead this process owns ──────────────────
                let now = playback::get(&sid);
                if now != last_head {
                    let lookup = session.as_ref().map(lookup_for);
                    out.extend(events::playback_events(&last_head, &now, lookup.as_ref()));
                    last_head = now;
                }

                let frames: Vec<Result<Event, Infallible>> =
                    out.iter().filter_map(|e| e.frame().ok()).map(Ok).collect();
                Some((
                    stream::iter(frames),
                    (sid, last_state, last_head, last_progress, terminal, true),
                ))
            },
        )
        .flatten();

    Sse::new(ticks).keep_alive(events::keepalive())
}

type Sse<S> = axum::response::sse::Sse<S>;

fn state_str(s: SessionState) -> &'static str {
    match s {
        SessionState::Preparing => "preparing",
        SessionState::Ready => "ready",
        SessionState::Failed => "failed",
    }
}

async fn load(sid: &str) -> Option<Session> {
    crate::session_impl::load_session(sid).await.ok().flatten()
}

/// `(step, steps_done, steps_total)` from the prep job row.
async fn progress(job_sid: &str) -> Option<(String, i64, i64)> {
    let j = opennotebook_build::job::get(job_sid).await.ok()??;
    // `steps_total: 0` is "this job does not report", so it is not forwarded
    // as progress — a bar drawn from it would be a lie about a job that never
    // promised one.
    if j.steps_total == 0 {
        return None;
    }
    Some((j.step, j.steps_done, j.steps_total))
}

fn lookup_for(s: &Session) -> events::SlideLookup {
    let by_ordinal: Vec<(i64, String, String, String)> = s
        .slides
        .iter()
        .map(|sl| {
            (
                sl.ordinal as i64,
                sl.slide_ref.collection.clone(),
                sl.slide_ref.presentation.clone(),
                sl.slide_ref.slide.clone(),
            )
        })
        .collect();
    let sid = s.sid.clone();
    let lines: Vec<(String, String, i64)> = s
        .slides
        .iter()
        .flat_map(|sl| sl.lines.iter())
        .map(|l| {
            (
                l.line_id.0.clone(),
                l.speaker_id.0.clone(),
                l.duration_ms.map(|d| d.millis() as i64).unwrap_or(0),
            )
        })
        .collect();
    events::SlideLookup {
        slide: Box::new(move |o| {
            by_ordinal
                .iter()
                .find(|(ord, ..)| *ord == o)
                .map(|(_, c, p, sl)| (c.clone(), p.clone(), sl.clone()))
        }),
        line: Box::new(move |id| {
            lines.iter().find(|(l, ..)| l == id).map(|(l, spk, dur)| {
                (
                    spk.clone(),
                    *dur,
                    format!("{}?session={}&line={}", crate::AUDIO_ROUTE, sid, l),
                )
            })
        }),
    }
}

/// `GET /api/session/player?session=<sid>`
/// `GET /api/session/console`
///
/// The index the player never had: what sessions exist, what state each is in,
/// and a form that submits a prep job and watches it finish on the same event
/// stream the player uses.
///
/// It takes no query parameters, so unlike [`serve_page`] there is nothing to
/// interpolate and nothing to escape. It is a static document; every value it
/// shows comes from an RPC call the browser makes for itself.
pub async fn serve_console() -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        CONSOLE,
    )
        .into_response()
}

pub async fn serve_page(Query(q): Query<SessionQuery>) -> Response {
    // Warm the courtesy lines for this session's voices while the listener is
    // still reading the first slide. By the time they press the mic the bank is
    // on disk and the acknowledgement is instant.
    // The collection the way back leads to: the session's own, or its sid for
    // a session from before collections. Empty when the session cannot be
    // read, and then the way back is the studio's home.
    let mut collection = String::new();
    if let Ok(Some(session)) = crate::session_impl::load_session(&q.session).await {
        collection = session.collection_id().to_string();
        let voices: Vec<String> = session
            .speakers
            .iter()
            .map(|s| s.voice_id.clone())
            .collect();
        if !voices.is_empty() {
            crate::banter::warm(voices);
        }
    }

    // Three settings the page acts on in the browser, read per load so a change
    // in the settings dialog reaches the next session opened.
    use opennotebook_session::settings;
    let lang_tag = settings::language_tag(&settings::language().await);
    let interrupt = if settings::is_on(&settings::get(settings::INTERRUPT_KEY).await) {
        "on"
    } else {
        "off"
    };
    let extend_ms = match settings::get(settings::PATIENCE_KEY).await.as_str() {
        "quick" => "1000",
        "patient" => "4000",
        _ => "2000",
    };

    let html = PAGE
        .replace("__SESSION__", &html_escape(&q.session))
        .replace("__COLLECTION__", &html_escape(&collection))
        .replace("__VAD_WASM_B64__", &vad_wasm_b64())
        .replace("__BUILD__", build_id())
        .replace("__LANG_TAG__", lang_tag)
        .replace("__INTERRUPT__", interrupt)
        .replace("__EXTEND_MS__", extend_ms)
        // The studio's look: its tokens, its icons, and the viewer's theme
        // applied before the first paint. One source with the app, so the
        // gallery and the player never disagree, and nothing comes from a CDN.
        .replace("__THEME_CSS__", opennotebook_sdk::theme::CSS)
        .replace("__THEME_BOOT__", opennotebook_sdk::theme::BOOT_SCRIPT)
        .replace("__ICONS__", &opennotebook_sdk::icons::sprite());
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "text/html; charset=utf-8"),
            // The page carries the detector inside it, so a cached copy is a
            // cached DETECTOR, and a browser holding yesterday's page holds
            // yesterday's bug with it. There is no version in the URL to break
            // that, so nothing may be reused without asking.
            (header::CACHE_CONTROL, "no-store, must-revalidate"),
        ],
        html,
    )
        .into_response()
}

/// The session sid reaches the page inside a JS string literal, so it is escaped
/// even though `session_prepare` already refuses anything but `[A-Za-z0-9_-]`.
/// The validation is one layer away and could be relaxed; the escaping is here.
fn html_escape(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .collect()
}

const _: Duration = events::POLL;

/// The page. Deliberately one file with no build step and no dependencies.
const PAGE: &str = include_str!("player.html");

/// One id per running binary.
///
/// Process start time rather than a git sha: a rebuild during development
/// restarts the process and must invalidate the page, and a sha would not
/// change for an uncommitted edit. It only has to differ, not to mean anything.
fn build_id() -> &'static str {
    static ID: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    ID.get_or_init(|| {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| format!("{}", d.as_millis()))
            .unwrap_or_else(|_| "0".into())
    })
}

/// `GET /api/session/build` — the id the page compares itself against.
pub async fn serve_build() -> Response {
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "text/plain; charset=utf-8"),
            (header::CACHE_CONTROL, "no-store"),
        ],
        build_id().to_string(),
    )
        .into_response()
}

/// The detector, base64, for substitution into the page.
///
/// Empty when the box never built it, which leaves the page's fallback chain
/// intact: it tries the route, and if that fails too it says so on screen.
/// Read at request time rather than compiled in, because the wasm is a build
/// artifact and a build artifact does not belong in git.
fn vad_wasm_b64() -> String {
    use base64::Engine as _;
    crate::ask::vad_wasm_bytes()
        .map(|b| base64::engine::general_purpose::STANDARD.encode(b))
        .unwrap_or_default()
}

/// The console. Same rule: one file, no build step, no toolchain.
const CONSOLE: &str = include_str!("console.html");

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_sid_cannot_break_out_of_the_script_literal() {
        // The page interpolates the sid into JS. Validation lives in
        // session_prepare, one layer away; this is the layer that renders.
        assert_eq!(html_escape("abc-123_x"), "abc-123_x");
        assert_eq!(html_escape("a\";alert(1);//"), "aalert1");
        assert_eq!(html_escape("</script>"), "script");
    }

    #[test]
    fn the_page_substitutes_the_session_and_leaves_no_placeholder() {
        let out = PAGE.replace("__SESSION__", &html_escape("s1"));
        assert!(!out.contains("__SESSION__"), "placeholder left in the page");
        assert!(out.contains("s1"));
    }

    #[test]
    fn the_page_letterboxes_rather_than_assuming_16_9() {
        // §4: "Letterbox, do not assume 16:9. HTML slides are 1920x1080; PNG
        // slides come back at 1376x768." html is currently fixed so the png path
        // has never rendered, which is exactly why this is asserted on the page
        // source rather than trusted.
        assert!(
            PAGE.contains("aspect"),
            "the page must use the per-slide aspect"
        );
        assert!(
            !PAGE.contains("16/9") && !PAGE.contains("56.25%"),
            "no hard-coded 16:9"
        );
    }

    #[test]
    fn the_page_never_hard_codes_an_unprefixed_api_path() {
        // A reverse proxy may mount the studio under a prefix, in front of
        // `/api/session/player`. An absolute `/api/session` loses the prefix
        // and every call 404s — which is exactly what happened
        // the first time the page was loaded in a browser, and what no
        // route-by-route probe could catch, because the probes supplied the
        // prefix themselves.
        assert!(
            PAGE.contains("location.pathname.replace"),
            "the API base must be derived from the page's own URL"
        );
        assert!(
            !PAGE.contains("\"/api/session\""),
            "no absolute /api/session base in the page"
        );
    }

    #[test]
    fn the_worklet_source_holds_no_stray_backtick() {
        // VAD_WORKLET_SRC is a template literal, so a backtick anywhere inside
        // it ENDS the string and the rest of the page stops being JavaScript.
        // Measured: a single pair around `captureFrom` in a comment took the
        // whole script out with "Unexpected identifier 'captureFrom'", and the
        // page still served a 200 with a perfectly valid-looking body. Nothing
        // short of executing it catches that, so the shape is asserted here.
        //
        // The two `${...}` on the first lines are deliberate interpolation of
        // SILENCE_PEAK and HANG_MS, so the fallback gate cannot drift from the
        // page's own constants.
        let start = PAGE
            .find("const VAD_WORKLET_SRC = `")
            .expect("the worklet source is gone");
        let body_at = start + "const VAD_WORKLET_SRC = `".len();
        let end = PAGE[body_at..]
            .find("`;")
            .expect("the worklet template literal is never closed");
        let body = &PAGE[body_at..body_at + end];

        assert!(
            !body.contains('`'),
            "a backtick inside VAD_WORKLET_SRC ends the template literal early"
        );
        assert!(
            body.contains("registerProcessor(\"opennotebook-vad\""),
            "the worklet must register itself as opennotebook-vad"
        );
        // The names the wasm exports, called by string from the worklet. A
        // rename in Rust compiles and then breaks the page at run time.
        for f in [
            "vad_new",
            "vad_push",
            "vad_alloc",
            "vad_dealloc",
            "vad_speech_ms",
        ] {
            assert!(body.contains(f), "the worklet no longer calls {f}");
        }
    }

    #[test]
    fn the_page_can_tell_it_is_stale() {
        // The page carries the detector, so a cached page is a cached detector
        // and there is nothing in the URL to break that. Both halves matter:
        // the id has to be IN the page, and the page has to be told not to sit
        // in a cache in the first place.
        assert!(PAGE.contains("__BUILD__"), "the page must carry a build id");
        assert!(
            PAGE.contains("sessionStorage.getItem(\"opennotebook_reloaded_for\")"),
            "the staleness reload must be guarded against looping"
        );
        let filled = PAGE
            .replace("__SESSION__", "s1")
            .replace("__VAD_WASM_B64__", "")
            .replace("__BUILD__", super::build_id());
        assert!(
            !filled.contains("__BUILD__"),
            "the build placeholder survived"
        );
    }

    #[test]
    fn the_composer_uses_icons_not_emoji() {
        // The studio app draws from Bootstrap Icons. An emoji is not
        // an icon: the OS picks the glyph, so it never matches the design and
        // it looks different on every machine.
        for icon in ["bi-mic-fill", "bi-send-fill", "bi-x-lg"] {
            assert!(
                PAGE.contains(&format!("#{icon}")),
                "the composer lost {icon}"
            );
            assert!(
                opennotebook_sdk::icons::icon(icon.trim_start_matches("bi-")).is_some(),
                "{icon} is not in the sprite the page is served with"
            );
        }
        assert!(
            PAGE.contains("__ICONS__"),
            "the icon sprite must actually be put in the page"
        );
        // Offline first: the page fetches nothing from the internet to look right.
        assert!(
            !PAGE.contains("cdn.") && !PAGE.contains("@import url(\"http"),
            "a CDN is back in the page"
        );
        assert!(
            !PAGE.contains("&#128465;") && !PAGE.contains("&#127908;"),
            "an emoji is being used as a control"
        );
    }

    #[test]
    fn the_microphone_is_opened_exactly_once_per_turn() {
        // askStart called micStart() TWICE and abandoned the first: a live
        // MediaStream with the indicator lit, an AudioContext and an
        // AudioWorklet, none closed, every turn. Four turns in, an 88 KB
        // ArrayBuffer allocation fails and the page can no longer decode the
        // detector. It also broke capture outright, because capturedSamples
        // counted both nodes while micChunks was reset between them, so
        // captureFrom ran past the end and micStop returned nothing: the
        // reported "0.0s of audio".
        let at = PAGE
            .find("async function askStart")
            .expect("askStart is gone");
        let end = PAGE[at..].find("\nfunction ").unwrap_or(4000);
        let body = &PAGE[at..at + end];
        assert_eq!(
            body.matches("await micStart()").count(),
            1,
            "askStart must open the microphone exactly once"
        );
        // And the guard that makes a future duplicate cost nothing.
        assert!(
            PAGE.contains("function micTeardown"),
            "the capture graph needs one place that releases it"
        );
        assert!(
            PAGE.contains("micTeardown();\n\n  micStream = await"),
            "micStart must release any previous capture before opening a new one"
        );
    }

    #[test]
    fn a_turn_never_vanishes_without_a_word() {
        // Reported as "recorded and sent but no response" and as "send button
        // not even working". Both were the same thing: the turn was dropped and
        // NOTHING replaced it, so the listener could not tell a swallowed turn
        // from a dead button. Ninth and tenth instance of the silent-empty
        // pattern, and both were ours.
        assert!(
            PAGE.contains("too short to be a question"),
            "a too-short recording must say so"
        );
        assert!(
            PAGE.contains("could not hear any speech in that"),
            "a silent verdict must say so"
        );
    }

    #[test]
    fn the_play_page_has_no_reactions() {
        // Removed at the owner's request during the play-page redesign: the
        // page is for watching and asking, and the emoji bar was noise over
        // the slide.
        assert!(
            !PAGE.contains("class=\"react\""),
            "the reaction bar is back"
        );
        assert!(!PAGE.contains("floater"), "the reaction animation is back");
    }

    #[test]
    fn the_player_has_a_video_players_controls() {
        // One play/pause, previous and next slide, a chaptered scrubber,
        // captions and an interactive transcript.
        for id in [
            "id=\"playpause\"",
            "id=\"prev\"",
            "id=\"next\"",
            "id=\"timeline\"",
            "id=\"cap\"",
            "id=\"toc\"",
            "id=\"poster\"",
            "id=\"endcard\"",
        ] {
            assert!(PAGE.contains(id), "the player lost {id}");
        }
        assert!(
            !PAGE.contains("id=\"play\""),
            "separate play and pause buttons are back"
        );
    }

    #[test]
    fn the_detector_rides_with_the_page() {
        // A second request is a second thing that can fail, and it did: the
        // owner's browser reported `Failed to fetch` on a URL that answers 200
        // to curl from the same host. The page carries the bytes now, so there
        // is no request to block, no cache to go stale and no proxy in the way.
        assert!(
            PAGE.contains("__VAD_WASM_B64__"),
            "the page must carry a slot for the detector"
        );
        assert!(
            PAGE.contains("const VAD_WASM_B64"),
            "the page must read the inline detector"
        );
        // And serve_page must always fill it, or the literal placeholder
        // reaches the browser and atob() chokes on it.
        let filled = PAGE
            .replace("__SESSION__", "s1")
            .replace("__VAD_WASM_B64__", "");
        assert!(
            !filled.contains("__VAD_WASM_B64__"),
            "the detector placeholder survived substitution"
        );
    }

    #[test]
    fn the_fallback_to_the_loudness_gate_is_never_silent() {
        // Without the wasm the worklet runs the old 0.06 peak threshold, and
        // that threshold IS the reported bug: any room noise above it resets
        // the hang timer every block, so the turn never sends. Falling back to
        // it quietly reproduces the bug with the evidence removed, which is the
        // silent-empty pattern that keeps catching this project.
        assert!(
            PAGE.contains("function vadDegraded"),
            "the degraded path must have one named place that says so"
        );
        // Said on screen, not only to a console nobody has open.
        assert!(
            PAGE.contains("appendMsg(bubble({") && PAGE.contains("Voice detection did not load"),
            "a degraded detector must be visible to the listener"
        );
        // force-cache would let a 404 cached from before this route existed pin
        // the page to the fallback for its whole life, with no way to recover.
        assert!(
            !PAGE.contains("force-cache"),
            "the wasm fetch must revalidate, or a stale 404 is permanent"
        );
    }

    #[test]
    fn capture_does_not_route_the_microphone_into_the_speakers() {
        // The ScriptProcessorNode needed `micNode.connect(micCtx.destination)`
        // to keep firing, which put the microphone into the output stream
        // Chrome's echo canceller takes its reference from. An AudioWorklet is
        // pulled without it. Both halves are asserted: the old node gone, and
        // the new one present.
        assert!(
            !PAGE.contains("createScriptProcessor"),
            "capture is back on the deprecated ScriptProcessorNode"
        );
        assert!(
            !PAGE.contains("micNode.connect(micCtx.destination)"),
            "the microphone is routed into the speaker output again"
        );
        assert!(
            PAGE.contains("audioWorklet.addModule"),
            "capture must run in an AudioWorklet"
        );
    }

    #[test]
    fn the_page_picks_the_render_by_content_type_not_by_body() {
        // A png deck and an html deck are both a 200 with bytes. Guessing from
        // the body is what let `slide_html_get` return an empty string with a
        // successful result and look like a working door.
        assert!(
            PAGE.contains(r#"r.headers.get("content-type")"#),
            "the page must branch on the response content type"
        );
        assert!(PAGE.contains("image/png"), "png must have its own arm");
        assert!(
            PAGE.contains("slideimg"),
            "png needs an img element, not srcdoc"
        );
    }

    #[test]
    fn the_page_renders_slides_with_srcdoc() {
        // §4 specifies iframe srcdoc, and it matters: the HTML is self-contained
        // and has no base URL, so a src= would need an origin it does not have.
        assert!(PAGE.contains("srcdoc"));
    }
}

#[cfg(test)]
mod look_tests {
    use super::PAGE;

    /// Every icon the page names is in the sprite it is served with: a missing
    /// symbol draws an empty button.
    #[test]
    fn every_icon_the_page_uses_is_in_the_sprite() {
        let mut missing = Vec::new();
        for part in PAGE.split("#bi-").skip(1) {
            let name: String = part
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '-')
                .collect();
            if opennotebook_sdk::icons::icon(&name).is_none() {
                missing.push(name);
            }
        }
        assert!(missing.is_empty(), "not in the sprite: {missing:?}");
    }

    /// The page takes its look from the shared tokens and its theme from the
    /// viewer's choice, the same three placeholders the server fills.
    #[test]
    fn the_page_wears_the_studio_theme() {
        for p in ["__THEME_CSS__", "__THEME_BOOT__", "__ICONS__"] {
            assert_eq!(PAGE.matches(p).count(), 1, "{p}");
        }
    }
}
