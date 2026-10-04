//! The New session chat, as an agent that does the work it talks about.
//!
//! # What it replaced
//!
//! The chat used to be a model asked for two sentences of JSON and forbidden to
//! claim it could do anything. With web research on, it also declared a session
//! buildable the moment a person named a topic — so "help me understand how
//! computers work" put a Start building button on screen beside a reply asking
//! what exactly they wanted to know. Nothing had been read and nothing had been
//! found; the button promised a session built from nothing.
//!
//! Now the model has tools and uses them: it searches the web, reads the pages
//! worth reading into the session's sources, can run a deeper web research
//! pass, and can start the build itself. The button appears only once there is
//! a source on disk, because that is the only thing a build can be made of.
//!
//! # How the work is shown
//!
//! The way Claude Code shows an agent working, because it is the pattern people
//! already read: every action is one line naming what is being done and to
//! what — "Searching the web · history of computing" — with a spinner while it
//! runs and a result line under it when it finishes ("⎿ 8 results"). Long
//! actions report progress on that result line rather than adding lines. The
//! reply comes after the work, short, and does not repeat what the lines
//! already said.
//!
//! The route streams those as server-sent events, one JSON object per event:
//!
//! | `t`         | fields                        | meaning                                   |
//! |-------------|-------------------------------|-------------------------------------------|
//! | `thinking`  |                               | the model is deciding what to do next     |
//! | `step`      | `id`, `kind`, `text`, `detail`| an action started                         |
//! | `step_note` | `id`, `text`                  | progress on a running action              |
//! | `step_done` | `id`, `ok`, `text`            | the action finished, with its result      |
//! | `source`    | `src` (a `Fetched`)           | a source was added to the panel           |
//! | `reply`     | `text`                        | what the agent says                       |
//! | `build`     | `kind`, `title`               | the agent asked for an output to be made  |
//! | `state`     | `ready`, `title`              | sources on disk, and the session's title  |
//!
//! # Where it runs
//!
//! In the request. A turn is seconds, or about a minute with deep research, and
//! the person is watching it happen. What it produces is not held in the
//! request: every source lands in the session's staging directory the moment it
//! is read, so a closed tab loses the conversation's last line and nothing the
//! build needs.

use std::convert::Infallible;

use axum::Json;
use axum::response::sse::{Event, Sse};
use axum::response::{IntoResponse, Response};
use futures_util::Stream;
use serde::Deserialize;
use serde_json::{Value, json};

use opennotebook_ai::{Message, ToolChoice, ToolDefinition};

use crate::create::{self, ChatTurn, Fetched};

/// How many think-act rounds one turn may take before it must answer.
const MAX_ROUNDS: usize = 8;

/// The most pages one `add_sources` call reads.
const MAX_FETCH: usize = 4;

/// How many search results go back to the model.
const MAX_RESULTS: usize = 8;

#[derive(Deserialize)]
pub struct AgentReq {
    #[serde(default)]
    pub session: String,
    pub messages: Vec<ChatTurn>,
    /// The page's "Research the web" switch. Off, the agent has no web tools
    /// and reads only what the person gives it.
    #[serde(default)]
    pub research: bool,
    /// What the page has picked to make: "session" (the default, and what an
    /// older page that sends nothing means), "audio", "mindmap" or "notes".
    /// It is what start_build makes when the person does not say which.
    #[serde(default)]
    pub output: String,
}

/// What the studio makes, as start_build names them, with the wire name the
/// page knows each by and how the agent describes it.
const KINDS: &[(&str, &str, &str)] = &[
    (
        "slides",
        "session",
        "narrated slides: a slide deck with a spoken script, watched like a lesson and interrupted with spoken questions (a few minutes to build)",
    ),
    (
        "audio",
        "audio",
        "an audio overview: a spoken conversation about the sources to listen to, which can be joined with spoken questions (a few minutes to build)",
    ),
    (
        "mindmap",
        "mindmap",
        "a mind map: the sources' topics as a tree; clicking a topic asks about it (seconds)",
    ),
    (
        "notes",
        "notes",
        "study notes: the key ideas, a quiz and a glossary, every claim cited (under a minute)",
    ),
];

/// The start_build kind a page's `output` stands for: slides when it names
/// nothing the studio makes.
fn default_kind(output: &str) -> &'static str {
    KINDS
        .iter()
        .find(|k| k.1 == output)
        .map(|k| k.0)
        .unwrap_or("slides")
}

/// The wire name of a start_build kind, falling back to what the page picked.
fn wire_kind(kind: &str, output: &str) -> &'static str {
    let kind = if kind.is_empty() {
        default_kind(output)
    } else {
        kind
    };
    KINDS
        .iter()
        .find(|k| k.0 == kind)
        .map(|k| k.1)
        .unwrap_or("session")
}

/// `POST /api/session/chat` — one turn of the agent, streamed.
pub async fn chat(Json(req): Json<AgentReq>) -> Response {
    let events = run(req);
    Sse::new(events).into_response()
}

fn ev(v: Value) -> Result<Event, Infallible> {
    Ok(Event::default().data(v.to_string()))
}

fn run(req: AgentReq) -> impl Stream<Item = Result<Event, Infallible>> {
    async_stream::stream! {
        let Ok(sid) = create::safe_sid(&req.session).map(str::to_string) else {
            yield ev(json!({ "t": "reply", "text": "This screen lost its session id. Reload the page and try again." }));
            return;
        };
        let last_user = req
            .messages
            .iter()
            .rev()
            .find(|m| m.role != "assistant")
            .map(|m| m.content.trim().to_string())
            .unwrap_or_default();
        let dir = create::staging_dir(&sid);
        let _ = std::fs::create_dir_all(&dir);
        let mut read_this_turn: Vec<Fetched> = Vec::new();
        let mut n = 0usize;
        let mut next_id = move || {
            n += 1;
            format!("a{n}")
        };

        // ── what they pasted ────────────────────────────────────────────────
        // Links in the message are read before the model is asked anything, so
        // it answers knowing they are in. A long paste is kept as a note.
        let (urls, rest) = create::split_material(&last_user);
        if !urls.is_empty() {
            let ids: Vec<String> = urls.iter().map(|_| next_id()).collect();
            for (id, u) in ids.iter().zip(&urls) {
                yield fetch_started(id, u);
            }
            for (id, f) in ids.iter().zip(create::fetch_urls(&dir, &urls).await) {
                for e in fetched_events(id, &f) {
                    yield e;
                }
                read_this_turn.push(f);
            }
        }
        if rest.chars().count() >= create::NOTE_CHARS {
            let id = next_id();
            yield ev(json!({ "t": "step", "id": id, "kind": "note", "text": "Keeping your note", "detail": "" }));
            let f = create::keep_note(&dir, &rest);
            for e in fetched_events(&id, &f) {
                yield e;
            }
            read_this_turn.push(f);
        }

        // ── the agent ───────────────────────────────────────────────────────
        let provider = match provider().await {
            Ok(p) => p,
            Err(e) => {
                eprintln!("opennotebook agent: {e}");
                yield ev(json!({ "t": "reply", "text": "I cannot reach a model just now. Add a link or some text on the left and you can still build from it." }));
                if read_this_turn.iter().any(|f| f.ok) {
                    crate::collection::sources_changed(&sid).await;
                }
                yield state(&sid, &read_this_turn, &req.messages);
                return;
            }
        };
        let model = opennotebook_session::settings::agent_model().await;
        let language = opennotebook_session::settings::language_rule(
            &opennotebook_session::settings::language().await,
        );

        let mut convo: Vec<Message> = Vec::new();
        let window = req.messages.len().saturating_sub(24);
        for t in req.messages.iter().skip(window) {
            convo.push(if t.role == "assistant" {
                Message::assistant(t.content.clone())
            } else {
                Message::user(t.content.clone())
            });
        }

        let mut replied = false;
        // Searched this turn / added a page this turn / already nudged once /
        // already corrected a reply that named a page it could not read.
        let (mut searched, mut added, mut nudged) = (false, false, false);
        let mut corrected = false;
        let mut build_nudged = false;
        // Every search result this turn, in the order found, and every url
        // already tried. A page that cannot be read is replaced from here.
        let mut pool: Vec<String> = Vec::new();
        let mut tried: std::collections::HashSet<String> = std::collections::HashSet::new();
        for _round in 0..MAX_ROUNDS {
            yield ev(json!({ "t": "thinking" }));
            let staged = create::staged_names(&sid);
            let mut call = provider.clone()
                .completions()
                .model(&model)
                .message(Message::system(system_prompt(&staged, req.research, &req.output, &language)));
            for m in &convo {
                call = call.message(m.clone());
            }
            for t in tools(req.research) {
                call = call.tool(t);
            }
            let resp = match call.tool_choice(ToolChoice::Auto).send().await {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("opennotebook agent: model call failed: {e}");
                    // Out of credit is not a hiccup, and "ask again" would fail
                    // the same way: seen live as a 402 behind "I lost the model".
                    let text = if matches!(e, opennotebook_ai::CompletionsError::QuotaExceeded { .. }) {
                        "The AI account behind the studio is out of credit, so I cannot search or write right now. Whatever I read is on the left; once credit is added, ask again."
                    } else {
                        "I lost the model in the middle of that. Whatever I read is on the left; ask again, or build from it."
                    };
                    yield ev(json!({ "t": "reply", "text": text }));
                    replied = true;
                    break;
                }
            };

            opennotebook_session::spend::record("agent", &model, resp.usage.as_ref());
            if resp.tool_calls.is_empty() {
                // A search that found pages and a reply that lists them is the
                // failure this is for: the pages never reach the sources, so
                // nothing can be built. Once, the model is sent back to add them.
                if searched && !added && !nudged && create::staged_names(&sid).is_empty() {
                    nudged = true;
                    convo.push(Message::assistant(resp.text.clone()));
                    convo.push(Message::user(
                        "(studio) Nothing has been added yet, so nothing can be built. Call add_sources \
                         now with the best 2 to 4 URLs from your search, then reply.".to_string(),
                    ));
                    continue;
                }
                // Asked to build once the sources were in, and the sources are
                // in, the model still stopped to ask "what would you like to
                // do next?". Once, it is reminded of what it was asked.
                if !build_nudged
                    && asked_to_build(&last_user)
                    && !create::staged_names(&sid).is_empty()
                {
                    build_nudged = true;
                    convo.push(Message::assistant(resp.text.clone()));
                    convo.push(Message::user(
                        "(studio) The person asked you to make something from the sources once they \
                         were gathered, and there are sources now. Call start_build now with the kind \
                         they asked for."
                            .to_string(),
                    ));
                    continue;
                }
                // A reply that names a page it could NOT read claims a source
                // that is not there. Seen live: a search result on a dead host
                // was listed as "added" beside the three that were. Once, the
                // model is told which pages failed and asked to rewrite; after
                // that, any line naming one is dropped rather than shown.
                let failed: Vec<String> = read_this_turn
                    .iter()
                    .filter(|f| !f.ok && !f.url.is_empty())
                    .map(|f| f.url.clone())
                    .collect();
                // What the person would see: the reply without the model's
                // reasoning. Checked for failed pages after that is gone, since
                // reasoning about a failed page is not a claim that it was added.
                let shown = without_reasoning(&resp.text);
                let names_failed = failed.iter().any(|u| shown.contains(u.as_str()));
                if names_failed && !corrected {
                    corrected = true;
                    convo.push(Message::assistant(resp.text.clone()));
                    convo.push(Message::user(format!(
                        "(studio) Your reply names pages that could NOT be read and are not sources: {}. \
                         Rewrite the reply naming only pages that were added.",
                        failed.join(", ")
                    )));
                    continue;
                }
                let text = without_failed(shown.trim(), &failed);
                if !text.is_empty() {
                    yield ev(json!({ "t": "reply", "text": text }));
                    replied = true;
                }
                break;
            }

            let mut assistant = Message::assistant(resp.text.clone());
            assistant.tool_calls = resp.tool_calls.clone();
            convo.push(assistant);

            let mut building = false;
            let mut answered = false;
            for tc in &resp.tool_calls {
                let args = &tc.arguments;
                let result: String = match tc.name.as_str() {
                    "web_search" if req.research => {
                        let q = args["query"].as_str().unwrap_or_default().trim().to_string();
                        let id = next_id();
                        yield ev(json!({ "t": "step", "id": id, "kind": "search", "text": "Searching the web", "detail": q }));
                        searched = true;
                        match web_search(&provider, &q).await {
                            Ok(hits) => {
                                yield ev(json!({ "t": "step_done", "id": id, "ok": true, "text": format!("{} results", hits.len()) }));
                                for h in &hits {
                                    if !pool.contains(&h.url) {
                                        pool.push(h.url.clone());
                                    }
                                }
                                serde_json::to_string(&hits).unwrap_or_default()
                            }
                            Err(e) => {
                                yield ev(json!({ "t": "step_done", "id": id, "ok": false, "text": e.clone() }));
                                format!("search failed: {e}")
                            }
                        }
                    }
                    "add_sources" => {
                        let urls: Vec<String> = args["urls"]
                            .as_array()
                            .into_iter()
                            .flatten()
                            .filter_map(|u| u.as_str().map(str::to_string))
                            .filter(|u| u.starts_with("http://") || u.starts_with("https://"))
                            .take(MAX_FETCH)
                            .collect();
                        if urls.is_empty() {
                            "no http(s) urls given".to_string()
                        } else {
                            let mut lines = Vec::new();
                            let ids: Vec<String> = urls.iter().map(|_| next_id()).collect();
                            for (id, u) in ids.iter().zip(&urls) {
                                yield fetch_started(id, u);
                            }
                            tried.extend(urls.iter().cloned());
                            let mut need = 0usize;
                            let mut dead_hosts: std::collections::HashSet<String> = std::collections::HashSet::new();
                            for (id, f) in ids.iter().zip(create::fetch_urls(&dir, &urls).await) {
                                for e in fetched_events(id, &f) {
                                    yield e;
                                }
                                lines.push(if f.ok {
                                    format!("added {} — \"{}\", about {} words", f.url, f.title, f.chars / 6)
                                } else {
                                    need += 1;
                                    dead_hosts.insert(short_host(&f.url));
                                    format!("could not read {}: {}", f.url, f.error)
                                });
                                added |= f.ok;
                                read_this_turn.push(f);
                            }
                            // A page that cannot be read leaves the list and the
                            // next best search result takes its place, until the
                            // gap is filled or the results run out. Skips any
                            // host that already failed this call: a dead site
                            // tends to be dead for every page on it.
                            let mut attempts = 0;
                            while need > 0 && attempts < MAX_REPLACE {
                                let Some(next) = next_replacement(&pool, &tried, &dead_hosts) else { break };
                                attempts += 1;
                                tried.insert(next.clone());
                                let id = next_id();
                                yield ev(json!({ "t": "step", "id": id, "kind": "fetch", "text": "Trying another page instead", "detail": short_host(&next) }));
                                let f = create::fetch_urls(&dir, std::slice::from_ref(&next)).await.remove(0);
                                for e in fetched_events(&id, &f) {
                                    yield e;
                                }
                                if f.ok {
                                    need -= 1;
                                    added = true;
                                    lines.push(format!("replaced a page that failed with {} — \"{}\", about {} words", f.url, f.title, f.chars / 6));
                                } else {
                                    dead_hosts.insert(short_host(&f.url));
                                    lines.push(format!("could not read {} either: {}", f.url, f.error));
                                }
                                read_this_turn.push(f);
                            }
                            lines.join("\n")
                        }
                    }
                    "deep_research" if req.research => {
                        let topic = args["topic"].as_str().unwrap_or_default().trim().to_string();
                        let id = next_id();
                        yield ev(json!({ "t": "step", "id": id, "kind": "research", "text": "Researching in depth", "detail": topic }));
                        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<String>();
                        let job = {
                            let dir = dir.clone();
                            let topic = topic.clone();
                            tokio::spawn(async move {
                                let depth = opennotebook_session::settings::research_depth().await;
                                crate::research::gather(&topic, &dir, &depth, Some(tx)).await
                            })
                        };
                        tokio::pin!(job);
                        let outcome = loop {
                            tokio::select! {
                                done = &mut job => break done,
                                Some(msg) = rx.recv() => {
                                    yield ev(json!({ "t": "step_note", "id": id, "text": msg }));
                                }
                            }
                        };
                        match outcome {
                            Ok(Ok(found)) => {
                                let f = Fetched {
                                    url: String::new(),
                                    ok: true,
                                    title: format!("Web research: {topic}"),
                                    chars: found.chars,
                                    error: String::new(),
                                    icon: String::new(),
                                };
                                yield ev(json!({ "t": "step_done", "id": id, "ok": true, "text": format!("report from {} sources, added", found.sources) }));
                                yield ev(json!({ "t": "source", "src": f }));
                                read_this_turn.push(f);
                                format!("staged a research report on \"{topic}\" drawn from {} sources", found.sources)
                            }
                            Ok(Err(e)) => {
                                yield ev(json!({ "t": "step_done", "id": id, "ok": false, "text": format!("{e:#}") }));
                                format!("research failed: {e:#}")
                            }
                            Err(e) => {
                                yield ev(json!({ "t": "step_done", "id": id, "ok": false, "text": e.to_string() }));
                                format!("research failed: {e}")
                            }
                        }
                    }
                    // The answer goes to the person as it is, citations and all, and
                    // ends the turn: handed back to the model it would come out
                    // paraphrased, and a paraphrase cannot keep a citation honest.
                    "ask_sources" => {
                        let q = args["question"].as_str().unwrap_or_default().trim().to_string();
                        let id = next_id();
                        yield ev(json!({ "t": "step", "id": id, "kind": "read", "text": "Reading your sources", "detail": q }));
                        let asked = match crate::sources_impl::read_docs(&sid, None, "ask") {
                            Ok(docs) => opennotebook_script::cite::answer(&docs, &q)
                                .await
                                .map(|a| (docs, a))
                                .map_err(|e| e.to_string()),
                            Err(e) => Err(e.message.clone()),
                        };
                        match asked {
                            Ok((docs, a)) => {
                                yield ev(json!({ "t": "step_done", "id": id, "ok": true, "text": match a.cited.len() { 0 => "no passage cited".to_string(), 1 => "1 passage cited".to_string(), n => format!("{n} passages cited") } }));
                                let dir = create::staging_dir(&sid);
                                let citations: Vec<Value> = a.cited.iter().map(|c| {
                                    let d = &docs[c.doc];
                                    let src = crate::sources_impl::describe(&dir.join(&d.name), d.name.clone());
                                    json!({ "n": c.n, "name": src.name, "title": src.title, "url": src.url, "excerpt": c.excerpt })
                                }).collect();
                                yield ev(json!({ "t": "reply", "text": a.text, "citations": citations }));
                                answered = true;
                                "the answer was shown to the person".to_string()
                            }
                            Err(e) => {
                                yield ev(json!({ "t": "step_done", "id": id, "ok": false, "text": e.clone() }));
                                format!("asking the sources failed: {e}")
                            }
                        }
                    }
                    "start_build" => {
                        if create::staged_names(&sid).is_empty() {
                            "refused: there are no sources yet. Find and add some first, or ask the person for a link or notes.".to_string()
                        } else {
                            let title = args["title"].as_str().unwrap_or_default().trim().to_string();
                            let kind = wire_kind(args["kind"].as_str().unwrap_or_default().trim(), &req.output);
                            yield ev(json!({ "t": "build", "kind": kind, "title": title }));
                            building = true;
                            "it is being made".to_string()
                        }
                    }
                    other => format!("unknown or unavailable tool `{other}`"),
                };
                convo.push(Message::tool(tc.id.clone(), result));
            }
            if answered {
                replied = true;
                break;
            }
            if building {
                yield ev(json!({ "t": "reply", "text": "Making it now — it shows up in the Studio tab. You can watch it there, or leave; it keeps going." }));
                replied = true;
                break;
            }
        }
        if !replied {
            yield ev(json!({ "t": "reply", "text": "That is what I found so far — it is on the left. Say build when you are happy with it, or tell me what is missing." }));
        }
        // The chat's session is the collection's cid, so what it read this turn
        // changed that collection's sources.
        if read_this_turn.iter().any(|f| f.ok) {
            crate::collection::sources_changed(&sid).await;
        }
        yield state(&sid, &read_this_turn, &req.messages);
    }
}

/// Whether the person asked for the session to be built, in this message.
///
/// "Help me understand the Linux kernel and build the session once you collect
/// the resources" asks for it; "don't build yet" does not.
fn asked_to_build(message: &str) -> bool {
    let m = message.to_lowercase();
    let wants = [
        "build",
        "generate",
        "make the session",
        "create the session",
        "start the session",
        "make a mind map",
        "make study notes",
        "make an audio",
        "make slides",
    ]
    .iter()
    .any(|w| m.contains(w));
    let holds = [
        "don't build",
        "do not build",
        "dont build",
        "not build yet",
        "before build",
        "before you build",
        "wait",
    ]
    .iter()
    .any(|w| m.contains(w));
    wants && !holds
}

/// `text` without the model's reasoning.
///
/// Some chat models write their deliberation into the reply itself, wrapped in
/// `<thinking>` or `<think>` tags. Nothing removed it, so a person saw "I'll now
/// list only the pages that were successfully added…" in a thinking block above
/// the actual answer. Closed blocks go wherever they are; an opening tag that is
/// never closed takes the rest of its paragraph with it, because a reply cut off
/// mid-reasoning has nothing after the tag worth showing.
fn without_reasoning(text: &str) -> String {
    const TAGS: &[&str] = &["thinking", "think", "reasoning", "reflection"];
    let mut out = text.to_string();
    for tag in TAGS {
        let open = format!("<{tag}>");
        let close = format!("</{tag}>");
        loop {
            let lower = out.to_ascii_lowercase();
            let Some(start) = lower.find(&open) else {
                break;
            };
            let end = match lower[start..].find(&close) {
                Some(i) => start + i + close.len(),
                None => lower[start..]
                    .find("\n\n")
                    .map(|i| start + i)
                    .unwrap_or(out.len()),
            };
            out.replace_range(start..end, "");
        }
        // A stray closing tag left by a block that opened in an earlier turn.
        while let Some(i) = out.to_ascii_lowercase().find(&close) {
            out.replace_range(i..i + close.len(), "");
        }
    }
    out.trim().to_string()
}

/// `text` without any line that names one of `failed`. The last guard after
/// the model has been asked once to leave them out.
fn without_failed(text: &str, failed: &[String]) -> String {
    if failed.is_empty() {
        return text.to_string();
    }
    text.lines()
        .filter(|l| !failed.iter().any(|u| l.contains(u.as_str())))
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

/// How many replacement pages one add_sources call may try.
const MAX_REPLACE: usize = 6;

/// The next search result not yet tried, on a host that has not failed.
fn next_replacement(
    pool: &[String],
    tried: &std::collections::HashSet<String>,
    dead_hosts: &std::collections::HashSet<String>,
) -> Option<String> {
    pool.iter()
        .find(|u| !tried.contains(*u) && !dead_hosts.contains(&short_host(u)))
        .cloned()
}

/// The closing event: whether anything is on disk to build from, and a title.
fn state(sid: &str, read: &[Fetched], msgs: &[ChatTurn]) -> Result<Event, Infallible> {
    ev(json!({
        "t": "state",
        "ready": !create::staged_names(sid).is_empty(),
        "title": create::distilled_title("", read, msgs),
    }))
}

/// The line for a page about to be read.
fn fetch_started(id: &str, url: &str) -> Result<Event, Infallible> {
    ev(
        json!({ "t": "step", "id": id, "kind": "fetch", "text": "Reading", "detail": short_host(url) }),
    )
}

/// A finished read as the events that show it: its result line, and the
/// source row. The step line itself went out before the read started.
fn fetched_events(id: &str, f: &Fetched) -> Vec<Result<Event, Infallible>> {
    let mut out = Vec::new();
    if f.ok {
        out.push(ev(json!({
            "t": "step_done", "id": id, "ok": true,
            "text": format!("added “{}” · {} words", f.title, f.chars / 6),
        })));
    } else {
        out.push(ev(
            json!({ "t": "step_done", "id": id, "ok": false, "text": f.error }),
        ));
    }
    // Only a page that was read becomes a source. A failed one stays as its
    // step line in the chat, which says what happened, and is left off the
    // list the build is made from.
    if f.ok {
        out.push(ev(json!({ "t": "source", "src": f })));
    }
    out
}

#[cfg(test)]
mod replace_tests {
    use std::collections::HashSet;

    #[test]
    fn a_failed_page_is_replaced_by_the_next_untried_result_on_a_live_host() {
        let pool: Vec<String> = [
            "https://dead.org/a",
            "https://dead.org/b",
            "https://ok.org/c",
            "https://ok.org/d",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let mut tried: HashSet<String> = HashSet::new();
        tried.insert("https://dead.org/a".into());
        let mut dead: HashSet<String> = HashSet::new();
        dead.insert("dead.org".into());
        assert_eq!(
            super::next_replacement(&pool, &tried, &dead).as_deref(),
            Some("https://ok.org/c")
        );
        tried.insert("https://ok.org/c".into());
        tried.insert("https://ok.org/d".into());
        assert_eq!(super::next_replacement(&pool, &tried, &dead), None);
    }
}

#[cfg(test)]
mod reply_tests {
    #[test]
    fn a_request_to_build_is_recognised_and_a_hold_is_respected() {
        assert!(super::asked_to_build(
            "Help me to understnad how linux kernel is implmeneted and build the session once you collect the resources"
        ));
        assert!(super::asked_to_build("build"));
        assert!(!super::asked_to_build("add more resources"));
        assert!(!super::asked_to_build("find sources but don't build yet"));
    }

    #[test]
    fn the_models_reasoning_is_never_shown() {
        let reply = "<thinking> I'll now list only the pages that were added.</thinking>\n\n\
                     Here are the sources that I've added:\n\nTour of the Linux kernel source";
        assert_eq!(
            super::without_reasoning(reply),
            "Here are the sources that I've added:\n\nTour of the Linux kernel source"
        );
        assert_eq!(super::without_reasoning("<THINK>hmm</THINK>Done."), "Done.");
        assert_eq!(
            super::without_reasoning("<thinking>never closed\n\nSay build."),
            "Say build."
        );
        assert_eq!(super::without_reasoning("No tags here."), "No tags here.");
    }

    #[test]
    fn a_failed_page_never_survives_into_the_reply() {
        let failed = vec!["https://dead.example/x.pdf".to_string()];
        let text = "Added these:\n1. [Good](https://ok.example/a)\n2. [Dead](https://dead.example/x.pdf)\nSay build.";
        let out = super::without_failed(text, &failed);
        assert!(!out.contains("dead.example"), "{out}");
        assert!(
            out.contains("ok.example") && out.contains("Say build."),
            "{out}"
        );
        assert_eq!(super::without_failed("plain", &[]), "plain");
    }
}

fn short_host(url: &str) -> String {
    url.split("://")
        .nth(1)
        .unwrap_or(url)
        .split('/')
        .next()
        .unwrap_or(url)
        .trim_start_matches("www.")
        .to_string()
}

/// The tools the agent has. Without web research it can still read links the
/// person names and make things from them; it cannot go looking.
fn tools(research: bool) -> Vec<ToolDefinition> {
    let mut v = Vec::new();
    if research {
        v.push(ToolDefinition {
            name: "web_search".into(),
            description: "Search the web. Returns results with title, url and snippet. Use it to find the best pages on the person's topic before adding them.".into(),
            input_schema: json!({
                "type": "object",
                "properties": { "query": { "type": "string", "description": "A focused search query" } },
                "required": ["query"]
            }),
        });
        v.push(ToolDefinition {
            name: "deep_research".into(),
            description: "Run a deeper web research pass (one to a few minutes, by the studio's research depth setting) that reads many pages and writes a report, added as one source. Use it for broad topics where a few pages are not enough.".into(),
            input_schema: json!({
                "type": "object",
                "properties": { "topic": { "type": "string" } },
                "required": ["topic"]
            }),
        });
    }
    v.push(ToolDefinition {
        name: "add_sources".into(),
        description: format!("Read up to {MAX_FETCH} web pages and add them to the session's sources. The build is made from the sources."),
        input_schema: json!({
            "type": "object",
            "properties": { "urls": { "type": "array", "items": { "type": "string" } } },
            "required": ["urls"]
        }),
    });
    v.push(ToolDefinition {
        name: "ask_sources".into(),
        description: "Answer a question from the sources already added, with citations. The answer is shown to the person directly, so do not repeat it. Use it whenever they ask what their sources or material say.".into(),
        input_schema: json!({
            "type": "object",
            "properties": { "question": { "type": "string", "description": "The question, in the person's words" } },
            "required": ["question"]
        }),
    });
    v.push(ToolDefinition {
        name: "start_build".into(),
        description: "Make one of the studio's outputs from the sources: narrated slides, an audio \
                      overview, a mind map or study notes. It appears in the Studio tab. Refused when \
                      there are no sources."
            .into(),
        input_schema: json!({
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": KINDS.iter().map(|k| k.0).collect::<Vec<_>>(),
                    "description": "What to make. Leave it out for what the person has picked on the page."
                },
                "title": { "type": "string", "description": "A short title for it" }
            },
            "required": ["title"]
        }),
    });
    v
}

fn system_prompt(staged: &[String], research: bool, output: &str, language: &str) -> String {
    let sources = if staged.is_empty() {
        "none yet".to_string()
    } else {
        staged.join(", ")
    };
    let web = if research {
        "Web research is ON. When the person names a topic, find the material yourself: \
         web_search, then ALWAYS add_sources with the 2 to 4 best pages from the results before you \
         reply — a page you only mention is not a source and cannot be built from (prefer authoritative ones: \
         encyclopedias, the paper's own page, official documentation, well-known explainers). \
         Use deep_research only for a broad topic where a few pages will not cover it. Never ask \
         the person to paste something you can search for. If a name is ambiguous (\"the Mochi \
         paper\" could be several things), search first and pick the reading that fits, or ask \
         one short question if the results really split."
    } else {
        "Web research is OFF. You cannot search. Read any link the person names with \
         add_sources, and otherwise ask them for a link or notes."
    };
    let can_make: String = KINDS
        .iter()
        .map(|(name, _, what)| format!("- {name}: {what}"))
        .collect::<Vec<_>>()
        .join("\n");
    let research_line = if research {
        "- find sources: search the web (web_search) and add the best pages (add_sources)\n\
         - research in depth: a longer pass over many pages, written up as one source (deep_research)\n"
    } else {
        ""
    };
    let picked = default_kind(output);
    let mut p = format!(
        "You are the OpenNotebook agent. The studio turns a person's sources (web pages, \
         PDFs, documents, notes) into things that help them learn, and everything it makes \
         is made only from those sources.\n\n\
         What you can do:\n\
         {research_line}\
         - read a link the person gives you into the sources (add_sources)\n\
         - answer questions from the sources, with citations (ask_sources)\n\
         - make any of these from the sources with start_build:\n{can_make}\n\n\
         When the person asks what you can do, say this plainly and briefly, mention that \
         they can add their own files and links on the left, and that typing / in the chat \
         lists these as commands (/slides, /audio, /mindmap, /notes, /search, /research, \
         /ask, /help). A message starting with /search means find sources on what follows; \
         /research means research what follows in depth.\n\n\
         {web}\n\n\
         Sources already added: {sources}\n\n\
         Making things: the person has picked {picked} on the page, so that is what \
         start_build makes unless they ask for another kind. When there are sources that \
         cover what they want, either call start_build — but only if they have asked you to \
         build, make, create or generate something — or tell them in one sentence what you \
         gathered and what you can make from it. start_build is refused with no sources.\n\n\
         Questions: when the person asks what their sources say about something, call \
         ask_sources rather than answering yourself. Its answer reaches them directly with \
         citations.\n\n\
         The person sees every tool you use as a line in the chat and every added source on \
         the left, so do not describe your searches or repeat the list of pages. Reply in two or \
         three short sentences, or a short list when asked what you can do; you may use \
         Markdown (bold, a link, a list) sparingly. Only ever call a \
         page a source if add_sources reported it as added — a page that could not be read is \
         NOT a source, so never name it as one. Never invent what a source says."
    );
    if !language.is_empty() {
        p.push_str("\n\n");
        p.push_str(language);
    }
    p
}

/// The model endpoint every call goes through; see `opennotebook_session::ai`.
async fn provider() -> Result<opennotebook_ai::Provider, String> {
    opennotebook_session::ai::provider().await
}

/// One web search result, as the model sees it.
#[derive(serde::Serialize, Debug, PartialEq)]
pub(crate) struct Hit {
    pub(crate) title: String,
    pub(crate) url: String,
    pub(crate) snippet: String,
}

/// Search the web through Perplexity Sonar on OpenRouter.
///
/// Not DuckDuckGo: its HTML endpoint answered this box for a handful of
/// queries and then returned nothing to anyone, curl included — the bot wall
/// other scrapers have documented too. Sonar goes through the same key and the
/// same client as every other model call, at about half a cent a search, and
/// its answer text comes back too, which gives the agent a summary to judge
/// the results by.
pub(crate) async fn web_search(
    provider: &opennotebook_ai::Provider,
    query: &str,
) -> Result<Vec<Hit>, String> {
    if query.is_empty() {
        return Err("empty query".into());
    }
    // The Web search model setting; Perplexity's Sonar by default, which does
    // a real web search behind a chat completion and returns the URLs it
    // found as `citations`.
    let model = opennotebook_session::settings::search_model().await;
    let resp = provider
        .clone()
        .completions()
        .model(&model)
        .message(Message::user(format!(
            "Search the web for the most useful, authoritative sources on: {query}\n\
             Answer in two sentences and cite your sources."
        )))
        .send()
        .await
        .map_err(|e| format!("search failed: {e}"))?;
    opennotebook_session::spend::record("web_search", &model, resp.usage.as_ref());
    let hits = citations(&resp.raw, &resp.text);
    if hits.is_empty() {
        return Err("no results".into());
    }
    Ok(hits)
}

/// The cited URLs out of a Sonar response: the top-level `citations` array, or
/// the message's `url_citation` annotations, whichever it used. The answer's
/// text rides along on the first hit as its snippet.
fn citations(raw: &Value, answer: &str) -> Vec<Hit> {
    let mut hits: Vec<Hit> = raw["citations"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|u| u.as_str())
        .filter(|u| u.starts_with("http"))
        .map(|u| Hit {
            title: short_host(u),
            url: u.to_string(),
            snippet: String::new(),
        })
        .collect();
    if hits.is_empty() {
        hits = raw["choices"][0]["message"]["annotations"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|a| {
                let c = &a["url_citation"];
                let url = c["url"].as_str()?.to_string();
                Some(Hit {
                    title: c["title"]
                        .as_str()
                        .map(str::to_string)
                        .unwrap_or_else(|| short_host(&url)),
                    url,
                    snippet: String::new(),
                })
            })
            .collect();
    }
    let mut seen = std::collections::HashSet::new();
    hits.retain(|h| seen.insert(h.url.clone()));
    hits.truncate(MAX_RESULTS);
    if let Some(first) = hits.first_mut() {
        first.snippet = answer.chars().take(600).collect();
    }
    hits
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sonar_citations_become_hits_with_the_answer_as_context() {
        let raw = json!({ "citations": ["https://en.wikipedia.org/wiki/Computer", "https://en.wikipedia.org/wiki/Computer", "https://www.britannica.com/technology/computer"] });
        let hits = citations(&raw, "Computers process data.");
        assert_eq!(hits.len(), 2, "duplicates dropped");
        assert_eq!(hits[0].url, "https://en.wikipedia.org/wiki/Computer");
        assert_eq!(hits[0].snippet, "Computers process data.");
        let ann = json!({ "choices": [{ "message": { "annotations": [{ "url_citation": { "url": "https://x.dev/a", "title": "A" } }] } }] });
        assert_eq!(citations(&ann, "")[0].title, "A");
    }

    #[test]
    fn web_tools_exist_only_with_research_on() {
        let names = |r| tools(r).into_iter().map(|t| t.name).collect::<Vec<_>>();
        assert!(names(true).contains(&"web_search".to_string()));
        assert!(!names(false).contains(&"web_search".to_string()));
        assert!(names(false).contains(&"start_build".to_string()));
    }
}

#[cfg(test)]
mod output_tests {
    /// Every page can make every kind; what the page picked is the default.
    #[test]
    fn the_agent_knows_everything_it_can_make() {
        let names: Vec<String> = super::tools(false).into_iter().map(|t| t.name).collect();
        assert!(names.contains(&"start_build".to_string()));
        assert!(names.contains(&"ask_sources".to_string()));
        let p = super::system_prompt(&[], true, "mindmap", "");
        for k in [
            "slides",
            "audio overview",
            "mind map",
            "study notes",
            "deep_research",
            "/help",
        ] {
            assert!(p.contains(k), "prompt lacks {k}");
        }
        assert!(p.contains("picked mindmap"));
        let p = super::system_prompt(&[], false, "", "");
        assert!(
            p.contains("picked slides"),
            "an older page that sends nothing builds slides"
        );
        assert!(
            !p.contains("deep_research"),
            "no research tools with research off"
        );
    }

    #[test]
    fn a_kind_maps_to_what_the_page_knows_it_by() {
        assert_eq!(super::wire_kind("mindmap", "session"), "mindmap");
        assert_eq!(super::wire_kind("slides", "audio"), "session");
        assert_eq!(super::wire_kind("", "audio"), "audio");
        assert_eq!(super::wire_kind("", ""), "session");
        assert_eq!(super::wire_kind("nonsense", "notes"), "session");
    }
}
