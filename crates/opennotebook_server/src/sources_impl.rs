//! The `sources` domain: what a collection's outputs are made from, as tools
//! any agent can call.
//!
//! Every method here is on the `sources` RPC endpoint, and is the same code the
//! Studio's own chat agent and the collection page use: a collection's sources
//! are its staging directory under `<data dir>/staging/<cid>`, a source is one Markdown file in it, and
//! `session_build`, `mindmap_create` and `notes_create` read that directory.
//! The wire field is still named `sid`; it carries the cid. Nothing here keeps
//! state of its own beyond telling the collection its sources changed.

use std::io::BufRead;

use async_trait::async_trait;

use crate::create::{self, Fetched};
use opennotebook_script::mindmap::NamedDoc;

use crate::sources::{
    Citation, DeepResearchInput, DeepResearchOutput, DraftCreateInput, DraftCreateOutput, Source,
    SourceAddFileInput, SourceAddFileOutput, SourceAddResult, SourceAddTextInput,
    SourceAddTextOutput, SourceAddUrlsInput, SourceAddUrlsOutput, SourceAskInput, SourceAskOutput,
    SourceListInput, SourceListOutput, SourceRemoveInput, SourceRemoveOutput, SourcesServiceApi,
    WebSearchHit, WebSearchInput, WebSearchOutput,
};

pub struct SourcesService;

type Ctx = opennotebook_api::RequestContext;
type RpcError = opennotebook_api::RpcError;

use crate::create::MAX_URLS;

#[async_trait]
impl SourcesServiceApi for SourcesService {
    async fn draft_create(
        &self,
        _ctx: &Ctx,
        _input: DraftCreateInput,
    ) -> Result<DraftCreateOutput, RpcError> {
        // A draft is an untitled collection now: the same cid, row and staging
        // directory `collection_create` makes, so a draft started by an agent
        // and a collection started in the browser are indistinguishable.
        let c = crate::collection::create("").await.map_err(internal)?;
        Ok(DraftCreateOutput { value: c.cid })
    }

    async fn source_add_urls(
        &self,
        _ctx: &Ctx,
        input: SourceAddUrlsInput,
    ) -> Result<SourceAddUrlsOutput, RpcError> {
        let req = input.req;
        let urls: Vec<String> = req
            .urls
            .iter()
            .map(|u| u.trim().to_string())
            .filter(|u| !u.is_empty())
            .collect();
        if urls.is_empty() {
            return Err(bad_request("no urls given"));
        }
        if urls.len() > MAX_URLS {
            return Err(bad_request(format!(
                "{} urls in one call; at most {MAX_URLS}",
                urls.len()
            )));
        }
        if let Some(u) = urls
            .iter()
            .find(|u| !(u.starts_with("http://") || u.starts_with("https://")))
        {
            return Err(bad_request(format!("`{u}` is not an http(s) link")));
        }
        let dir = staging_made(&req.sid)?;
        let results: Vec<SourceAddResult> = create::fetch_urls(&dir, &urls)
            .await
            .into_iter()
            .map(result)
            .collect();
        if results.iter().any(|r| r.ok) {
            crate::collection::sources_changed(&req.sid).await;
        }
        Ok(SourceAddUrlsOutput {
            added: results.iter().filter(|r| r.ok).count() as i64,
            results,
        })
    }

    async fn source_add_text(
        &self,
        _ctx: &Ctx,
        input: SourceAddTextInput,
    ) -> Result<SourceAddTextOutput, RpcError> {
        let req = input.req;
        let text = req.text.trim();
        if text.is_empty() {
            return Err(bad_request("the text is empty"));
        }
        let dir = staging_made(&req.sid)?;
        // A title, when given, becomes the note's heading, which is what the
        // build and the source list read it by. One line: a newline in it
        // would end the heading and start the body.
        let title = req
            .title
            .as_deref()
            .map(create::one_line)
            .filter(|t| !t.is_empty());
        let title = title.as_deref();
        let body = match title {
            Some(title) => format!("# {title}\n\n{text}"),
            None => text.to_string(),
        };
        let mut r = result(create::keep_note(&dir, &body));
        // keep_note names a note by its opening words, which here would start
        // with the heading's `#`. The caller named it; report that name.
        if let (Some(t), true) = (title, r.ok) {
            r.title = t.to_string();
        }
        if r.ok {
            crate::collection::sources_changed(&req.sid).await;
        }
        Ok(SourceAddTextOutput {
            url: r.url,
            ok: r.ok,
            title: r.title,
            chars: r.chars,
            error: r.error,
        })
    }

    /// A file, for an agent that has one: the same reading and staging as the
    /// upload route (`create::stage_file`), so a document added either way
    /// becomes the same source. A file that cannot be read is `ok: false`
    /// with the reason, as a page that cannot be read is; only a request that
    /// is not a file at all is an error.
    async fn source_add_file(
        &self,
        _ctx: &Ctx,
        input: SourceAddFileInput,
    ) -> Result<SourceAddFileOutput, RpcError> {
        let req = input.req;
        create::safe_sid(&req.sid).map_err(bad_request)?;
        if req.name.trim().is_empty() {
            return Err(bad_request("the file needs its name, extension included"));
        }
        let bytes = match decode_upload(&req.data_base64)? {
            Ok(b) => b,
            Err(too_large) => return Ok(file_result(Err(too_large))),
        };
        let dir = staging_made(&req.sid)?;
        let name = req.name.clone();
        let staged = tokio::task::spawn_blocking(move || create::stage_file(&dir, &name, &bytes))
            .await
            .map_err(internal)?;
        if staged.is_ok() {
            crate::collection::sources_changed(&req.sid).await;
        }
        Ok(file_result(staged))
    }

    async fn source_list(
        &self,
        _ctx: &Ctx,
        input: SourceListInput,
    ) -> Result<SourceListOutput, RpcError> {
        let dir = staging_of(&input.sid)?;
        let sources = create::staged_names(&input.sid)
            .into_iter()
            .map(|name| describe(&dir.join(&name), name))
            .collect();
        Ok(SourceListOutput { sources })
    }

    async fn source_remove(
        &self,
        _ctx: &Ctx,
        input: SourceRemoveInput,
    ) -> Result<SourceRemoveOutput, RpcError> {
        let req = input.req;
        let dir = staging_of(&req.sid)?;
        // A name, never a path: one segment that names a file in this collection.
        if req.name.is_empty() || req.name.contains(['/', '\\']) || req.name.starts_with('.') {
            return Err(bad_request(format!("`{}` is not a source name", req.name)));
        }
        let removed = std::fs::remove_file(dir.join(&req.name)).is_ok();
        if removed {
            crate::collection::sources_changed(&req.sid).await;
        }
        Ok(SourceRemoveOutput { value: removed })
    }

    async fn web_search(
        &self,
        _ctx: &Ctx,
        input: WebSearchInput,
    ) -> Result<WebSearchOutput, RpcError> {
        let query = input.query.trim();
        if query.is_empty() {
            return Err(bad_request("the query is empty"));
        }
        let provider = opennotebook_session::ai::provider()
            .await
            .map_err(internal)?;
        let hits = crate::agent::web_search(&provider, query)
            .await
            .map_err(internal)?;
        Ok(WebSearchOutput {
            hits: hits
                .into_iter()
                .map(|h| WebSearchHit {
                    title: h.title,
                    url: h.url,
                    snippet: h.snippet,
                })
                .collect(),
        })
    }

    async fn deep_research(
        &self,
        _ctx: &Ctx,
        input: DeepResearchInput,
    ) -> Result<DeepResearchOutput, RpcError> {
        let req = input.req;
        let topic = req.topic.trim();
        if topic.is_empty() {
            return Err(bad_request("the topic is empty"));
        }
        // Made now, not when the report is written: a collection deleted
        // during the minute this takes stays deleted, because the report's
        // write then finds no directory to land in.
        let dir = staging_made(&req.sid)?;
        // A failed research run is a result the caller reads, like a page that
        // could not be read, not an RPC error: the collection is still usable.
        let depth = opennotebook_session::settings::research_depth().await;
        let found = crate::research::gather(topic, &dir, &depth, None).await;
        if found.is_ok() {
            crate::collection::sources_changed(&req.sid).await;
        }
        Ok(match found {
            Ok(found) => DeepResearchOutput {
                url: String::new(),
                ok: true,
                title: format!("Research report: {topic} ({} sources)", found.sources),
                chars: found.chars as i64,
                error: String::new(),
            },
            Err(e) => DeepResearchOutput {
                url: String::new(),
                ok: false,
                title: String::new(),
                chars: 0,
                error: format!("{e:#}"),
            },
        })
    }
    async fn source_ask(
        &self,
        _ctx: &Ctx,
        input: SourceAskInput,
    ) -> Result<SourceAskOutput, RpcError> {
        let req = input.req;
        let question = req.question.trim();
        if question.is_empty() {
            return Err(bad_request("the question is empty"));
        }
        let docs = read_docs(&req.sid, req.sources.as_deref(), "ask")?;
        let got = opennotebook_script::cite::answer(&docs, question)
            .await
            .map_err(|e| internal(format!("the sources could not be asked: {e}")))?;
        let dir = staging_of(&req.sid)?;
        let citations = got
            .cited
            .into_iter()
            .map(|c| {
                let d = &docs[c.doc];
                let src = describe(&dir.join(&d.name), d.name.clone());
                Citation {
                    n: c.n as _,
                    name: src.name,
                    title: src.title,
                    url: src.url,
                    excerpt: c.excerpt,
                }
            })
            .collect();
        Ok(SourceAskOutput {
            answer: got.text,
            citations,
        })
    }
}

/// A collection's sources as documents to read: the ones named, or every
/// source when none are. A name that is not one of its sources is refused
/// rather than skipped, because an agent passing a stale name should hear
/// about it. `what` finishes "no sources to …" in the error for an empty one.
pub(crate) fn read_docs(
    sid: &str,
    names: Option<&[String]>,
    what: &str,
) -> Result<Vec<NamedDoc>, RpcError> {
    let sid = create::safe_sid(sid).map_err(bad_request)?;
    let dir = create::staging_dir(sid);
    let staged = create::staged_names(sid);
    let chosen: Vec<String> = match names {
        Some(asked) if !asked.is_empty() => {
            if let Some(missing) = asked.iter().find(|n| !staged.contains(n)) {
                return Err(bad_request(format!(
                    "`{missing}` is not a source of collection `{sid}`; source_list names them"
                )));
            }
            asked.to_vec()
        }
        _ => staged,
    };
    if chosen.is_empty() {
        return Err(bad_request(format!(
            "collection `{sid}` has no sources to {what}; add a page or a note first"
        )));
    }
    let docs: Vec<NamedDoc> = chosen
        .iter()
        .map(|name| {
            let text = std::fs::read_to_string(dir.join(name)).unwrap_or_default();
            NamedDoc {
                name: name.clone(),
                title: head(text.lines().map(str::to_string), name).0,
                text,
            }
        })
        .filter(|d| !d.text.trim().is_empty())
        .collect();
    if docs.is_empty() {
        return Err(bad_request(format!(
            "the sources of collection `{sid}` have no readable text"
        )));
    }
    Ok(docs)
}

/// A collection's staging directory, refusing a cid that could name anything
/// else. Not made: reading a collection never brings one into being.
pub(crate) fn staging_of(cid: &str) -> Result<std::path::PathBuf, RpcError> {
    let cid = create::safe_sid(cid).map_err(bad_request)?;
    Ok(create::staging_dir(cid))
}

/// The same, made if it is not there, by a call about to add a source.
///
/// Made at the START of the call, never at its write: `create::stage` does not
/// make directories, so a collection deleted while a page loads or a report is
/// researched stays deleted, the late write failing instead of recreating it.
/// A cid with nothing under it yet is a new collection, made by its first
/// source, as an agent that picked its own id expects.
fn staging_made(cid: &str) -> Result<std::path::PathBuf, RpcError> {
    let dir = staging_of(cid)?;
    std::fs::create_dir_all(&dir).map_err(|e| internal(format!("staging: {e}")))?;
    Ok(dir)
}

/// A file sent as base64, decoded; the inner error is a file too large, which
/// the caller answers as a result, not a failure.
///
/// Refused before decoding when the text alone says it is too large: base64 is
/// four characters for three bytes, so nothing is decoded only to be thrown
/// away. Text that is not base64 at all is a malformed request.
fn decode_upload(data: &str) -> Result<Result<Vec<u8>, create::UploadError>, RpcError> {
    use base64::Engine as _;
    let data = data.trim();
    if data.len() / 4 * 3 > create::MAX_UPLOAD_BYTES + 3 {
        return Ok(Err(create::too_large()));
    }
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(data)
        .map_err(|e| bad_request(format!("data_base64 is not standard base64: {e}")))?;
    if bytes.len() > create::MAX_UPLOAD_BYTES {
        return Ok(Err(create::too_large()));
    }
    Ok(Ok(bytes))
}

fn result(f: Fetched) -> SourceAddResult {
    SourceAddResult {
        url: f.url,
        ok: f.ok,
        title: f.title,
        chars: f.chars as i64,
        error: f.error,
    }
}

fn file_result(r: Result<create::StagedFile, create::UploadError>) -> SourceAddFileOutput {
    match r {
        Ok(f) => SourceAddFileOutput {
            url: String::new(),
            ok: true,
            title: f.title,
            chars: f.chars as _,
            error: String::new(),
        },
        Err(e) => SourceAddFileOutput {
            url: String::new(),
            ok: false,
            title: String::new(),
            chars: 0,
            error: e.message().to_string(),
        },
    }
}

/// How far into a staged file its heading and `Source:` line are looked for.
/// `create` writes both at the top; a `# ` deep in a pasted note is a section
/// of it, not its name.
const HEAD_LINES: usize = 6;

/// A staged file's title and address from its opening lines: its `# `
/// heading and its `Source:` line, which is what `create` writes. A note has
/// neither and is named by its file.
fn head(lines: impl Iterator<Item = String>, name: &str) -> (String, String) {
    let (mut title, mut url) = (None, None);
    for l in lines.take(HEAD_LINES) {
        if title.is_none()
            && let Some(t) = l.strip_prefix("# ")
        {
            title = Some(t.trim().to_string());
        }
        if url.is_none()
            && let Some(u) = l.strip_prefix("Source: ")
        {
            url = Some(u.trim().to_string());
        }
    }
    (
        title.unwrap_or_else(|| name.trim_end_matches(".md").replace('_', " ")),
        url.unwrap_or_default(),
    )
}

/// [`head`] of a file on disk, reading only its opening lines: what a list of
/// collections needs from every source, without reading every source whole.
pub(crate) fn heading(path: &std::path::Path, name: &str) -> (String, String) {
    let lines = std::fs::File::open(path)
        .map(|f| {
            std::io::BufReader::new(f)
                .lines()
                .take(HEAD_LINES)
                .map_while(Result::ok)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    head(lines.into_iter(), name)
}

#[derive(serde::Deserialize)]
pub struct ReadQuery {
    session: String,
    name: String,
}

/// `GET /api/session/source/read?session=<cid>&name=<file>` — one source,
/// whole: its title, the page it came from, and its text as Markdown. What a
/// citation opens. Only a name the collection stages is read, so `name`
/// cannot reach outside it.
pub async fn read_source(
    axum::extract::Query(q): axum::extract::Query<ReadQuery>,
) -> axum::response::Response {
    use axum::http::StatusCode;
    use axum::response::IntoResponse;
    let Ok(sid) = create::safe_sid(&q.session) else {
        return (StatusCode::BAD_REQUEST, "bad session id").into_response();
    };
    if !create::staged_names(sid).contains(&q.name) {
        return (
            StatusCode::NOT_FOUND,
            "that source is not in this collection",
        )
            .into_response();
    }
    let text = std::fs::read_to_string(create::staging_dir(sid).join(&q.name)).unwrap_or_default();
    let (title, url) = head(text.lines().map(str::to_string), &q.name);
    axum::Json(serde_json::json!({ "name": q.name, "title": title, "url": url, "text": text }))
        .into_response()
}

/// A staged file as a source, its length included, so read whole.
pub(crate) fn describe(path: &std::path::Path, name: String) -> Source {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let (title, url) = head(text.lines().map(str::to_string), &name);
    Source {
        name,
        title,
        url,
        chars: text.chars().count() as i64,
    }
}

fn internal(e: impl std::fmt::Display) -> RpcError {
    RpcError::internal(e.to_string())
}

fn bad_request(e: impl std::fmt::Display) -> RpcError {
    RpcError::invalid_params(e.to_string())
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_staged_page_is_described_by_its_heading_and_source_line() {
        let dir = std::env::temp_dir().join(format!("hs_src_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("tour.md");
        std::fs::write(
            &p,
            "# Tour of the kernel\n\nSource: https://tldp.org/x\n\nBody text.",
        )
        .unwrap();
        let s = super::describe(&p, "tour.md".into());
        assert_eq!(s.title, "Tour of the kernel");
        assert_eq!(s.url, "https://tldp.org/x");
        let n = dir.join("my_note.md");
        std::fs::write(&n, "Just a pasted note.").unwrap();
        let s = super::describe(&n, "my_note.md".into());
        assert_eq!((s.title.as_str(), s.url.as_str()), ("my note", ""));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn only_the_opening_lines_name_a_source() {
        let (t, u) = super::head(
            "Intro line\n\n\n\n\n\n# Section deep inside\n"
                .lines()
                .map(str::to_string),
            "pasted_note.md",
        );
        assert_eq!((t.as_str(), u.as_str()), ("pasted note", ""));
    }

    #[test]
    fn a_file_is_refused_by_its_size_before_it_is_decoded() {
        use base64::Engine as _;
        let ok = base64::engine::general_purpose::STANDARD.encode(b"hello");
        assert_eq!(super::decode_upload(&ok).unwrap().unwrap(), b"hello");
        // Longer than any file within the limit could encode to, and not
        // valid base64 either: the size answers first, so nothing is decoded.
        let huge = "!".repeat((crate::create::MAX_UPLOAD_BYTES / 3 + 2) * 4);
        assert!(matches!(
            super::decode_upload(&huge).unwrap(),
            Err(crate::create::UploadError::TooLarge(_))
        ));
        // Within the size, not base64: a malformed request, not a result.
        assert!(super::decode_upload("not base64 at all!").is_err());
    }
}
