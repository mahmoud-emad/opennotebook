// The Ask tab: a conversation about a collection's sources, kept by the
// server, with the agent's work shown line by line as it happens. A port of
// the old app's `chat.rs`.

import { Fragment, useEffect, useState, type KeyboardEvent } from "react";
import { errText } from "./api";
import {
  chatClear,
  chatCommand,
  chatHistory,
  chatSay,
  listCommands,
  said,
  type Msg,
} from "./api-studio";
import { readable } from "./errors";
import { Icon } from "./Icon";
import { citeFrom, citeGroups, mdToHtml, withChips, type Cite } from "./markdown";
import { SettingsLink, keys, useSettings } from "./settings";
import { OUTPUTS, focusId, report, type Output } from "./shell";
import { srcFrom, type Src } from "./sources";
import { store, useStore, type Store } from "./store";

export type { Msg } from "./api-studio";

/** The Ask tab's opening line. */
export function greeting(): Msg[] {
  return [
    said(
      "Studio",
      "**Ask me anything about your sources.**\n\n" +
        "I answer from what is on the left and cite it. I can find pages on a " +
        "topic and add them as sources, then make narrated slides, an audio " +
        "overview, a mind map or study notes from them. Type **/** to see " +
        "everything I can do.",
      false,
    ),
  ];
}

// ── slash commands ───────────────────────────────────────────────────────────

/** What a slash command does: make one of the four outputs from the sources
 * at once; find pages on a topic (the agent's job); research a topic in depth
 * (the agent's too); answer a question from the sources with citations; say
 * what the studio can do, without a model call; or clear the conversation. */
export type Cmd = { make: Output } | "search" | "research" | "ask" | "help" | "clear";

/** One entry of the `/` menu, as the server lists it. */
export type MenuCommand = {
  name: string;
  /** What follows the name, as the menu shows it; empty for none. */
  arg: string;
  label: string;
  icon: string;
};

export type Command = MenuCommand & { cmd: Cmd };

/** Every command, in the order the menu lists them. The makers come first:
 * they are what the person came for. The server's `/api/commands` lists the
 * same, and is the menu once it has answered. */
export const COMMANDS: Command[] = [
  { name: "slides", arg: "[title]", label: "Build narrated slides", icon: "easel", cmd: { make: "session" } },
  { name: "audio", arg: "[focus]", label: "Make an audio overview", icon: "soundwave", cmd: { make: "audio" } },
  { name: "mindmap", arg: "[focus]", label: "Make a mind map", icon: "diagram-3", cmd: { make: "mindmap" } },
  { name: "notes", arg: "[focus]", label: "Make study notes", icon: "journal-text", cmd: { make: "notes" } },
  { name: "search", arg: "<topic>", label: "Find sources on the web", icon: "search", cmd: "search" },
  { name: "research", arg: "<topic>", label: "Research a topic in depth", icon: "stars", cmd: "research" },
  { name: "ask", arg: "<question>", label: "Ask your sources, with citations", icon: "chat-dots", cmd: "ask" },
  { name: "help", arg: "", label: "What I can do", icon: "info-circle", cmd: "help" },
  { name: "clear", arg: "", label: "Clear the conversation", icon: "trash", cmd: "clear" },
];

/** A message as a command: null when it is not one, `{ unknown }` for a name
 * no command has, else the command and what followed it. */
export function parseCommand(text: string): null | { unknown: string } | { cmd: Cmd; arg: string } {
  const t = text.trim();
  if (!t.startsWith("/")) return null;
  const rest = t.slice(1);
  const m = /\s/.exec(rest);
  const name = (m ? rest.slice(0, m.index) : rest).toLowerCase();
  const arg = m ? rest.slice(m.index + 1) : "";
  const c = COMMANDS.find((c) => c.name === name);
  return c ? { cmd: c.cmd, arg: arg.trim() } : { unknown: name };
}

/** The commands the box's text so far could be: while it is a `/` and a name
 * being typed, before any space. */
export function commandMatches<T extends MenuCommand>(text: string, list: T[]): T[] {
  if (!text.startsWith("/")) return [];
  const typed = text.slice(1);
  if (/\s/.test(typed)) return [];
  const low = typed.toLowerCase();
  return list.filter((c) => c.name.startsWith(low));
}

/** The `/help` answer: everything the studio does, from the same list the
 * menu shows. */
export function helpText(list: MenuCommand[] = COMMANDS): string {
  let s =
    "**What I can do**\n\n" +
    "Everything I make comes from the sources on the left. Add your own " +
    "links and files there, paste a link here, or let me find pages.\n\n";
  for (const c of list) s += `- \`/${c.name}${c.arg === "" ? "" : ` ${c.arg}`}\` — ${c.label}\n`;
  return s + "\nOr just tell me what you want to learn, and say build when you are happy with the sources.";
}

/** The output a `build` event names, by its wire name. */
export function outputFromWire(w: string): Output {
  return OUTPUTS.find((k) => k === w) ?? "session";
}

// The server's menu, read once per page load. Until it answers, and if it
// cannot, the built-in list is the menu.
let served: Promise<MenuCommand[]> | null = null;
let servedNow: MenuCommand[] | null = null;
function servedCommands(): Promise<MenuCommand[]> {
  served ??= listCommands().then(
    (l) => {
      servedNow = l.length > 0 ? l : null;
      return servedNow ?? COMMANDS;
    },
    () => COMMANDS,
  );
  return served;
}

/** Whether the server runs a command of this name, for one this page does
 * not know itself. */
export function servedCommand(name: string): boolean {
  return !!servedNow?.some((c) => c.name === name);
}

/** The `/` menu: the server's list once it has answered. */
export function useCommands(): MenuCommand[] {
  const [list, setList] = useState<MenuCommand[]>(COMMANDS);
  useEffect(() => {
    let live = true;
    void servedCommands().then((l) => live && setList(l));
    return () => {
      live = false;
    };
  }, []);
  return list;
}

/** How close to the bottom still counts as "at the bottom", in px. A
 * trackpad's last flick rarely lands on exactly 0. */
export const STICK_PX = 48;

/** Whether a scrolled box is showing its end. */
export function nearBottom(scrollTop: number, scrollHeight: number, clientHeight: number): boolean {
  return scrollHeight - scrollTop - clientHeight <= STICK_PX;
}

/** The chat thread's id, which is also the Ask tab's panel. */
export const THREAD_ID = "chat-thread";

/** Scroll the chat to its last line. */
function threadToBottom(): void {
  const el = document.getElementById(THREAD_ID);
  if (el) el.scrollTop = el.scrollHeight;
}

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter((w) => w !== "")
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
}

// ── the conversation's state ─────────────────────────────────────────────────

/** A collection's conversation, shared by the Ask tab and by what asks through
 * it from elsewhere on the page (a click on a mind map topic). */
export type ChatState = {
  cid: string;
  msgs: Store<Msg[]>;
  /** A turn is running; nothing else is sent until it ends. */
  talking: Store<boolean>;
  /** The model is deciding what to do next: a "Thinking…" line under the work. */
  thinking: Store<boolean>;
  /** Whether the thread follows new lines down. True until the person scrolls
   * up to read something, true again once they are back at the bottom. */
  stick: Store<boolean>;
};

/** The state, held by the calling component: the conversation as the server
 * keeps it, the greeting until it has answered or when there is none. */
export function useChatState(cid: string): ChatState {
  const [st] = useState<ChatState>(() => ({
    cid,
    msgs: store(greeting()),
    talking: store(false),
    thinking: store(false),
    stick: store(true),
  }));
  useEffect(() => {
    let live = true;
    chatHistory(st.cid).then(
      (list) => {
        // Not over anything said here while it was on its way.
        if (live && list.length > 0 && !st.msgs.get().some((m) => m.me)) st.msgs.set(list);
      },
      (e) => live && report(`The conversation could not be loaded. ${errText(e)}`),
    );
    return () => {
      live = false;
    };
  }, [st]);
  return st;
}

const push = (st: ChatState, ...m: Msg[]) => st.msgs.set((v) => [...v, ...m]);

/** Change the newest step line with this id. */
function updateStep(st: ChatState, id: string, f: (m: Msg) => Msg): void {
  st.msgs.set((v) => {
    for (let i = v.length - 1; i >= 0; i--) {
      const m = v[i]!;
      if (m.kind === "step" && m.id === id) return [...v.slice(0, i), f(m), ...v.slice(i + 1)];
    }
    return v;
  });
}

/** Something said and answered here, without the agent: a command's echo and
 * the studio's word on it. */
export function exchange(st: ChatState, mine: string, reply: string): void {
  st.stick.set(true);
  push(st, said("You", mine, true), said("Studio", reply, false));
}

/** Clear the conversation, here and on the server. */
export function clearChat(st: ChatState): void {
  st.msgs.set(greeting());
  chatClear(st.cid).catch((e) => report(`The conversation could not be cleared. ${errText(e)}`));
}

const str = (v: unknown) => (typeof v === "string" ? v : "");
const citesOf = (v: unknown): Cite[] =>
  Array.isArray(v) ? v.map(citeFrom).filter((c): c is Cite => c !== null) : [];

/** One turn with the agent: what was said goes up, and its work comes back
 * line by line. A page it reads goes to `onSource`, what it asks to be made
 * to `onBuild`. A `command` runs that `/` command on the server instead of
 * the text. True when it changed the collection, so the caller reads it back. */
export async function send(
  st: ChatState,
  text: string,
  onSource: (s: Src) => void,
  onBuild: (kind: Output, named: string | null) => void,
  command?: { name: string; arg: string },
): Promise<boolean> {
  if (text.trim() === "" || st.talking.get()) return false;
  st.stick.set(true);
  push(st, said("You", text, true));
  st.talking.set(true);
  st.thinking.set(true);
  let changed = false;
  const onEvent = (v: Record<string, unknown>) => {
    const t = str(v.t);
    const said_ = str(v.text);
    switch (t) {
      case "thinking":
        st.thinking.set(true);
        break;
      case "step":
        st.thinking.set(false);
        push(st, {
          ...said("Studio", said_, false),
          kind: "step",
          id: str(v.id),
          detail: str(v.detail),
          status: "run",
        });
        break;
      case "step_note":
      case "step_done": {
        const failed = t === "step_done" && v.ok !== true;
        updateStep(st, str(v.id), (m) => ({
          ...m,
          // A failed step's result is the server's error: said in words.
          note: failed ? readable(said_) : said_,
          status: t === "step_done" ? (failed ? "bad" : "ok") : m.status,
        }));
        break;
      }
      case "source": {
        changed = true;
        const s = v.src ?? v.source;
        if (s && typeof s === "object") onSource(srcFrom(s as Record<string, unknown>));
        break;
      }
      case "reply":
        st.thinking.set(false);
        push(st, { ...said("Studio", said_, false), cites: citesOf(v.citations) });
        break;
      case "build":
        onBuild(outputFromWire(str(v.kind)), str(v.title) || null);
        break;
      case "state":
        // The collection changed under the turn: read it back after.
        changed = true;
        break;
    }
  };
  try {
    if (command) await chatCommand(st.cid, command.name, command.arg, onEvent);
    else await chatSay(st.cid, text, onEvent);
  } catch (e) {
    push(st, said("Studio", `I could not answer. ${readable(errText(e))}`, false));
  }
  st.thinking.set(false);
  st.talking.set(false);
  return changed;
}

/** A question answered from the sources with citations, in the conversation.
 * The `/ask` command rather than the agent, so a click on a map topic always
 * gets a grounded answer. */
export async function askSources(st: ChatState, question: string): Promise<void> {
  if (question.trim() === "" || st.talking.get()) return;
  st.stick.set(true);
  push(st, said("You", question, true));
  st.talking.set(true);
  const id = `mm${Date.now()}`;
  push(st, { ...said("Studio", "Reading your sources", false), kind: "step", id, status: "run" });
  const got: { text?: string; cites?: Cite[] } = {};
  let err = "";
  try {
    await chatCommand(st.cid, "ask", question, (v) => {
      if (v.t === "reply") {
        got.text = str(v.text);
        got.cites = citesOf(v.citations);
      }
    });
    if (got.text === undefined) err = "No answer came back. Try again.";
  } catch (e) {
    err = errText(e);
  }
  const cites = got.cites ?? [];
  updateStep(st, id, (m) =>
    err === ""
      ? {
          ...m,
          status: "ok",
          note:
            cites.length === 0
              ? "no passage cited"
              : cites.length === 1
                ? "1 passage cited"
                : `${cites.length} passages cited`,
        }
      : { ...m, status: "bad", note: err },
  );
  if (err === "") push(st, { ...said("Studio", got.text ?? "", false), cites });
  else push(st, said("Studio", `I could not read the sources. ${err}`, false));
  st.talking.set(false);
}

// ── the tab ──────────────────────────────────────────────────────────────────

/** The chat box's id, so a picked command can give it the focus back. */
const INPUT_ID = "chat-input";

/** A text box that clears itself on send. Its own component so its state does
 * not re-render the whole conversation on every keystroke.
 *
 * A `/` at the start opens the command menu above it: typing narrows it, the
 * arrows move through it, Enter or a click picks. A command that takes
 * nothing is sent at once; one that takes a topic or a question is written
 * into the box to finish. */
export function ChatInput({ onSend }: { onSend: (t: string) => void }) {
  const [text, setText] = useState("");
  const [sel, setSel] = useState(0);
  // Escape closed the menu for what is typed now; typing opens it again.
  const [hidden, setHidden] = useState(false);
  const commands = useCommands();
  const matches = commandMatches(text, commands);
  const menuOpen = matches.length > 0 && !hidden;
  const n = matches.length;
  const at = Math.min(sel, Math.max(n - 1, 0));
  const pick = (c: MenuCommand) => {
    if (c.arg === "") {
      onSend(`/${c.name}`);
      setText("");
    } else setText(`/${c.name} `);
    setSel(0);
    focusId(INPUT_ID);
  };
  const submit = () => {
    if (text.trim() !== "") {
      onSend(text);
      setText("");
    }
  };
  const onKey = (e: KeyboardEvent) => {
    if (menuOpen) {
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setSel((at + 1) % n);
          return;
        case "ArrowUp":
          e.preventDefault();
          setSel((at + n - 1) % n);
          return;
        case "Enter":
        case "Tab": {
          e.preventDefault();
          const c = matches[at];
          if (c) pick(c);
          return;
        }
        case "Escape":
          e.preventDefault();
          setHidden(true);
          return;
      }
    }
    if (e.key === "Enter") submit();
  };
  return (
    <>
      {menuOpen && (
        <div className="cmd-menu" role="listbox" id="cmd-menu" aria-label="Commands">
          {matches.map((c, i) => (
            <button
              key={c.name}
              className={i === at ? "cmd on" : "cmd"}
              role="option"
              aria-selected={i === at}
              tabIndex={-1}
              // Keep the focus in the box.
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setSel(i)}
              onClick={() => pick(c)}
            >
              <Icon name={c.icon} />
              <span className="cmd-n">/{c.name}</span>
              {c.arg !== "" && <span className="cmd-a">{c.arg}</span>}
              <span className="cmd-l">{c.label}</span>
            </button>
          ))}
        </div>
      )}
      <div className="composer-box">
        <input
          id={INPUT_ID}
          value={text}
          aria-label="Message the studio"
          aria-controls="cmd-menu"
          aria-expanded={menuOpen}
          autoComplete="off"
          placeholder="Tell me what you want to learn, or type / for commands…"
          onChange={(e) => {
            setText(e.target.value);
            setSel(0);
            setHidden(false);
          }}
          onKeyDown={onKey}
        />
        <button className="primary" title="Send (Enter)" aria-label="Send" disabled={text.trim() === ""} onClick={submit}>
          <Icon name="send-fill" />
        </button>
      </div>
    </>
  );
}

/** The Ask tab: the conversation, then the box to say something in. */
export function AskTab({ st, onSend }: { st: ChatState; onSend: (t: string) => void }) {
  const msgs = useStore(st.msgs);
  const talking = useStore(st.talking);
  const thinking = useStore(st.thinking);
  const stick = useStore(st.stick);
  const last = msgs[msgs.length - 1];
  // Down to the newest line as it arrives, and on opening the tab, while the
  // person is following the end.
  useEffect(() => {
    if (!st.stick.get()) return;
    const t = setTimeout(() => st.stick.get() && threadToBottom(), 0);
    return () => clearTimeout(t);
  }, [st, msgs.length, last?.note, last?.status, last?.text, thinking]);
  // Which language and model answer, from the settings, once they are read.
  const cfg = useSettings();
  const lang = cfg.get(keys.LANGUAGE);
  const model = cfg.shown(keys.CHAT_MODEL);
  return (
    <>
      <div
        id={THREAD_ID}
        className="thread"
        role="tabpanel"
        aria-labelledby="tab-ask"
        // The person scrolling is what turns following off and on.
        onScroll={(e) => {
          const el = e.currentTarget;
          const at = nearBottom(el.scrollTop, el.scrollHeight, el.clientHeight);
          if (at !== st.stick.get()) st.stick.set(at);
        }}
      >
        {msgs.map((m, n) =>
          m.kind === "step" ? (
            // One line per action: what, on what, then the result under it.
            <div key={n} className={`step-row ${m.status}`}>
              <span className="step-ico">
                {m.status === "run" ? (
                  <span className="mini-spin" />
                ) : m.status === "bad" ? (
                  <Icon name="x-lg" />
                ) : (
                  <Icon name="check-lg" />
                )}
              </span>
              <div className="step-body">
                <div>
                  <span className="step-t">{m.text}</span>
                  {m.detail !== "" && <span className="step-dt">{` · ${m.detail}`}</span>}
                </div>
                {m.note !== "" && (
                  <div className="step-note">
                    <span className="corner" />
                    {m.note}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div key={n} className={m.me ? "cmsg me" : "cmsg"}>
              <div className="cav">{initials(m.who)}</div>
              <div className="cbub">
                <div className="cnm">{m.who}</div>
                {/* The Studio writes Markdown; the person's own words stay
                    plain text. */}
                {m.me ? (
                  <div>{m.text}</div>
                ) : m.cites.length === 0 ? (
                  <div className="md" dangerouslySetInnerHTML={{ __html: mdToHtml(m.text) }} />
                ) : (
                  <>
                    <div className="md" dangerouslySetInnerHTML={{ __html: withChips(mdToHtml(m.text), m.cites) }} />
                    <div className="cites">
                      {"Sources: "}
                      {citeGroups(m.cites).map(([title, url, ns], i) => (
                        <Fragment key={i}>
                          {i > 0 && " · "}
                          {url === "" ? (
                            title
                          ) : (
                            <a href={url} target="_blank" rel="noopener noreferrer">
                              {title}
                            </a>
                          )}
                          <span className="cite-ns">{` ${ns}`}</span>
                        </Fragment>
                      ))}
                    </div>
                  </>
                )}
              </div>
            </div>
          ),
        )}
        {talking && thinking && (
          <div className="step-row run">
            <span className="step-ico">
              <span className="mini-spin" />
            </span>
            <div className="step-body">
              <span className="step-t dim">Thinking…</span>
            </div>
          </div>
        )}
        {/* Scrolled up while the conversation goes on: a way back down that
            also turns following on again. */}
        {!stick && (
          <button
            className="to-latest"
            title="Jump to the latest message"
            onClick={() => {
              st.stick.set(true);
              threadToBottom();
            }}
          >
            <Icon name="arrow-down" />
            Latest
          </button>
        )}
      </div>
      <div className="composer">
        <ChatInput onSend={onSend} />
        <p className="chat-foot">
          {lang !== undefined && model !== undefined && (
            <>
              <span>{`Answers in ${lang} with ${model}. `}</span>
              <SettingsLink tab="models" />
            </>
          )}
          {msgs.some((m) => m.me) && !talking && (
            <button className="link-btn chat-clear" onClick={() => clearChat(st)}>
              Clear the conversation
            </button>
          )}
        </p>
      </div>
    </>
  );
}
