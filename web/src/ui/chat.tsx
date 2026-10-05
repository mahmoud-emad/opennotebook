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
  said as said_,
  type Msg,
  type Picks,
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
    said_(
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

/** One entry of the `/` menu, as the server lists it. */
export type MenuCommand = {
  name: string;
  /** What follows the name, as the menu shows it; empty for none. */
  arg: string;
  label: string;
  icon: string;
};

/** A message as a command: null when it is not one, else the name typed
 * (lowercased) and what followed it. What a command does, and the answer to
 * one that does not exist, are the server's. */
export function splitCommand(text: string): { name: string; arg: string } | null {
  const t = text.trim();
  if (!t.startsWith("/")) return null;
  const rest = t.slice(1);
  const m = /\s/.exec(rest);
  const name = (m ? rest.slice(0, m.index) : rest).toLowerCase();
  return { name, arg: m ? rest.slice(m.index + 1).trim() : "" };
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

/** The output a `build` event names, by its wire name. */
export function outputFromWire(w: string): Output {
  return OUTPUTS.find((k) => k === w) ?? "session";
}

// The server's menu, read once per page load and shared by every box. Until
// it answers the menu is empty; a `/` typed meanwhile still goes to the
// server, which answers it.
let served: Promise<MenuCommand[]> | null = null;
function servedCommands(): Promise<MenuCommand[]> {
  served ??= listCommands().catch((e: unknown) => {
    served = null;
    throw e;
  });
  return served;
}

/** The `/` menu, as the server lists it. */
export function useCommands(): MenuCommand[] {
  const [list, setList] = useState<MenuCommand[]>([]);
  useEffect(() => {
    let live = true;
    servedCommands().then(
      (l) => live && setList(l),
      () => undefined,
    );
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
const mine = (text: string) => said_("You", text, true);

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

/** Clear the conversation, here and on the server. */
export function clearChat(st: ChatState): void {
  st.msgs.set(greeting());
  chatClear(st.cid).catch((e) => report(`The conversation could not be cleared. ${errText(e)}`));
}

const str = (v: unknown) => (typeof v === "string" ? v : "");
const citesOf = (v: unknown): Cite[] =>
  Array.isArray(v) ? v.map(citeFrom).filter((c): c is Cite => c !== null) : [];

/** What a turn made: a deck or audio overview started, or a map or notes
 * made, by the server. */
export type ChatMade = { kind: Output; id: string; title: string };

/** One turn: what was said goes up, and the server's work comes back line by
 * line. A message starting with `/` runs that command on the server; `said`
 * is what is shown and kept as said, when it is not the text (a question
 * asked from a mind map). A page read goes to `onSource`, what was started
 * or made to `onMade`. True when it changed the collection, so the caller
 * reads it back. */
export async function send(
  st: ChatState,
  text: string,
  picks: Picks,
  onSource: (s: Src) => void,
  onMade: (m: ChatMade) => void,
  said = "",
): Promise<boolean> {
  const shown = said.trim() === "" ? text : said;
  if (text.trim() === "" || st.talking.get()) return false;
  st.stick.set(true);
  push(st, mine(shown));
  st.talking.set(true);
  st.thinking.set(true);
  let changed = false;
  const onEvent = (v: Record<string, unknown>) => {
    const t = str(v.t);
    const text_ = str(v.text);
    switch (t) {
      case "thinking":
        st.thinking.set(true);
        break;
      case "step":
        st.thinking.set(false);
        push(st, {
          ...said_("Studio", text_, false),
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
          // The server words its failures; an old wording is put in words.
          note: failed ? readable(text_) : text_,
          status: t === "step_done" ? (failed ? "bad" : "ok") : m.status,
        }));
        break;
      }
      case "source": {
        changed = true;
        const s = v.src;
        if (s && typeof s === "object") onSource(srcFrom(s as Record<string, unknown>));
        break;
      }
      case "reply":
        st.thinking.set(false);
        push(st, { ...said_("Studio", text_, false), cites: citesOf(v.citations) });
        break;
      case "build":
        changed = true;
        onMade({ kind: outputFromWire(str(v.kind)), id: str(v.id), title: str(v.title) });
        break;
      case "state":
        // The collection changed under the turn: read it back after.
        changed = true;
        break;
      case "cleared":
        st.msgs.set(greeting());
        break;
    }
  };
  try {
    const cmd = splitCommand(text);
    if (cmd) await chatCommand(st.cid, cmd.name, cmd.arg, picks, onEvent, said);
    else await chatSay(st.cid, text, picks, onEvent);
  } catch (e) {
    push(st, said_("Studio", `I could not answer. ${readable(errText(e))}`, false));
  }
  st.thinking.set(false);
  st.talking.set(false);
  return changed;
}

/** A question answered from the sources with citations, in the conversation.
 * The server's `/ask` rather than the agent, so a click on a map topic always
 * gets a grounded answer; the question is shown as it was asked. */
export function askSources(st: ChatState, question: string, picks: Picks): Promise<boolean> {
  return send(st, `/ask ${question.trim()}`, picks, () => undefined, () => undefined, question);
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
