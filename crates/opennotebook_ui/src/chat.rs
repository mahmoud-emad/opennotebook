//! The Ask tab: a conversation about a collection's sources, kept in this
//! browser, with the agent's work shown line by line as it happens.

use dioxus::prelude::*;

use opennotebook_sdk::sources::{SourceAskInput, SourceAskReq};

use crate::api::{api_base, clean_rpc_error, gloo_sleep, post_stream, sources_client, storage};
use crate::settings::{SettingsLink, keys, use_settings};
use crate::{Icon, Output, Src, mindmap, src_from};

/// One line of the Ask tab's conversation: something said, or a line of the
/// agent's work.
#[derive(Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub(crate) struct Msg {
    pub(crate) who: String,
    pub(crate) text: String,
    pub(crate) me: bool,
    /// "" for something said, "step" for a line of the agent's work.
    #[serde(default)]
    pub(crate) kind: String,
    /// A step's id, so its progress and result land on the right line.
    #[serde(default)]
    pub(crate) id: String,
    /// What the step acted on: a query, a site.
    #[serde(default)]
    pub(crate) detail: String,
    /// The step's latest progress, then its result.
    #[serde(default)]
    pub(crate) note: String,
    /// "run", "ok" or "bad".
    #[serde(default)]
    pub(crate) status: String,
    /// The passages an answer from the sources cites, matching its `[n]`.
    #[serde(default)]
    pub(crate) cites: Vec<mindmap::Cite>,
}

impl Msg {
    pub(crate) fn said(who: &str, text: impl Into<String>, me: bool) -> Self {
        Msg {
            who: who.into(),
            text: text.into(),
            me,
            kind: String::new(),
            id: String::new(),
            detail: String::new(),
            note: String::new(),
            status: String::new(),
            cites: Vec::new(),
        }
    }
}

/// The Ask tab's conversation of a collection, kept in this browser.
///
/// Per collection, under its cid. A collection that began as a draft before
/// collections existed kept its conversation in the draft list; that is moved
/// across once, so the conversation is not lost, and taken out of the old list,
/// which goes altogether when nothing is left in it.
pub(crate) fn chat_load(cid: &str) -> Option<Vec<Msg>> {
    let st = storage()?;
    if let Some(v) = st
        .get_item(&format!("{CHAT_KEY}.{cid}"))
        .ok()
        .flatten()
        .and_then(|j| serde_json::from_str::<Vec<Msg>>(&j).ok())
    {
        return Some(v);
    }
    let drafts: Vec<serde_json::Value> = st
        .get_item(OLD_DRAFTS_KEY)
        .ok()
        .flatten()
        .and_then(|j| serde_json::from_str(&j).ok())?;
    let (mine, rest): (Vec<_>, Vec<_>) = drafts.into_iter().partition(|d| d["sid"] == cid);
    let msgs: Vec<Msg> = serde_json::from_value(mine.first()?["msgs"].clone()).ok()?;
    chat_save(cid, &msgs);
    let _ = if rest.is_empty() {
        st.remove_item(OLD_DRAFTS_KEY)
    } else {
        st.set_item(
            OLD_DRAFTS_KEY,
            &serde_json::to_string(&rest).unwrap_or_default(),
        )
    };
    Some(msgs)
}

pub(crate) fn chat_save(cid: &str, msgs: &[Msg]) {
    if let (Some(s), Ok(j)) = (storage(), serde_json::to_string(msgs)) {
        let _ = s.set_item(&format!("{CHAT_KEY}.{cid}"), &j);
    }
}

pub(crate) fn chat_forget(cid: &str) {
    if let Some(s) = storage() {
        let _ = s.remove_item(&format!("{CHAT_KEY}.{cid}"));
    }
}

pub(crate) const CHAT_KEY: &str = "opennotebook.chat";

/// Where drafts were kept before collections; only ever emptied.
pub(crate) const OLD_DRAFTS_KEY: &str = "opennotebook.drafts";

/// The Ask tab's opening line.
pub(crate) fn greeting() -> Vec<Msg> {
    vec![Msg::said(
        "Studio",
        "**Ask me anything about your sources.**\n\n\
         I answer from what is on the left and cite it. I can find pages on a \
         topic and add them as sources, then make narrated slides, an audio \
         overview, a mind map or study notes from them. Type **/** to see \
         everything I can do.",
        false,
    )]
}

// ── slash commands ───────────────────────────────────────────────────────────

/// What a slash command does.
#[derive(Clone, Copy, PartialEq, Debug)]
pub(crate) enum Cmd {
    /// Make one of the four outputs from the sources, at once.
    Make(Output),
    /// Find pages on a topic and add the best: the agent's job.
    Search,
    /// A deep research pass on a topic: the agent's job too.
    Research,
    /// A question answered from the sources, with citations.
    Ask,
    /// What the studio can do, said here without a model call.
    Help,
    /// Clear the conversation.
    Clear,
}

/// One entry of the `/` menu.
pub(crate) struct Command {
    pub(crate) name: &'static str,
    /// What follows the name, as the menu shows it; empty for none.
    pub(crate) arg: &'static str,
    pub(crate) label: &'static str,
    pub(crate) icon: &'static str,
    pub(crate) cmd: Cmd,
}

/// Every command, in the order the menu lists them. The makers come first:
/// they are what the person came for.
pub(crate) const COMMANDS: &[Command] = &[
    Command {
        name: "slides",
        arg: "[title]",
        label: "Build narrated slides",
        icon: "easel",
        cmd: Cmd::Make(Output::Session),
    },
    Command {
        name: "audio",
        arg: "[focus]",
        label: "Make an audio overview",
        icon: "soundwave",
        cmd: Cmd::Make(Output::Audio),
    },
    Command {
        name: "mindmap",
        arg: "[focus]",
        label: "Make a mind map",
        icon: "diagram-3",
        cmd: Cmd::Make(Output::MindMap),
    },
    Command {
        name: "notes",
        arg: "[focus]",
        label: "Make study notes",
        icon: "journal-text",
        cmd: Cmd::Make(Output::Notes),
    },
    Command {
        name: "search",
        arg: "<topic>",
        label: "Find sources on the web",
        icon: "search",
        cmd: Cmd::Search,
    },
    Command {
        name: "research",
        arg: "<topic>",
        label: "Research a topic in depth",
        icon: "stars",
        cmd: Cmd::Research,
    },
    Command {
        name: "ask",
        arg: "<question>",
        label: "Ask your sources, with citations",
        icon: "chat-dots",
        cmd: Cmd::Ask,
    },
    Command {
        name: "help",
        arg: "",
        label: "What I can do",
        icon: "info-circle",
        cmd: Cmd::Help,
    },
    Command {
        name: "clear",
        arg: "",
        label: "Clear the conversation",
        icon: "trash",
        cmd: Cmd::Clear,
    },
];

/// A message as a command: `None` when it is not one, `Some(Err(name))` for
/// a name no command has, else the command and what followed it.
pub(crate) fn parse_command(text: &str) -> Option<Result<(Cmd, String), String>> {
    let rest = text.trim().strip_prefix('/')?;
    let (name, arg) = rest.split_once(char::is_whitespace).unwrap_or((rest, ""));
    let name = name.to_ascii_lowercase();
    Some(
        COMMANDS
            .iter()
            .find(|c| c.name == name)
            .map(|c| (c.cmd, arg.trim().to_string()))
            .ok_or(name),
    )
}

/// The commands the box's text so far could be: while it is a `/` and a
/// name being typed, before any space.
pub(crate) fn command_matches(text: &str) -> Vec<&'static Command> {
    match text.strip_prefix('/') {
        Some(typed) if !typed.contains(char::is_whitespace) => {
            let typed = typed.to_ascii_lowercase();
            COMMANDS
                .iter()
                .filter(|c| c.name.starts_with(&typed))
                .collect()
        }
        _ => Vec::new(),
    }
}

/// The `/help` answer: everything the studio does, from the same list the
/// menu shows.
pub(crate) fn help_text() -> String {
    let mut s = String::from(
        "**What I can do**\n\n\
         Everything I make comes from the sources on the left. Add your own \
         links and files there, paste a link here, or let me find pages.\n\n",
    );
    for c in COMMANDS {
        let arg = if c.arg.is_empty() {
            String::new()
        } else {
            format!(" {}", c.arg)
        };
        s.push_str(&format!("- `/{}{}` — {}\n", c.name, arg, c.label));
    }
    s.push_str("\nOr just tell me what you want to learn, and say build when you are happy with the sources.");
    s
}

/// The output a `build` event names, by its wire name.
pub(crate) fn output_from_wire(w: &str) -> Output {
    Output::ALL
        .into_iter()
        .find(|k| k.wire() == w)
        .unwrap_or(Output::Session)
}

/// Something said and answered here, without the agent: a command's echo
/// and the studio's word on it.
pub(crate) fn exchange(st: ChatState, mine: &str, reply: impl Into<String>) {
    let ChatState {
        mut msgs,
        mut stick,
        ..
    } = st;
    stick.set(true);
    let mut m = msgs.write();
    m.push(Msg::said("You", mine, true));
    m.push(Msg::said("Studio", reply, false));
}

/// How close to the bottom still counts as "at the bottom", in px. A
/// trackpad's last flick rarely lands on exactly 0.
pub(crate) const STICK_PX: f64 = 48.0;

/// Whether a scrolled box is showing its end.
pub(crate) fn near_bottom(scroll_top: f64, scroll_height: f64, client_height: f64) -> bool {
    scroll_height - scroll_top - client_height <= STICK_PX
}

/// The chat thread's id, which is also the Ask tab's panel.
pub(crate) const THREAD_ID: &str = "chat-thread";

/// The chat thread's element, by its id.
pub(crate) fn thread_el() -> Option<web_sys::Element> {
    web_sys::window()?.document()?.get_element_by_id(THREAD_ID)
}

/// Scroll the chat to its last line.
pub(crate) fn thread_to_bottom() {
    if let Some(el) = thread_el() {
        el.set_scroll_top(el.scroll_height());
    }
}

/// A text box that clears itself on send. Its own component so its state does
/// not re-render the whole conversation on every keystroke.
///
/// A `/` at the start opens the command menu above it: typing narrows it, the
/// arrows move through it, Enter or a click picks. A command that takes
/// nothing is sent at once; one that takes a topic or a question is written
/// into the box to finish.
#[component]
pub(crate) fn ChatInput(on_send: EventHandler<String>) -> Element {
    let mut text = use_signal(String::new);
    let mut sel = use_signal(|| 0usize);
    // Escape closed the menu for what is typed now; typing opens it again.
    let mut hidden = use_signal(|| false);
    let matches = command_matches(&text.read());
    let menu_open = !matches.is_empty() && !*hidden.read();
    let n = matches.len();
    let at = (*sel.read()).min(n.saturating_sub(1));
    let mut pick = move |c: &Command| {
        if c.arg.is_empty() {
            on_send.call(format!("/{}", c.name));
            text.set(String::new());
        } else {
            text.set(format!("/{} ", c.name));
        }
        sel.set(0);
        crate::focus_id(INPUT_ID);
    };
    let mut submit = move || {
        let t = text.read().clone();
        if !t.trim().is_empty() {
            on_send.call(t);
            text.set(String::new());
        }
    };
    rsx! {
        if menu_open {
            div { class: "cmd-menu", role: "listbox", id: "cmd-menu", aria_label: "Commands",
                for (i, c) in matches.iter().enumerate() {
                    button {
                        key: "{c.name}",
                        class: if i == at { "cmd on" } else { "cmd" },
                        role: "option",
                        "aria-selected": if i == at { "true" } else { "false" },
                        tabindex: "-1",
                        // Keep the focus in the box.
                        onmousedown: move |e| e.prevent_default(),
                        onmouseenter: move |_| sel.set(i),
                        onclick: { let c: &'static Command = c; move |_| pick(c) },
                        Icon { name: c.icon }
                        span { class: "cmd-n", "/{c.name}" }
                        if !c.arg.is_empty() { span { class: "cmd-a", "{c.arg}" } }
                        span { class: "cmd-l", "{c.label}" }
                    }
                }
            }
        }
        div { class: "composer-box",
        input {
            id: INPUT_ID,
            value: "{text}",
            aria_label: "Message the studio",
            "aria-controls": "cmd-menu",
            "aria-expanded": if menu_open { "true" } else { "false" },
            autocomplete: "off",
            placeholder: "Tell me what you want to learn, or type / for commands…",
            oninput: move |e| {
                text.set(e.value());
                sel.set(0);
                hidden.set(false);
            },
            onkeydown: move |e| {
                let k = e.key();
                if menu_open {
                    match k {
                        Key::ArrowDown => { e.prevent_default(); sel.set((at + 1) % n); return; }
                        Key::ArrowUp => { e.prevent_default(); sel.set((at + n - 1) % n); return; }
                        Key::Enter | Key::Tab => {
                            e.prevent_default();
                            if let Some(c) = command_matches(&text.peek()).get(at).copied() {
                                pick(c);
                            }
                            return;
                        }
                        Key::Escape => { e.prevent_default(); hidden.set(true); return; }
                        _ => {}
                    }
                }
                if k == Key::Enter {
                    submit();
                }
            },
        }
        button {
            class: "primary",
            title: "Send (Enter)",
            aria_label: "Send",
            disabled: text.read().trim().is_empty(),
            onclick: move |_| submit(),
            Icon { name: "send-fill" }
        }
        }
    }
}

/// The chat box's id, so a picked command can give it the focus back.
const INPUT_ID: &str = "chat-input";

pub(crate) fn initials(name: &str) -> String {
    name.split_whitespace()
        .take(2)
        .filter_map(|w| w.chars().next())
        .map(|c| c.to_ascii_uppercase())
        .collect()
}

/// A chat reply's Markdown as safe HTML.
///
/// The text comes from a model, so it is treated as untrusted: raw HTML in it
/// is shown as text rather than parsed, and a link keeps its href only when it
/// is http(s) or mailto, which rules out `javascript:` and `data:`. Links open
/// in a new tab, since leaving this screen would drop the conversation.
pub(crate) fn md_to_html(src: &str) -> String {
    use pulldown_cmark::{CowStr, Event, Options, Parser, Tag, html};
    let safe = |u: &str| {
        let l = u.trim().to_ascii_lowercase();
        l.starts_with("http://") || l.starts_with("https://") || l.starts_with("mailto:")
    };
    let parser = Parser::new_ext(src, Options::ENABLE_STRIKETHROUGH).map(|ev| match ev {
        Event::Html(t) | Event::InlineHtml(t) => Event::Text(t),
        Event::Start(Tag::Link {
            link_type,
            dest_url,
            title,
            id,
        }) if !safe(&dest_url) => Event::Start(Tag::Link {
            link_type,
            dest_url: CowStr::from(""),
            title,
            id,
        }),
        Event::Start(Tag::Image { .. }) | Event::End(pulldown_cmark::TagEnd::Image) => {
            Event::Text(CowStr::from(""))
        }
        other => other,
    });
    let mut out = String::new();
    html::push_html(&mut out, parser);
    out.replace(
        "<a href=",
        "<a target=\"_blank\" rel=\"noopener noreferrer\" href=",
    )
}

// ── the conversation's state ─────────────────────────────────────────────────

/// A collection's conversation, shared by the Ask tab and by what asks through
/// it from elsewhere on the page (a click on a mind map topic).
#[derive(Clone, Copy, PartialEq)]
pub(crate) struct ChatState {
    pub(crate) msgs: Signal<Vec<Msg>>,
    /// A turn is running; nothing else is sent until it ends.
    pub(crate) talking: Signal<bool>,
    /// The model is deciding what to do next: a "Thinking…" line under the work.
    thinking: Signal<bool>,
    /// Whether the thread follows new lines down. True until the person scrolls
    /// up to read something, true again once they are back at the bottom.
    stick: Signal<bool>,
    cid: Signal<String>,
}

/// The state, as hooks of the calling component: the conversation kept in
/// this browser, saved once the person has said something.
pub(crate) fn use_chat_state(cid: &str) -> ChatState {
    let st = ChatState {
        msgs: use_signal(|| chat_load(cid).unwrap_or_else(greeting)),
        talking: use_signal(|| false),
        thinking: use_signal(|| false),
        stick: use_signal(|| true),
        cid: use_signal(|| cid.to_string()),
    };
    // The greeting alone is not worth a key.
    use_effect(move || {
        let m = st.msgs.read();
        if m.iter().any(|m| m.me) {
            chat_save(&st.cid.peek(), &m);
        }
    });
    st
}

/// One turn with the agent: what was said goes up with the conversation so
/// far, and its work comes back line by line. A page it reads goes to
/// `on_source`, what it asks to be made to `on_build`. True when it added sources,
/// so the caller reads them back.
pub(crate) async fn send(
    st: ChatState,
    text: String,
    output: Output,
    mut on_source: impl FnMut(Src),
    mut on_build: impl FnMut(Output, Option<String>),
) -> bool {
    let ChatState {
        mut msgs,
        mut talking,
        mut thinking,
        mut stick,
        cid,
    } = st;
    if text.trim().is_empty() || *talking.peek() {
        return false;
    }
    stick.set(true);
    msgs.write().push(Msg::said("You", text.clone(), true));
    talking.set(true);
    // Only what was said: the step lines are the agent's own record of its
    // work, not turns of the conversation.
    let hist: Vec<serde_json::Value> = msgs
        .read()
        .iter()
        .filter(|m| m.kind.is_empty())
        .map(|m| serde_json::json!({ "role": if m.me { "user" } else { "assistant" }, "content": m.text }))
        .collect();
    // The collection's id: everything the agent reads is staged into it.
    let body = serde_json::json!({
        "session": cid.peek().clone(),
        "messages": hist,
        "research": true,
        "output": output.wire(),
    })
    .to_string();
    thinking.set(true);
    let mut added = false;
    let streamed = post_stream(&format!("{}/chat", api_base()), &body, |v| {
        let t = v["t"].as_str().unwrap_or_default();
        let text = v["text"].as_str().unwrap_or_default().to_string();
        match t {
            "thinking" => thinking.set(true),
            "step" => {
                thinking.set(false);
                msgs.write().push(Msg {
                    kind: "step".into(),
                    id: v["id"].as_str().unwrap_or_default().to_string(),
                    detail: v["detail"].as_str().unwrap_or_default().to_string(),
                    status: "run".into(),
                    ..Msg::said("Studio", text, false)
                });
            }
            "step_note" | "step_done" => {
                let id = v["id"].as_str().unwrap_or_default();
                if let Some(m) = msgs
                    .write()
                    .iter_mut()
                    .rev()
                    .find(|m| m.kind == "step" && m.id == id)
                {
                    m.note = text;
                    if t == "step_done" {
                        m.status = if v["ok"].as_bool().unwrap_or(false) {
                            "ok"
                        } else {
                            "bad"
                        }
                        .into();
                    }
                }
            }
            "source" => {
                added = true;
                on_source(src_from(&v["src"]));
            }
            "reply" => {
                thinking.set(false);
                let cites = v["citations"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(mindmap::Cite::from_json)
                    .collect();
                msgs.write().push(Msg {
                    cites,
                    ..Msg::said("Studio", text, false)
                });
            }
            "build" => on_build(
                output_from_wire(v["kind"].as_str().unwrap_or_default()),
                v["title"]
                    .as_str()
                    .map(str::to_string)
                    .filter(|t| !t.is_empty()),
            ),
            _ => {}
        }
    })
    .await;
    thinking.set(false);
    if let Err(e) = streamed {
        msgs.write().push(Msg::said(
            "Studio",
            format!("I could not answer: {e}"),
            false,
        ));
    }
    talking.set(false);
    added
}

/// A question answered from the sources with citations, in the conversation.
/// Straight to `source_ask` rather than through the agent, so a click on a map
/// topic always gets a grounded answer.
pub(crate) async fn ask_sources(st: ChatState, question: String) {
    let ChatState {
        mut msgs,
        mut talking,
        mut stick,
        cid,
        ..
    } = st;
    if question.trim().is_empty() || *talking.peek() {
        return;
    }
    stick.set(true);
    msgs.write().push(Msg::said("You", question.clone(), true));
    talking.set(true);
    let id = format!("mm{}", js_sys::Date::now() as u64);
    msgs.write().push(Msg {
        kind: "step".into(),
        id: id.clone(),
        status: "run".into(),
        ..Msg::said("Studio", "Reading your sources", false)
    });
    let got = async {
        sources_client()?
            .source_ask(SourceAskInput {
                req: SourceAskReq {
                    sid: cid.peek().clone(),
                    question,
                    sources: None,
                },
            })
            .await
            .map_err(|e| clean_rpc_error(&e.to_string()))
    }
    .await;
    if let Some(m) = msgs
        .write()
        .iter_mut()
        .rev()
        .find(|m| m.kind == "step" && m.id == id)
    {
        match &got {
            Ok(a) => {
                m.status = "ok".into();
                m.note = match a.citations.len() {
                    0 => "no passage cited".to_string(),
                    1 => "1 passage cited".to_string(),
                    n => format!("{n} passages cited"),
                };
            }
            Err(e) => {
                m.status = "bad".into();
                m.note = e.clone();
            }
        }
    }
    match got {
        Ok(a) => {
            let cites = a
                .citations
                .iter()
                .map(|c| mindmap::Cite {
                    n: c.n as u32,
                    title: c.title.clone(),
                    url: c.url.clone(),
                    excerpt: c.excerpt.clone(),
                })
                .collect();
            msgs.write().push(Msg {
                cites,
                ..Msg::said("Studio", a.answer, false)
            });
        }
        Err(e) => msgs.write().push(Msg::said(
            "Studio",
            format!("I could not read the sources: {e}"),
            false,
        )),
    }
    talking.set(false);
}

// ── the tab ──────────────────────────────────────────────────────────────────

/// The Ask tab: the conversation, then the box to say something in.
#[component]
pub(crate) fn AskTab(st: ChatState, on_send: EventHandler<String>) -> Element {
    let ChatState {
        mut msgs,
        talking,
        thinking,
        mut stick,
        ..
    } = st;
    // Down to the newest line as it arrives, and on opening the tab, while
    // the person is following the end.
    use_effect(move || {
        let _ = msgs.read().len();
        let _ = msgs
            .read()
            .last()
            .map(|m| (m.note.len(), m.status.len(), m.text.len()));
        let _ = *thinking.read();
        if !*stick.peek() {
            return;
        }
        spawn(async move {
            gloo_sleep(0).await;
            if *stick.peek() {
                thread_to_bottom();
            }
        });
    });
    // Which language and model answer, from the settings, once they are read.
    let cfg = use_settings();
    let answered_by = cfg.get(keys::LANGUAGE).zip(cfg.shown(keys::CHAT_MODEL));
    rsx! {
        div {
            id: THREAD_ID,
            class: "thread",
            role: "tabpanel",
            aria_labelledby: "tab-ask",
            // The person scrolling is what turns following off and on.
            onscroll: move |_| {
                if let Some(el) = thread_el() {
                    let at = near_bottom(
                        el.scroll_top() as f64,
                        el.scroll_height() as f64,
                        el.client_height() as f64,
                    );
                    if at != *stick.peek() {
                        stick.set(at);
                    }
                }
            },
            for (n, m) in msgs.read().iter().cloned().enumerate() {
                if m.kind == "step" {
                    // One line per action: what, on what, then the
                    // result under it.
                    div { key: "{n}", class: "step-row {m.status}",
                        span { class: "step-ico",
                            if m.status == "run" {
                                span { class: "mini-spin" }
                            } else if m.status == "bad" {
                                Icon { name: "x-lg" }
                            } else {
                                Icon { name: "check-lg" }
                            }
                        }
                        div { class: "step-body",
                            div {
                                span { class: "step-t", "{m.text}" }
                                if !m.detail.is_empty() { span { class: "step-dt", " · {m.detail}" } }
                            }
                            if !m.note.is_empty() { div { class: "step-note", span { class: "corner" } "{m.note}" } }
                        }
                    }
                } else {
                    div { key: "{n}", class: if m.me { "cmsg me" } else { "cmsg" },
                        div { class: "cav", "{initials(&m.who)}" }
                        div { class: "cbub",
                            div { class: "cnm", "{m.who}" }
                            // The Studio writes Markdown; the person's
                            // own words stay plain text.
                            if m.me {
                                div { "{m.text}" }
                            } else if m.cites.is_empty() {
                                div { class: "md", dangerous_inner_html: md_to_html(&m.text) }
                            } else {
                                div { class: "md", dangerous_inner_html: mindmap::with_chips(&md_to_html(&m.text), &m.cites) }
                                div { class: "cites",
                                    "Sources: "
                                    for (i, (title, url, ns)) in mindmap::cite_groups(&m.cites).into_iter().enumerate() {
                                        if i > 0 { " · " }
                                        if url.is_empty() {
                                            "{title}"
                                        } else {
                                            a { href: "{url}", target: "_blank", rel: "noopener noreferrer", "{title}" }
                                        }
                                        span { class: "cite-ns", " {ns}" }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if *talking.read() && *thinking.read() {
                div { class: "step-row run",
                    span { class: "step-ico", span { class: "mini-spin" } }
                    div { class: "step-body", span { class: "step-t dim", "Thinking…" } }
                }
            }
            // Scrolled up while the conversation goes on: a way back
            // down that also turns following on again.
            if !*stick.read() {
                button {
                    class: "to-latest",
                    title: "Jump to the latest message",
                    onclick: move |_| {
                        stick.set(true);
                        thread_to_bottom();
                    },
                    Icon { name: "arrow-down" }
                    "Latest"
                }
            }
        }
        div { class: "composer",
            ChatInput { on_send: move |t: String| on_send.call(t) }
            p { class: "chat-foot",
                if let Some((lang, model)) = answered_by {
                    span { "Answers in {lang} with {model}. " }
                    SettingsLink { tab: "models" }
                }
                if msgs.read().iter().any(|m| m.me) && !*talking.read() {
                    button {
                        class: "link-btn chat-clear",
                        onclick: move |_| {
                            chat_forget(&st.cid.peek());
                            msgs.set(greeting());
                        },
                        "Clear the conversation"
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod md_tests {
    #[test]
    fn markdown_renders_and_html_does_not() {
        let h = super::md_to_html("See **this**: [Docs](https://x.org/a)\n\n1. one\n2. two");
        assert!(h.contains("<strong>this</strong>"), "{h}");
        assert!(
            h.contains("href=\"https://x.org/a\"") && h.contains("target=\"_blank\""),
            "{h}"
        );
        assert!(h.contains("<ol>") && h.contains("<li>one</li>"), "{h}");
        let bad = super::md_to_html("<script>alert(1)</script>");
        assert!(!bad.contains("<script>"), "{bad}");
        let link = super::md_to_html("A [trap](javascript:alert(1)) link");
        assert!(!link.contains("href=\"javascript"), "{link}");
    }
}

#[cfg(test)]
mod stick_tests {
    use super::near_bottom;

    /// The thread follows new lines only while the person is at its end.
    #[test]
    fn the_bottom_is_the_end_give_or_take_a_flick() {
        // 1,000 px of chat in a 400 px box: the end is scroll_top 600.
        assert!(near_bottom(600.0, 1000.0, 400.0));
        assert!(near_bottom(560.0, 1000.0, 400.0));
        assert!(!near_bottom(500.0, 1000.0, 400.0));
        assert!(!near_bottom(0.0, 1000.0, 400.0));
        // A chat shorter than its box is always at its end.
        assert!(near_bottom(0.0, 300.0, 400.0));
    }
}

#[cfg(test)]
mod command_tests {
    use super::{Cmd, command_matches, help_text, parse_command};
    use crate::Output;

    #[test]
    fn a_slash_names_a_command_and_the_rest_is_its_argument() {
        assert_eq!(
            parse_command("/mindmap"),
            Some(Ok((Cmd::Make(Output::MindMap), String::new())))
        );
        assert_eq!(
            parse_command("  /Search  rust async  "),
            Some(Ok((Cmd::Search, "rust async".into())))
        );
        assert_eq!(parse_command("/nope x"), Some(Err("nope".into())));
        assert_eq!(parse_command("no slash"), None);
    }

    #[test]
    fn the_menu_narrows_as_the_name_is_typed_and_closes_at_a_space() {
        assert_eq!(command_matches("/").len(), super::COMMANDS.len());
        let names: Vec<_> = command_matches("/re").iter().map(|c| c.name).collect();
        assert_eq!(names, ["research"]);
        assert!(command_matches("/search rust").is_empty());
        assert!(command_matches("hello").is_empty());
    }

    #[test]
    fn help_lists_every_command() {
        let h = help_text();
        for c in super::COMMANDS {
            assert!(h.contains(&format!("/{}", c.name)), "{h}");
        }
    }
}
