//! Sources in: the collection page's routes for adding them, and the staging
//! directory they land in.
//!
//! Phase 1's `session_prepare` takes a `resource_dir`, a path on this box. That
//! is fine for a prep job the server spawns and useless to a browser, which has
//! no filesystem here. These routes close that gap: a person pastes text or
//! links, the studio stages them as files under the collection's cid, and
//! `session_prepare` is handed that staging directory like any other.
//!
//! Every source is written by [`stage`], which never makes a directory and
//! never writes over another source. The directory is made when a call that
//! adds a source starts; a collection deleted while a page loads or a file
//! converts then stays deleted, its late write failing for want of a directory.
//!
//! Nothing here decides anything about the deck. Slide count, voices and the
//! rest are chosen for the user rather than asked about — the whole point of
//! this flow is that a topic and some sources is all anybody should have to
//! supply.

use std::path::PathBuf;

use axum::Json;
use axum::extract::Query;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use opennotebook_convert::{InputKind, to_markdown};
use opennotebook_sdk::UPLOAD_KINDS;
use serde::{Deserialize, Serialize};

/// Where staged sources live until a prep job reads them.
///
/// One directory per session id, because ingest names the memory collection
/// after the directory it reads. A shared staging directory would give every
/// session the same collection name.
pub(crate) fn staging_root() -> PathBuf {
    opennotebook_session::paths::data_dir().join("staging")
}

pub(crate) fn staging_dir(sid: &str) -> PathBuf {
    staging_root().join(sid)
}

/// A session id or a collection id: a directory name, and a memory
/// collection name. It is never allowed to be anything that can leave the
/// directory it names.
pub(crate) fn safe_sid(sid: &str) -> Result<&str, String> {
    let ok = !sid.is_empty()
        && sid.len() <= 64
        && sid
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if ok {
        Ok(sid)
    } else {
        Err(format!(
            "`{sid}` is not a usable session or collection id: 1 to 64 letters, digits, `-` or `_`"
        ))
    }
}

#[derive(Deserialize)]
pub struct SidQuery {
    pub session: String,
}

// ── typed text ───────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct TextSource {
    pub name: String,
    pub text: String,
}

#[derive(Serialize)]
pub struct StagedSource {
    pub name: String,
    pub chars: usize,
    pub ok: bool,
    pub error: String,
}

/// `POST /api/session/source/text?session=<cid>` — stage something typed.
pub async fn stage_text(Query(q): Query<SidQuery>, Json(src): Json<TextSource>) -> Response {
    let sid = match safe_sid(&q.session) {
        Ok(s) => s,
        Err(e) => return (StatusCode::BAD_REQUEST, e).into_response(),
    };
    let dir = staging_dir(sid);
    if let Err(e) = std::fs::create_dir_all(&dir) {
        return (StatusCode::INTERNAL_SERVER_ERROR, format!("staging: {e}")).into_response();
    }
    match stage(&dir, &slug(&src.name), &src.text) {
        Ok(name) => {
            crate::collection::sources_changed(sid).await;
            Json(StagedSource {
                name,
                chars: src.text.chars().count(),
                ok: true,
                error: String::new(),
            })
            .into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, format!("write: {e}")).into_response(),
    }
}

/// Write one source into a staging directory as `<stem>.md`, or `<stem>_2.md`
/// and on when that is taken, and return the name it got.
///
/// The one way a source is written. `.md` because ingest passes text through
/// verbatim for that extension and runs no parser over it. `create_new`, so
/// two sources with the same name are two files and never one written over
/// the other, even when two calls race for the same name. The directory is
/// NOT made here: see the module note.
pub(crate) fn stage(dir: &std::path::Path, stem: &str, body: &str) -> std::io::Result<String> {
    use std::io::Write as _;
    for n in 1..=MAX_SAME_NAME {
        let name = match n {
            1 => format!("{stem}.md"),
            n => format!("{stem}_{n}.md"),
        };
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(dir.join(&name))
        {
            Ok(mut f) => {
                f.write_all(body.as_bytes())?;
                return Ok(name);
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
    Err(std::io::Error::new(
        std::io::ErrorKind::AlreadyExists,
        format!("{MAX_SAME_NAME} sources are already named `{stem}`"),
    ))
}

/// Past this many sources of one name, the next is refused rather than written
/// over one of them.
const MAX_SAME_NAME: usize = 999;

/// A name or title as one line: control characters, newlines among them, are
/// dropped. A newline in a heading would end it and start the body, and a
/// name reaches the `# ` and `File:` lines of the file it is staged as.
pub(crate) fn one_line(s: &str) -> String {
    s.chars()
        .filter(|c| !c.is_control())
        .collect::<String>()
        .trim()
        .to_string()
}

// ── uploaded files ───────────────────────────────────────────────────────────

/// The largest file a person may hand over. A long report as a PDF is a few
/// megabytes; past this it is a scanned book or a slide deck full of photos,
/// and neither turns into text worth narrating. The RPC carries the same file
/// in base64, a third larger, still well inside the server's 64 MiB body
/// limit.
/// Links read in one add, on either path in. More than this in one call is a
/// crawl, not a handful of sources; the page and the RPC say the same number.
pub(crate) const MAX_URLS: usize = 8;

pub(crate) const MAX_UPLOAD_BYTES: usize = 25 * 1024 * 1024;

/// A converted document with fewer visible characters than this is a scan
/// with no text layer. The same floor ingest applies, for the same reason.
const MIN_CONVERTED_CHARS: usize = 80;

/// Why a file was not staged. The HTTP status follows the kind; the message
/// is written for the person who picked the file.
#[derive(Debug, PartialEq)]
pub(crate) enum UploadError {
    /// Over [`MAX_UPLOAD_BYTES`]: 413.
    TooLarge(String),
    /// A type the studio cannot read, or a file it could not read text out
    /// of: 400.
    Unreadable(String),
    /// The disk refused: 500.
    Io(String),
}

impl UploadError {
    pub(crate) fn message(&self) -> &str {
        match self {
            Self::TooLarge(m) | Self::Unreadable(m) | Self::Io(m) => m,
        }
    }

    fn status(&self) -> StatusCode {
        match self {
            Self::TooLarge(_) => StatusCode::PAYLOAD_TOO_LARGE,
            Self::Unreadable(_) => StatusCode::BAD_REQUEST,
            Self::Io(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }
}

/// A file that became a source.
#[derive(Debug)]
pub(crate) struct StagedFile {
    /// The staged file's name, as `source_list` names it.
    pub name: String,
    /// The file's own name without its extension: the source's heading.
    pub title: String,
    /// Readable characters kept.
    pub chars: usize,
}

pub(crate) fn too_large() -> UploadError {
    UploadError::TooLarge(format!(
        "That file is over {} MB. Upload a smaller one, or split it.",
        MAX_UPLOAD_BYTES / (1024 * 1024)
    ))
}

/// The base name a browser or an agent sent, with any directory taken off:
/// old browsers send `C:\fakepath\x.pdf`, and a name must never pick where
/// it is written. Only its stem and extension are used from here on, as one
/// line.
fn base_name(name: &str) -> String {
    one_line(name.rsplit(['/', '\\']).next().unwrap_or(name))
}

/// Read an uploaded file into Markdown and stage it as one source, into a
/// directory the caller made.
///
/// Staged the way a pasted note with a title is: one `.md` file, the title as
/// its `# ` heading, which `sources_impl::describe` and the build read it by.
/// Under the heading a `File:` line names what was uploaded. Not `Source:`:
/// that line is a page's address, and the page draws a site icon from it.
///
/// PDF, Word, PowerPoint and Excel go through `opennotebook_convert`, the
/// converter ingest uses, so what is staged is what the build would have read
/// out of the original. Markdown, text and CSV are already text and are kept
/// as they are, invalid UTF-8 replaced rather than refused: a stray byte in a
/// CSV export should not cost the whole file. Staged as `.md` whatever came
/// in, because ingest passes `.md` through untouched and would otherwise run
/// a parser over text it cannot parse.
pub(crate) fn stage_file(
    dir: &std::path::Path,
    name: &str,
    bytes: &[u8],
) -> Result<StagedFile, UploadError> {
    if bytes.len() > MAX_UPLOAD_BYTES {
        return Err(too_large());
    }
    let name = base_name(name);
    let name = name.as_str();
    let path = std::path::Path::new(name);
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let title = path
        .file_stem()
        .and_then(|s| s.to_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("Uploaded file")
        .to_string();

    let text = match ext.as_str() {
        "md" | "markdown" | "txt" | "csv" => {
            let text = String::from_utf8_lossy(bytes).into_owned();
            if text.trim().is_empty() {
                return Err(UploadError::Unreadable(format!("{name} is empty.")));
            }
            text
        }
        _ => {
            let Some(kind) = InputKind::from_extension(&ext) else {
                let what = if ext.is_empty() {
                    "files without an extension".to_string()
                } else {
                    format!(".{ext} files")
                };
                return Err(UploadError::Unreadable(format!(
                    "Can't read {what} yet. Upload {UPLOAD_KINDS}."
                )));
            };
            let md = to_markdown(bytes, kind)
                .map_err(|e| UploadError::Unreadable(format!("Couldn't read {name}: {e}")))?;
            if md.chars().filter(|c| !c.is_whitespace()).count() < MIN_CONVERTED_CHARS {
                return Err(UploadError::Unreadable(format!(
                    "{name} has no text to read. A scanned document is pictures of \
                     pages; upload one with a text layer."
                )));
            }
            md
        }
    };

    let text = text.trim();
    let body = format!("# {title}\n\nFile: {name}\n\n{text}\n");
    let staged = stage(dir, &slug(&title), &body)
        .map_err(|e| UploadError::Io(format!("could not keep it: {e}")))?;
    Ok(StagedFile {
        name: staged,
        title,
        chars: text.chars().count(),
    })
}

/// Both optional to the extractor, so a request missing one is answered in
/// [`StagedSource`] like any other refusal, not with axum's plain-text 400.
#[derive(Deserialize)]
pub struct UploadQuery {
    #[serde(default)]
    pub session: String,
    #[serde(default)]
    pub name: String,
}

/// `POST /api/session/source/upload?session=<cid>&name=<file name>` — stage a
/// file. The body is the file's bytes, whatever its `Content-Type` says.
///
/// Answers in [`StagedSource`], the shape `/source/text` answers in, failures
/// included: a refused file comes back as `ok: false` with the reason in
/// `error`, under the status that fits it (413 too large, 400 unreadable), so
/// a page reads one shape either way.
///
/// The body is read here against [`MAX_UPLOAD_BYTES`] rather than through an
/// extractor: an extractor refuses an oversized body with its own plain-text
/// 413 before this code runs, and the person would be told nothing useful.
pub async fn upload_source(
    Query(q): Query<UploadQuery>,
    headers: axum::http::HeaderMap,
    body: axum::body::Body,
) -> Response {
    let refuse = |status: StatusCode, error: String| {
        (
            status,
            Json(StagedSource {
                name: q.name.clone(),
                chars: 0,
                ok: false,
                error,
            }),
        )
            .into_response()
    };
    let sid = match safe_sid(&q.session) {
        Ok(s) => s.to_string(),
        Err(e) => return refuse(StatusCode::BAD_REQUEST, e),
    };
    if q.name.trim().is_empty() {
        return refuse(
            StatusCode::BAD_REQUEST,
            "The file's name is missing: send it as ?name=, extension included.".into(),
        );
    }
    // A declared length over the limit is refused before a byte is read.
    let declared = headers
        .get(axum::http::header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<usize>().ok());
    if declared.is_some_and(|n| n > MAX_UPLOAD_BYTES) {
        let e = too_large();
        return refuse(e.status(), e.message().to_string());
    }
    let bytes = match axum::body::to_bytes(body, MAX_UPLOAD_BYTES).await {
        Ok(b) => b,
        Err(_) => {
            let e = too_large();
            return refuse(e.status(), e.message().to_string());
        }
    };
    let dir = staging_dir(&sid);
    if let Err(e) = std::fs::create_dir_all(&dir) {
        return refuse(StatusCode::INTERNAL_SERVER_ERROR, format!("staging: {e}"));
    }
    // Parsing a large PDF is seconds of CPU; off the async workers.
    let name = q.name.clone();
    let staged = tokio::task::spawn_blocking(move || stage_file(&dir, &name, &bytes))
        .await
        .unwrap_or_else(|e| Err(UploadError::Io(format!("conversion stopped: {e}"))));
    match staged {
        Ok(f) => {
            crate::collection::sources_changed(&sid).await;
            Json(StagedSource {
                name: f.name,
                chars: f.chars,
                ok: true,
                error: String::new(),
            })
            .into_response()
        }
        Err(e) => refuse(e.status(), e.message().to_string()),
    }
}

// ── fetched links ────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct FetchReq {
    pub urls: Vec<String>,
}

#[derive(Serialize)]
pub struct Fetched {
    pub url: String,
    pub ok: bool,
    pub title: String,
    pub chars: usize,
    /// Empty on success. Named rather than swallowed: a source that did not
    /// arrive must be visible, because a deck built without it looks exactly
    /// like a deck built with it.
    pub error: String,
    /// The site's own icon, as the page itself declares it, for the sources
    /// panel. Empty when there is no page to ask (a note, a failure).
    pub icon: String,
}

/// `POST /api/session/source/fetch?session=<cid>` — pull pages in and stage them.
///
/// One fetcher for the whole batch, so pages on one site share a connection.
/// A linked PDF is read like an uploaded one.
///
/// A site behind a bot check is a named failure rather than silence —
/// measured, openai.com answers a Cloudflare challenge to an automated visit. A source that did not arrive must be visible, because a deck built
/// without it looks exactly like a deck built with it.
pub async fn fetch_sources(Query(q): Query<SidQuery>, Json(req): Json<FetchReq>) -> Response {
    let sid = match safe_sid(&q.session) {
        Ok(s) => s,
        Err(e) => return (StatusCode::BAD_REQUEST, e).into_response(),
    };
    let dir = staging_dir(sid);
    if let Err(e) = std::fs::create_dir_all(&dir) {
        return (StatusCode::INTERNAL_SERVER_ERROR, format!("staging: {e}")).into_response();
    }

    let browser = match Browser::open().await {
        Ok(b) => b,
        Err(e) => {
            // No browser is not "no sources": say which, for every url asked for.
            let out: Vec<Fetched> = req
                .urls
                .iter()
                .map(|u| Fetched {
                    icon: String::new(),
                    url: u.clone(),
                    ok: false,
                    title: String::new(),
                    chars: 0,
                    error: format!("no browser available: {e}"),
                })
                .collect();
            return Json(out).into_response();
        }
    };

    let mut out = Vec::new();
    for url in req.urls.iter().take(MAX_URLS) {
        out.push(browser.fetch_one(&dir, url).await);
    }
    browser.close().await;
    if out.iter().any(|f| f.ok) {
        crate::collection::sources_changed(sid).await;
    }
    Json(out).into_response()
}

/// A page as read: its title, its text, and the HTML it came from (for the
/// icon and a fallback title). A PDF has no HTML; its text is its Markdown.
pub(crate) struct Page {
    pub(crate) title: String,
    pub(crate) text: String,
    pub(crate) html: String,
}

/// What a browser says it is. Some sites answer a client that names no browser
/// with an error page.
const USER_AGENT: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 \
                          (KHTML, like Gecko) Chrome/129.0 Safari/537.36 OpenNotebook";

/// A page bigger than this is refused rather than read: no source the model
/// can use is that large, and a stream that never ends must not hang a fetch.
const MAX_PAGE_BYTES: usize = 25 * 1024 * 1024;

/// The page fetcher, held for a batch of pages so they share connections.
///
/// A plain HTTP client: a page that only exists after its own JavaScript has
/// run comes back nearly empty, and is then refused by name as "almost no
/// readable text" rather than staged as a blank source.
pub(crate) struct Browser {
    http: reqwest::Client,
}

impl Browser {
    pub(crate) async fn open() -> Result<Self, String> {
        let http = reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .redirect(reqwest::redirect::Policy::limited(10))
            .connect_timeout(std::time::Duration::from_secs(15))
            .timeout(std::time::Duration::from_secs(45))
            .build()
            .map_err(|e| format!("the HTTP client could not start: {e}"))?;
        Ok(Self { http })
    }

    async fn close(self) {}

    /// Read one page, without staging it.
    pub(crate) async fn read(&self, url: &str) -> Result<Page, String> {
        if !(url.starts_with("http://") || url.starts_with("https://")) {
            return Err("not an http(s) link".into());
        }
        // One retry for the failures that are usually momentary. Seen live:
        // tldp.org reported as "address does not exist", and resolving and
        // opening it a minute later worked. A name lookup or a timeout gets
        // a second try after a short wait; anything else is final.
        let resp = match self.get(url).await {
            Ok(r) => r,
            Err(e) if is_transient(&e) => {
                tokio::time::sleep(std::time::Duration::from_millis(1500)).await;
                self.get(url).await.map_err(|e| friendly_open_error(&e))?
            }
            Err(e) => return Err(friendly_open_error(&e)),
        };
        let status = resp.status();
        let ctype = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_ascii_lowercase();
        let bytes = resp
            .bytes()
            .await
            .map_err(|e| friendly_open_error(&e.to_string()))?;
        if bytes.len() > MAX_PAGE_BYTES {
            return Err("the page is too large to read".into());
        }
        // A bot check is often a 403 or 503 with a page of its own; read it
        // anyway so the refusal check below can name it.
        if !status.is_success() && !matches!(status.as_u16(), 403 | 429 | 503) {
            return Err(match status.as_u16() {
                404 | 410 => "the page does not exist (404)".to_string(),
                401 => "the page needs a login".to_string(),
                s => format!("the site answered with an error ({s})"),
            });
        }
        // A linked PDF is a document, read like an uploaded one.
        if ctype.contains("application/pdf") || bytes.starts_with(b"%PDF-") {
            let md = to_markdown(&bytes, InputKind::Pdf)
                .map_err(|e| format!("the PDF could not be read: {e}"))?;
            let title = md
                .lines()
                .find_map(|l| l.strip_prefix("# "))
                .map(str::to_string)
                .unwrap_or_default();
            return Ok(Page {
                title,
                text: md,
                html: String::new(),
            });
        }
        if !(ctype.is_empty()
            || ctype.contains("html")
            || ctype.contains("xml")
            || ctype.starts_with("text/"))
        {
            return Err(format!(
                "the link is not a web page ({})",
                ctype.split(';').next().unwrap_or(&ctype)
            ));
        }
        let html = String::from_utf8_lossy(&bytes).into_owned();
        Ok(Page {
            title: html_title(&html),
            text: html_to_text(&html),
            html,
        })
    }

    async fn get(&self, url: &str) -> Result<reqwest::Response, String> {
        self.http
            .get(url)
            .header(
                reqwest::header::ACCEPT,
                "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            )
            .send()
            .await
            .map_err(|e| {
                // The chain carries the cause (DNS, TLS, refused); the top
                // level only says "error sending request".
                let mut msg = e.to_string();
                let mut src = std::error::Error::source(&e);
                while let Some(s) = src {
                    msg.push_str(": ");
                    msg.push_str(&s.to_string());
                    src = s.source();
                }
                msg
            })
    }

    async fn fetch_one(&self, dir: &std::path::Path, url: &str) -> Fetched {
        let bad = |e: String| Fetched {
            icon: String::new(),
            url: url.to_string(),
            ok: false,
            title: String::new(),
            chars: 0,
            error: e,
        };
        let Page { title, text, html } = match self.read(url).await {
            Ok(p) => p,
            Err(e) => return bad(e),
        };

        // A refusal can carry enough words to pass the length check below: an
        // O'Reilly "Access Denied" page was added as a 40-word source and then
        // presented as the book it refused to show.
        if is_refusal(&title, &text) {
            return bad("the site refused an automated visit".to_string());
        }
        if text.chars().filter(|c| !c.is_whitespace()).count() < 200 {
            return bad(
                if title.contains("Just a moment") || title.contains("Attention Required") {
                    "the site refused an automated visit".to_string()
                } else {
                    "the page carried almost no readable text".to_string()
                },
            );
        }
        // A page with no <title> used to be added as “”: its first heading, or
        // failing that its address made readable, is what a person would call it.
        let title = if title.trim().is_empty() {
            fallback_title(&html, url)
        } else {
            title.trim().to_string()
        };
        let title = one_line(&title);
        let named = if title.is_empty() { url } else { &title };
        let body = format!("# {named}\n\nSource: {url}\n\n{text}");
        if let Err(e) = stage(dir, &slug(named), &body) {
            return bad(format!("could not stage it: {e}"));
        }
        Fetched {
            icon: page_icon(&html, url),
            url: url.to_string(),
            ok: true,
            title,
            chars: text.chars().count(),
            error: String::new(),
        }
    }
}

/// The page's `<title>`, entities decoded, or empty.
fn html_title(html: &str) -> String {
    let lower = html.to_ascii_lowercase();
    let Some(a) = lower.find("<title") else {
        return String::new();
    };
    let Some(open_end) = lower[a..].find('>') else {
        return String::new();
    };
    let start = a + open_end + 1;
    let Some(len) = lower[start..].find("</title") else {
        return String::new();
    };
    decode_entities(html[start..start + len].trim())
}

/// Whether a fetched page is the site saying no rather than the page asked for.
///
/// Judged on the title, which block pages set consistently, and on the text
/// only when the page is short — a long article that merely mentions "access
/// denied" is still an article.
fn is_refusal(title: &str, text: &str) -> bool {
    const TITLES: &[&str] = &[
        "access denied",
        "403 forbidden",
        "forbidden",
        "just a moment",
        "attention required",
        "are you a robot",
        "request rejected",
        "security check",
        "captcha",
        "blocked",
    ];
    const TEXT: &[&str] = &[
        "you don't have permission to access",
        "you do not have permission to access",
        "access to this page has been denied",
        "verify you are human",
        "enable javascript and cookies to continue",
        "request unsuccessful. incapsula",
    ];
    // "Attention Required! | Cloudflare" and "Just a moment..." compare as
    // their main part; "Blocked I/O in Linux" is not "blocked".
    let lower_title = title.to_lowercase();
    let main = lower_title
        .split(['|', '–', '—'])
        .next()
        .unwrap_or_default()
        .split(" - ")
        .next()
        .unwrap_or_default()
        .trim_matches(|c: char| !c.is_alphanumeric());
    if TITLES.contains(&main) {
        return true;
    }
    let short = text.chars().filter(|c| !c.is_whitespace()).count() < 2_000;
    let lower = text.to_lowercase();
    short && TEXT.iter().any(|b| lower.contains(b))
}

#[cfg(test)]
mod refusal_tests {
    #[test]
    fn a_block_page_is_not_a_source() {
        assert!(super::is_refusal(
            "Access Denied",
            "You don't have permission to access this resource."
        ));
        assert!(super::is_refusal("Just a moment...", ""));
        assert!(super::is_refusal(
            "Page",
            "Access to this page has been denied because we believe you are using automation tools."
        ));
        assert!(!super::is_refusal(
            "Understanding the Linux Kernel, 3rd Edition",
            "The kernel schedules processes..."
        ));
        assert!(!super::is_refusal(
            "Blocked I/O in Linux",
            "A blocked process waits for I/O. ".repeat(10).as_str()
        ));
    }
}

/// The icon a page declares for itself, as an absolute URL.
///
/// Read from the rendered page's `<link rel=…icon…>` tags rather than guessed
/// as `/favicon.ico`: that path is often a stale or generic icon, or missing,
/// while the page names the real one. `apple-touch-icon` wins when present
/// because it is the large, full-colour logo; after it the largest declared
/// `icon`. `mask-icon` is skipped, being a one-colour silhouette. A page that
/// declares nothing falls back to `/favicon.ico`, which browsers ask for anyway.
fn page_icon(html: &str, page_url: &str) -> String {
    let lower = html.to_ascii_lowercase();
    let mut best: Option<(u32, String)> = None;
    let mut at = 0;
    while let Some(i) = lower[at..].find("<link") {
        let start = at + i;
        let Some(len) = lower[start..].find('>') else {
            break;
        };
        let tag = &html[start..start + len];
        at = start + len;
        let rel = attr(tag, "rel").to_ascii_lowercase();
        let Some(href) = Some(attr(tag, "href")).filter(|h| !h.is_empty()) else {
            continue;
        };
        if !rel
            .split_whitespace()
            .any(|r| r == "icon" || r == "apple-touch-icon")
            || rel.contains("mask-icon")
        {
            continue;
        }
        let size = attr(tag, "sizes")
            .split(['x', 'X'])
            .next()
            .and_then(|n| n.trim().parse::<u32>().ok())
            .unwrap_or(if href.ends_with(".svg") { 512 } else { 16 });
        let score = if rel.contains("apple-touch-icon") {
            10_000 + size
        } else {
            size
        };
        if best.as_ref().is_none_or(|(b, _)| score > *b) {
            best = Some((score, href));
        }
    }
    match best {
        Some((_, href)) => absolute(&decode_entities(&href), page_url),
        None => format!("{}/favicon.ico", origin(page_url)),
    }
}

/// One attribute's value out of a tag, quoted either way or bare.
fn attr(tag: &str, name: &str) -> String {
    let lower = tag.to_ascii_lowercase();
    let mut from = 0;
    while let Some(i) = lower[from..].find(name) {
        let pos = from + i;
        from = pos + name.len();
        // A whole attribute name, not the tail of another (`data-href`).
        let before = lower[..pos].chars().last().unwrap_or(' ');
        if !before.is_whitespace() {
            continue;
        }
        let rest = tag[from..].trim_start();
        let Some(rest) = rest.strip_prefix('=') else {
            continue;
        };
        let rest = rest.trim_start();
        return match rest.chars().next() {
            Some(q @ ('"' | '\'')) => rest[1..].split(q).next().unwrap_or("").to_string(),
            _ => rest
                .split(|c: char| c.is_whitespace() || c == '>')
                .next()
                .unwrap_or("")
                .to_string(),
        };
    }
    String::new()
}

/// `scheme://host` of a link.
fn origin(url: &str) -> String {
    match url.split_once("://") {
        Some((scheme, rest)) => format!("{scheme}://{}", rest.split('/').next().unwrap_or(rest)),
        None => url.to_string(),
    }
}

/// An href resolved against the page it was found on.
fn absolute(href: &str, page_url: &str) -> String {
    if href.starts_with("http://") || href.starts_with("https://") || href.starts_with("data:") {
        return href.to_string();
    }
    if let Some(rest) = href.strip_prefix("//") {
        let scheme = page_url.split_once("://").map_or("https", |(s, _)| s);
        return format!("{scheme}://{rest}");
    }
    if href.starts_with('/') {
        return format!("{}{href}", origin(page_url));
    }
    // Relative to the page's directory, query and fragment dropped first.
    let path = page_url.split(['?', '#']).next().unwrap_or(page_url);
    let dir = match path.rfind('/') {
        Some(i) if i > path.find("://").map_or(0, |j| j + 2) => &path[..=i],
        _ => return format!("{}/{href}", origin(page_url)),
    };
    format!("{dir}{href}")
}

/// Tags out, entities decoded, script and style dropped whole.
///
/// Deliberately not an HTML parser: the grounding store wants the words, and a
/// dependency that renders a DOM to get them is a lot of surface for a job that
/// a state machine does. It is lossy on layout and exact on text, which is the
/// right trade for something a model reads.
/// What a failed page open means, in words. The raw error names the HTTP
/// client's resolver or TLS layer, which says nothing to a person; it goes to
/// the log instead.
fn friendly_open_error(raw: &str) -> String {
    eprintln!("opennotebook fetch: {raw}");
    let r = raw.to_ascii_lowercase();
    let why = if r.contains("resolve host")
        || r.contains("name_not_resolved")
        || r.contains("no address")
        || r.contains("dns error")
        || r.contains("failed to lookup")
    {
        "the site could not be reached (its address does not exist)"
    } else if r.contains("timeout") || r.contains("timed out") {
        "the site took too long to answer"
    } else if r.contains("refused") {
        "the site refused the connection"
    } else if r.contains("certificate") || r.contains("ssl") || r.contains("tls") {
        "the site's security certificate is not valid"
    } else {
        "the page could not be opened"
    };
    why.to_string()
}

/// Whether a page-open failure is worth one more try.
fn is_transient(raw: &str) -> bool {
    let r = raw.to_ascii_lowercase();
    r.contains("resolve host")
        || r.contains("name_not_resolved")
        || r.contains("lookup address")
        || r.contains("dns error")
        || r.contains("timeout")
        || r.contains("timed out")
        || r.contains("connection reset")
}

/// A title for a page that declares none: its first <h1>, else its address.
fn fallback_title(html: &str, url: &str) -> String {
    let lower = html.to_ascii_lowercase();
    if let Some(a) = lower.find("<h1")
        && let Some(open_end) = lower[a..].find('>')
    {
        let start = a + open_end + 1;
        if let Some(len) = lower[start..].find("</h1>") {
            let t = html_to_text(&html[start..start + len]);
            let t = t.split_whitespace().collect::<Vec<_>>().join(" ");
            if !t.is_empty() && t.len() <= 160 {
                return t;
            }
        }
    }
    // tldp.org/LDP/tlk/kernel/processes.html -> "tldp.org · processes"
    let rest = url.split("://").nth(1).unwrap_or(url);
    let raw_host = rest.split('/').next().unwrap_or(rest);
    let host = raw_host.trim_start_matches("www.");
    let last = rest
        .trim_end_matches('/')
        .rsplit('/')
        .next()
        .filter(|l| *l != raw_host)
        .map(|l| {
            l.split(['.', '?', '#'])
                .next()
                .unwrap_or(l)
                .replace(['-', '_'], " ")
        })
        .unwrap_or_default();
    if last.trim().is_empty() {
        host.to_string()
    } else {
        format!("{host} · {}", last.trim())
    }
}

#[cfg(test)]
mod fetch_tests {
    #[test]
    fn a_page_without_a_title_is_named_by_its_heading_or_address() {
        assert_eq!(
            super::fallback_title(
                "<html><body><h1 class=x>Processes <b>in</b> Linux</h1>",
                "https://a.org/b"
            ),
            "Processes in Linux"
        );
        assert_eq!(
            super::fallback_title(
                "<p>no heading</p>",
                "https://tldp.org/LDP/tlk/kernel/processes.html"
            ),
            "tldp.org · processes"
        );
        assert_eq!(
            super::fallback_title("", "https://www.example.com/"),
            "example.com"
        );
    }

    #[test]
    fn a_dead_host_is_explained_not_quoted() {
        let m = super::friendly_open_error(
            "RPC error -32012: Navigation error: cannot resolve host \"x.gov\": failed to lookup address information",
        );
        assert_eq!(
            m,
            "the site could not be reached (its address does not exist)"
        );
    }
}

pub(crate) fn html_to_text(html: &str) -> String {
    let mut out = String::with_capacity(html.len() / 4);
    let mut chars = html.chars().peekable();
    let mut depth_skip = 0usize;
    let mut tag = String::new();
    while let Some(c) = chars.next() {
        if c == '<' {
            tag.clear();
            for t in chars.by_ref() {
                if t == '>' {
                    break;
                }
                tag.push(t);
            }
            let lower = tag.trim_start_matches('/').trim().to_ascii_lowercase();
            let name: String = lower
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric())
                .collect();
            let closing = tag.trim_start().starts_with('/');
            if matches!(
                name.as_str(),
                "script" | "style" | "noscript" | "svg" | "head"
            ) {
                if closing {
                    depth_skip = depth_skip.saturating_sub(1);
                } else {
                    depth_skip += 1;
                }
            } else if depth_skip == 0
                && matches!(
                    name.as_str(),
                    "p" | "div"
                        | "br"
                        | "li"
                        | "tr"
                        | "h1"
                        | "h2"
                        | "h3"
                        | "h4"
                        | "section"
                        | "article"
                )
            {
                out.push('\n');
            }
            continue;
        }
        if depth_skip == 0 {
            out.push(c);
        }
    }
    let out = decode_entities(&out);
    // Collapse the whitespace the markup left behind, keeping paragraph breaks.
    let mut tidy = String::with_capacity(out.len());
    let mut blank = 0;
    for line in out.lines() {
        let t = line.split_whitespace().collect::<Vec<_>>().join(" ");
        if t.is_empty() {
            blank += 1;
            if blank <= 1 && !tidy.is_empty() {
                tidy.push('\n');
            }
        } else {
            blank = 0;
            tidy.push_str(&t);
            tidy.push('\n');
        }
    }
    tidy.trim().to_string()
}

fn decode_entities(s: &str) -> String {
    s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&mdash;", "-")
        .replace("&ndash;", "-")
        .replace("&hellip;", "...")
}

/// A filename that is also a legal collection name.
fn slug(s: &str) -> String {
    let mut out = String::new();
    let mut dash = false;
    for c in s.chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c.to_ascii_lowercase());
            dash = false;
        } else if !dash && !out.is_empty() {
            out.push('_');
            dash = true;
        }
        if out.chars().count() >= 48 {
            break;
        }
    }
    let out = out.trim_matches('_').to_string();
    if out.is_empty() {
        "source".to_string()
    } else {
        out
    }
}

// ── what is staged ───────────────────────────────────────────────────────────

/// `GET /api/session/source/list?session=<cid>`
pub async fn list_sources(Query(q): Query<SidQuery>) -> Response {
    let Ok(sid) = safe_sid(&q.session) else {
        return Json(Vec::<StagedSource>::new()).into_response();
    };
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(staging_dir(sid)) {
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let chars = std::fs::read_to_string(e.path())
                .map(|s| s.chars().count())
                .unwrap_or(0);
            out.push(StagedSource {
                name,
                chars,
                ok: true,
                error: String::new(),
            });
        }
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Json(out).into_response()
}

/// A staging directory, made if it is not there yet, as the path a prep job
/// ingests.
pub fn staging_dir_made(sid: &str) -> Result<String, String> {
    let sid = safe_sid(sid)?;
    let dir = staging_dir(sid);
    std::fs::create_dir_all(&dir).map_err(|e| format!("staging: {e}"))?;
    Ok(dir.to_string_lossy().into_owned())
}

pub fn staged_path(sid: &str) -> Option<String> {
    let sid = safe_sid(sid).ok()?;
    let dir = staging_dir(sid);
    let any = std::fs::read_dir(&dir)
        .map(|mut d| d.next().is_some())
        .unwrap_or(false);
    any.then(|| dir.to_string_lossy().into_owned())
}

// ── the studio's side of the conversation ────────────────────────────────────

#[derive(Deserialize)]
pub struct ChatTurn {
    pub role: String,
    pub content: String,
}

/// Prose long enough to be material rather than conversation.
///
/// Somebody pasting six paragraphs into the chat box means them to be read;
/// somebody typing "go on" does not. A threshold is a guess, but the failure it
/// replaces is not a guess: without one, typed notes are invited by the greeting
/// and then silently dropped.
pub(crate) const NOTE_CHARS: usize = 240;

/// Read pages into a staging directory the caller made, with one browser.
///
/// One result per url, in order, failures included: a source that did not
/// arrive must be visible, because a deck built without it looks exactly like a
/// deck built with it.
pub(crate) async fn fetch_urls(dir: &std::path::Path, urls: &[String]) -> Vec<Fetched> {
    let bad = |u: &str, e: String| Fetched {
        icon: String::new(),
        url: u.to_string(),
        ok: false,
        title: String::new(),
        chars: 0,
        error: e,
    };
    match Browser::open().await {
        Ok(b) => {
            let mut out = Vec::new();
            for u in urls {
                out.push(b.fetch_one(dir, u).await);
            }
            b.close().await;
            out
        }
        // No browser is not "no sources": say which, for every url asked for.
        Err(e) => urls
            .iter()
            .map(|u| bad(u, format!("no browser available: {e}")))
            .collect(),
    }
}

/// Keep a long paste as a note source, in a staging directory the caller
/// made. Somebody pasting six paragraphs means them to be read.
pub(crate) fn keep_note(dir: &std::path::Path, text: &str) -> Fetched {
    let name = text
        .split_whitespace()
        .take(6)
        .collect::<Vec<_>>()
        .join(" ");
    match stage(dir, &slug(&name), text) {
        Ok(_) => Fetched {
            icon: String::new(),
            url: String::new(),
            ok: true,
            title: name,
            chars: text.chars().count(),
            error: String::new(),
        },
        Err(e) => Fetched {
            icon: String::new(),
            url: String::new(),
            ok: false,
            title: "your note".into(),
            chars: 0,
            error: format!("could not keep it: {e}"),
        },
    }
}

/// The links in a message, and everything that is not a link.
///
/// Whitespace-separated, because that is how a person pastes: the link is a
/// token or it is prose about a link. The punctuation around it is trimmed
/// before the token is judged — "see (https://x.dev/a)." is a sentence with a
/// link in it, not a page called `a.` and not prose with no link at all.
pub(crate) fn split_material(text: &str) -> (Vec<String>, String) {
    let mut urls = Vec::new();
    let mut rest = Vec::new();
    for token in text.split_whitespace() {
        let bare = token
            .trim_start_matches(['(', '[', '{', '<', '"', '\''])
            .trim_end_matches(['.', ',', ';', ':', ')', ']', '}', '>', '"', '\'']);
        if bare.starts_with("http://") || bare.starts_with("https://") {
            urls.push(bare.to_string());
        } else {
            rest.push(token);
        }
    }
    (urls, rest.join(" "))
}

/// The filenames staged under a cid, sorted. The build reads this directory,
/// so this is what "there is a source" means.
pub(crate) fn staged_names(sid: &str) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(staging_dir(sid))
        .map(|rd| {
            rd.flatten()
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .collect()
        })
        .unwrap_or_default();
    v.sort();
    v
}

/// A title for the session, from the model if it offered one and from the
/// material if it did not. Nobody should reach the gallery to find a card called
/// "Untitled session" because a small model left a field empty.
pub(crate) fn distilled_title(from_model: &str, staged: &[Fetched], msgs: &[ChatTurn]) -> String {
    let clip = clip_title;
    if !from_model.trim().is_empty() {
        return clip(from_model);
    }
    if let Some(s) = staged.iter().find(|s| s.ok && !s.title.is_empty()) {
        return clip(&s.title);
    }
    // What they said, minus any link in it: a session called
    // "https://arxiv.org/abs/2410.00037" names nothing.
    msgs.iter()
        .filter(|m| m.role != "assistant")
        .map(|m| split_material(&m.content).1)
        .find(|talk| talk.chars().count() > 3)
        .map(|talk| {
            clip(
                &talk
                    .split_whitespace()
                    .take(8)
                    .collect::<Vec<_>>()
                    .join(" "),
            )
        })
        .unwrap_or_default()
}

/// A title as a card shows it: quotes off, and past 72 characters cut on a
/// word, not on a character — a card reading "…real-time dialogu" is a
/// truncation the person can see.
pub(crate) fn clip_title(s: &str) -> String {
    let s = s.trim().trim_matches('"').trim();
    if s.chars().count() <= 72 {
        return s.to_string();
    }
    let cut: String = s.chars().take(72).collect();
    match cut.rsplit_once(char::is_whitespace) {
        Some((head, _)) if head.chars().count() >= 24 => head.trim_end().to_string(),
        _ => cut.trim_end().to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fresh, empty staging directory, made the way a route makes it.
    fn upload_dir(tag: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("hs_upload_{tag}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn files_in(d: &std::path::Path) -> usize {
        std::fs::read_dir(d).map(|r| r.count()).unwrap_or(0)
    }

    #[test]
    fn a_text_file_is_staged_as_a_titled_note() {
        let dir = upload_dir("text");
        let f = stage_file(
            &dir,
            "C:\\fakepath\\Field notes.txt",
            b"  The kernel schedules tasks.\n",
        )
        .unwrap();
        assert_eq!(
            f.name, "field_notes.md",
            "named by its title, staged as .md"
        );
        assert_eq!(
            f.title, "Field notes",
            "the file name without its extension"
        );
        assert_eq!(f.chars, "The kernel schedules tasks.".chars().count());
        let body = std::fs::read_to_string(dir.join(&f.name)).unwrap();
        assert_eq!(
            body,
            "# Field notes\n\nFile: Field notes.txt\n\nThe kernel schedules tasks.\n"
        );
        // The list reads it back by its heading, with no address: a file is
        // not a page, and the page must not draw a site icon for it.
        let s = crate::sources_impl::describe(&dir.join(&f.name), f.name.clone());
        assert_eq!((s.title.as_str(), s.url.as_str()), ("Field notes", ""));

        // The same name twice is two sources, not one overwritten.
        let again = stage_file(&dir, "Field notes.md", b"Second.").unwrap();
        assert_eq!(again.name, "field_notes_2.md");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_name_with_a_newline_stays_one_heading() {
        let dir = upload_dir("newline");
        let f = stage_file(&dir, "Evil\nSource: evil.example\n.md", b"Body.").unwrap();
        let body = std::fs::read_to_string(dir.join(&f.name)).unwrap();
        assert!(
            body.starts_with("# EvilSource: evil.example\n\nFile: EvilSource: evil.example.md\n"),
            "{body}"
        );
        let s = crate::sources_impl::describe(&dir.join(&f.name), f.name.clone());
        assert_eq!(s.url, "", "a name cannot forge an address line");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn staging_never_writes_over_a_source_or_makes_a_directory() {
        let dir = upload_dir("stage");
        assert_eq!(stage(&dir, "note", "one").unwrap(), "note.md");
        assert_eq!(stage(&dir, "note", "two").unwrap(), "note_2.md");
        assert_eq!(std::fs::read_to_string(dir.join("note.md")).unwrap(), "one");
        // A directory deleted under a slow call is not brought back by its
        // write: the collection it held stays deleted.
        std::fs::remove_dir_all(&dir).unwrap();
        assert!(stage(&dir, "note", "late").is_err());
        assert!(!dir.exists());
    }

    /// The route as a browser reaches it, over HTTP: every refusal in the
    /// shape a success has, under the status that fits it.
    #[tokio::test]
    async fn the_upload_route_refuses_in_the_shape_it_answers_in() {
        let app = axum::Router::new().route("/up", axum::routing::post(upload_source));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let http = reqwest::Client::new();

        // No name: refused before anything is read or staged.
        let r = http
            .post(format!("http://{addr}/up?session=s1700000000001"))
            .body("x")
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), reqwest::StatusCode::BAD_REQUEST);
        let v: serde_json::Value = r.json().await.unwrap();
        assert_eq!(v["ok"], false);
        assert!(v["error"].as_str().unwrap().contains("name"), "{v}");

        // A declared length over the limit: 413 before a byte is read. The
        // body is short; the header is what is judged.
        let r = http
            .post(format!(
                "http://{addr}/up?session=s1700000000001&name=big.pdf"
            ))
            .header("content-length", (MAX_UPLOAD_BYTES + 1).to_string())
            .body(vec![0u8; 16])
            .send()
            .await;
        // The server may close before the client finishes sending a body
        // shorter than it declared; when it answers, it answers 413.
        if let Ok(r) = r {
            assert_eq!(r.status(), reqwest::StatusCode::PAYLOAD_TOO_LARGE);
            let v: serde_json::Value = r.json().await.unwrap();
            assert_eq!(
                (v["ok"].as_bool(), v["name"].as_str()),
                (Some(false), Some("big.pdf"))
            );
        }

        // A body over the limit with no length declared: 413 once it is read.
        let big = vec![b'a'; MAX_UPLOAD_BYTES + 1];
        let stream = futures_util::stream::once(async move {
            Ok::<_, std::io::Error>(axum::body::Bytes::from(big))
        });
        let r = http
            .post(format!(
                "http://{addr}/up?session=s1700000000001&name=big.txt"
            ))
            .body(reqwest::Body::wrap_stream(stream))
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), reqwest::StatusCode::PAYLOAD_TOO_LARGE);
        let v: serde_json::Value = r.json().await.unwrap();
        assert_eq!(v["ok"], false);

        // A session id that could leave its directory.
        let r = http
            .post(format!("http://{addr}/up?session=..%2Fx&name=a.txt"))
            .body("x")
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), reqwest::StatusCode::BAD_REQUEST);
    }

    #[test]
    fn a_csv_with_a_stray_byte_is_kept_not_refused() {
        let dir = upload_dir("csv");
        let f = stage_file(&dir, "sales.CSV", b"region,total\nnorth,\xff12\n").unwrap();
        assert_eq!(f.title, "sales");
        let body = std::fs::read_to_string(dir.join(&f.name)).unwrap();
        assert!(body.contains("region,total\nnorth,\u{fffd}12"), "{body}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unreadable_file_is_refused_with_what_would_work() {
        let dir = upload_dir("refused");
        assert_eq!(
            stage_file(&dir, "talk.key", b"whatever").unwrap_err(),
            (UploadError::Unreadable(
                "Can't read .key files yet. Upload PDF, Word, PowerPoint, Excel, Markdown, text or CSV."
                    .into()
            ))
        );
        assert!(matches!(
            stage_file(&dir, "README", b"x"),
            Err(UploadError::Unreadable(m)) if m.starts_with("Can't read files without an extension")
        ));
        assert!(matches!(
            stage_file(&dir, "empty.md", b"  \n "),
            Err(UploadError::Unreadable(_))
        ));
        // A PDF of page images converts to nothing worth narrating.
        let scanned = include_bytes!("../../opennotebook_ingest/tests/fixtures/scanned.pdf");
        assert!(matches!(
            stage_file(&dir, "scan.pdf", scanned),
            Err(UploadError::Unreadable(_))
        ));
        assert_eq!(files_in(&dir), 0, "a refused file stages nothing");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_file_over_the_limit_is_too_large_before_it_is_read() {
        let dir = upload_dir("large");
        let big = vec![b'a'; MAX_UPLOAD_BYTES + 1];
        let e = stage_file(&dir, "big.txt", &big).unwrap_err();
        assert!(matches!(e, UploadError::TooLarge(_)));
        assert_eq!(e.status(), StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(files_in(&dir), 0);
        // Exactly the limit is allowed.
        let at = vec![b'a'; MAX_UPLOAD_BYTES];
        assert!(stage_file(&dir, "at.txt", &at).is_ok());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_word_document_is_converted_to_markdown() {
        let dir = upload_dir("docx");
        let docx = include_bytes!("../../opennotebook_ingest/tests/fixtures/verbatim.docx");
        let f = stage_file(&dir, "verbatim.docx", docx).unwrap();
        assert_eq!(f.title, "verbatim");
        let body = std::fs::read_to_string(dir.join(&f.name)).unwrap();
        assert!(
            body.starts_with("# verbatim\n\nFile: verbatim.docx\n\n"),
            "{body}"
        );
        assert!(f.chars >= MIN_CONVERTED_CHARS, "{body}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_session_id_cannot_escape_its_directory() {
        assert!(safe_sid("../../etc").is_err());
        assert!(safe_sid("a/b").is_err());
        assert!(safe_sid("").is_err());
        assert!(safe_sid("ok_one-2").is_ok());
    }

    #[test]
    fn a_pasted_link_is_material_and_a_short_reply_is_not() {
        let (urls, rest) = split_material("https://arxiv.org/abs/2410.00037");
        assert_eq!(urls, vec!["https://arxiv.org/abs/2410.00037".to_string()]);
        assert!(rest.is_empty(), "{rest}");
        // The turn that used to start the loop: a link and nothing else. There
        // is no question to answer, so no model is asked one.
        assert!(rest.chars().count() < NOTE_CHARS);

        let (urls, rest) = split_material("read (https://x.dev/a). what is it about?");
        assert_eq!(
            urls,
            vec!["https://x.dev/a".to_string()],
            "punctuation kept"
        );
        assert_eq!(rest, "read what is it about?");

        // Conversation is not material, however often it arrives.
        let (urls, rest) = split_material("go on");
        assert!(urls.is_empty());
        assert!(rest.chars().count() < NOTE_CHARS);
    }

    #[test]
    fn a_title_is_never_left_to_a_model_that_skipped_it() {
        let msgs = vec![ChatTurn {
            role: "user".into(),
            content: "everything about variance in evaluation please".into(),
        }];
        let read = vec![Fetched {
            icon: String::new(),
            url: "https://arxiv.org/abs/2410.00037".into(),
            ok: true,
            title: "Quantifying Variance in Evaluation Benchmarks".into(),
            chars: 42_000,
            error: String::new(),
        }];
        assert_eq!(
            distilled_title("", &read, &msgs),
            "Quantifying Variance in Evaluation Benchmarks"
        );
        assert_eq!(distilled_title("  \"Named It\" ", &read, &msgs), "Named It");
        // Cut on a word, and never on the link itself.
        let long = vec![ChatTurn {
            role: "user".into(),
            content: "https://arxiv.org/abs/2410.00037".into(),
        }];
        assert_eq!(distilled_title("", &[], &long), "");
        assert_eq!(
            distilled_title(
                "[2410.00037] Moshi: a speech-text foundation model for real-time dialogue",
                &[],
                &long
            ),
            "[2410.00037] Moshi: a speech-text foundation model for real-time"
        );
        assert_eq!(
            distilled_title("", &[], &msgs),
            "everything about variance in evaluation please"
        );
    }

    #[test]
    fn a_page_icon_is_the_one_the_page_declares() {
        let page = "https://example.com/blog/post?x=1";
        // The large touch icon beats a small favicon.
        let html = r#"<head><link rel="icon" href="/favicon-16.png" sizes="16x16">
            <link rel="apple-touch-icon" href="/apple-touch.png" sizes="180x180"></head>"#;
        assert_eq!(page_icon(html, page), "https://example.com/apple-touch.png");
        // The largest plain icon, relative hrefs resolved, &amp; decoded.
        let html = r#"<link rel='icon' sizes=32x32 href="i32.png">
            <link rel="shortcut icon" sizes="192x192" href="img/i192.png?v=1&amp;b=2">"#;
        assert_eq!(
            page_icon(html, page),
            "https://example.com/blog/img/i192.png?v=1&b=2"
        );
        // A one-colour mask icon is never the logo; protocol-relative hrefs keep the scheme.
        let html = r#"<link rel="mask-icon" href="/mask.svg"><link rel="icon" href="//cdn.example.com/f.ico">"#;
        assert_eq!(page_icon(html, page), "https://cdn.example.com/f.ico");
        // Nothing declared: the conventional path.
        assert_eq!(
            page_icon("<p>hi</p>", page),
            "https://example.com/favicon.ico"
        );
    }

    #[test]
    fn markup_becomes_words_and_scripts_do_not() {
        let html = "<html><head><title>T</title><style>p{color:red}</style></head>\
                    <body><script>var x='hidden'</script><h1>Heading</h1>\
                    <p>First&nbsp;paragraph &amp; more.</p><p>Second.</p></body></html>";
        let text = html_to_text(html);
        assert!(text.contains("Heading"), "{text}");
        assert!(text.contains("First paragraph & more."), "{text}");
        assert!(text.contains("Second."), "{text}");
        // The two things that must NOT survive, because a model reading them
        // treats them as content.
        assert!(!text.contains("hidden"), "script body leaked: {text}");
        assert!(!text.contains("color:red"), "stylesheet leaked: {text}");
    }
}
