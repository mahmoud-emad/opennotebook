// The watch page of a video (docs/video-overview-spec.md, the watch page):
// the video on the left, and beside it a tutor that knows the moment.
//
// The video leads, with its chapters as a strip under it to jump by. The
// rail has three tabs:
// - Explain: ask about what is on now. The quick prompts ask the usual
//   things in one press; a question pauses the video, and Resume carries on.
//   An answer cites the sources as the chat does, and its [m:ss] moments are
//   links that seek the video there. The thread is kept in this browser.
// - Transcript: the narration, following the voice; a line plays from there.
// - Chapters: the chapters and, for a whiteboard, its scenes, with times.
// A shared video has no Explain: the tutor answers from its owner's sources.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type * as Rest from "@/client/types.gen";
import { apiBase, call, enc, errText, storage } from "./api";
import { citeFrom, citeGroups, type Cite } from "./cite";
import { Icon } from "./Icon";
import { mdToHtml, withChips } from "./markdown";
import { go } from "./routes";
import { type VideoStyle, videoName } from "./video";
import "../styles/watch.css";

export type Script = Rest.VideoScript;
type Mode = "ask" | "explain" | "example" | "why" | "quiz";
type Tab = "explain" | "transcript" | "chapters";

/** One turn of the thread with the tutor. */
export type Turn = {
  role: "user" | "assistant";
  text: string;
  t_ms: number;
  cites: Cite[];
  err?: boolean;
};

const QUICK: { mode: Exclude<Mode, "ask">; label: string; says: string }[] = [
  { mode: "explain", label: "Explain this part", says: "Explain this part." },
  { mode: "example", label: "Give me an example", says: "Give me an example." },
  { mode: "why", label: "Why does it matter?", says: "Why does it matter?" },
  { mode: "quiz", label: "Quiz me", says: "Quiz me on what I've watched." },
];

// ── the server ───────────────────────────────────────────────────────────────

function base(sid: string, share: string | null): string {
  return share ? `/shares/${enc(share)}/sessions/${enc(sid)}` : `/sessions/${enc(sid)}`;
}

export function scriptOf(sid: string, style: VideoStyle, share: string | null): Promise<Script> {
  return call<Script>("GET", `${base(sid, share)}/video/script?style=${style}`);
}

export function explainAt(
  sid: string,
  body: { style: VideoStyle; t_ms: number; mode: Mode; question: string; history: { role: string; text: string }[] },
): Promise<Rest.ExplainOut> {
  return call<Rest.ExplainOut>("POST", `/sessions/${enc(sid)}/video/explain`, body);
}

function captionsOf(sid: string, style: VideoStyle, share: string | null): string {
  return `${apiBase()}${base(sid, share)}/video/captions?style=${style}`;
}

// Captions over the video, on unless turned off in this browser: the voice
// is the content, and a room is not always quiet (as the slides' player).
const CAPTIONS_KEY = "watch-captions";

function captionsWanted(): boolean {
  try {
    return storage()?.getItem(CAPTIONS_KEY) !== "off";
  } catch {
    return true;
  }
}

function mediaUrl(sid: string, style: VideoStyle, share: string | null, download = false): string {
  return `${apiBase()}${base(sid, share)}/video?style=${style}${download ? "&download=true" : ""}`;
}

// ── time ─────────────────────────────────────────────────────────────────────

/** A time as the player shows it: 1:24, or 1:02:03 past the hour. */
export function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

/** The index of the item on at `t`: the last to start by then, or -1. */
export function onAt(items: { start_ms: number }[], t: number): number {
  let on = -1;
  items.forEach((it, i) => {
    if (it.start_ms <= t) on = i;
  });
  return on;
}

/** An answer's HTML with each [m:ss] a button that seeks the video there. */
export function withMoments(html: string, duration: number): string {
  return html.replace(/\[(\d{1,3}):([0-5]\d)\]/g, (whole, m: string, s: string) => {
    const t = (Number(m) * 60 + Number(s)) * 1000;
    if (t > duration) return whole;
    return `<button type="button" class="moment" data-t="${t}" title="Play from ${m}:${s}">${m}:${s}</button>`;
  });
}

/** Play, quietly: a browser that will not play without a press says so by
 * rejecting, and some return nothing at all. */
function playIt(v: HTMLVideoElement | null): void {
  try {
    void v?.play()?.catch(() => {});
  } catch {
    // Nothing to play with.
  }
}

// ── the thread, kept in this browser ─────────────────────────────────────────

const KEEP = 40;
const threadKey = (sid: string, style: VideoStyle) => `watch:${sid}:${style}`;

function loadThread(sid: string, style: VideoStyle): Turn[] {
  try {
    const raw = storage()?.getItem(threadKey(sid, style));
    const got: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(got)) return [];
    return got.flatMap((v: unknown): Turn[] => {
      if (!v || typeof v !== "object") return [];
      const o = v as Record<string, unknown>;
      if ((o.role !== "user" && o.role !== "assistant") || typeof o.text !== "string") return [];
      const cites = Array.isArray(o.cites) ? o.cites.map(citeFrom).filter((c): c is Cite => c !== null) : [];
      return [{ role: o.role, text: o.text, t_ms: Number(o.t_ms) || 0, cites }];
    });
  } catch {
    return [];
  }
}

function saveThread(sid: string, style: VideoStyle, turns: Turn[]): void {
  try {
    const kept = turns.filter((t) => !t.err).slice(-KEEP);
    storage()?.setItem(threadKey(sid, style), JSON.stringify(kept));
  } catch {
    // A full or blocked storage keeps the thread for this visit only.
  }
}

// ── the page ─────────────────────────────────────────────────────────────────

export function WatchPage({ sid, style, share }: { sid: string; style: VideoStyle; share: string | null }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [script, setScript] = useState<Script | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [tab, setTab] = useState<Tab>(share ? "transcript" : "explain");
  const [turns, setTurns] = useState<Turn[]>(() => loadThread(sid, style));
  const [asking, setAsking] = useState(false);
  // The video was playing when a question paused it: Resume is offered.
  const [paused, setPaused] = useState(false);
  const [captions, setCaptionsState] = useState(captionsWanted);
  const setCaptions = useCallback((on: boolean) => {
    setCaptionsState(on);
    try {
      storage()?.setItem(CAPTIONS_KEY, on ? "on" : "off");
    } catch {
      // Kept for this visit only.
    }
  }, []);
  // The video's own captions track, shown or hidden: in full screen too.
  const showCaptions = useCallback(() => {
    const track = videoRef.current?.textTracks?.[0];
    if (track) track.mode = captions ? "showing" : "hidden";
  }, [captions]);
  useEffect(showCaptions, [showCaptions]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable)) return;
      if (e.key === "c" || e.key === "C") setCaptions(!captions);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [captions, setCaptions]);

  useEffect(() => {
    document.body.classList.add("watch-open");
    return () => document.body.classList.remove("watch-open");
  }, []);
  useEffect(() => {
    let live = true;
    scriptOf(sid, style, share)
      .then((s) => live && setScript(s))
      .catch((e) => live && setLoadErr(errText(e)));
    return () => {
      live = false;
    };
  }, [sid, style, share]);
  useEffect(() => saveThread(sid, style, turns), [sid, style, turns]);

  const duration = script?.duration_ms ?? 0;
  const seek = useCallback((ms: number, play = true) => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = ms / 1000;
    setT(ms);
    if (play) playIt(v);
  }, []);

  const ask = (mode: Mode, question: string, says: string) => {
    if (asking || !script) return;
    const v = videoRef.current;
    const at = Math.round((v?.currentTime ?? t / 1000) * 1000);
    if (v && !v.paused) {
      v.pause();
      setPaused(true);
    }
    const history = turns.filter((x) => !x.err).map((x) => ({ role: x.role, text: x.text }));
    setTurns((xs) => [...xs, { role: "user", text: says, t_ms: at, cites: [] }]);
    setAsking(true);
    explainAt(sid, { style, t_ms: at, mode, question, history })
      .then((got) => {
        const cites = got.citations.map(citeFrom).filter((c): c is Cite => c !== null);
        setTurns((xs) => [...xs, { role: "assistant", text: got.answer, t_ms: at, cites }]);
      })
      .catch((e) => setTurns((xs) => [...xs, { role: "assistant", text: errText(e), t_ms: at, cites: [], err: true }]))
      .finally(() => setAsking(false));
  };

  const chapters = script?.chapters ?? [];
  const scenes = script?.scenes ?? [];
  const chapterOn = onAt(chapters, t);
  const sceneOn = onAt(scenes, t);
  const where = scenes[sceneOn]?.title || chapters[chapterOn]?.title || "";

  const back = () => {
    if (history.length > 1) history.back();
    else go({ kind: "mine" });
  };

  return (
    <div className="watch-page">
      <header className="w-top">
        <button className="w-back" title="Back" aria-label="Back" onClick={back}>
          <Icon name="chevron-left" />
        </button>
        <div className="w-titles">
          <div className="w-title">{script?.title ?? ""}</div>
          <div className="w-sub">
            {videoName({ style, theme: script?.theme })}
            {duration > 0 && ` · ${clock(duration)}`}
            {chapters.length > 0 && ` · ${chapters.length} chapters`}
          </div>
        </div>
        <a className="w-icon" href={mediaUrl(sid, style, share, true)} title="Download" aria-label="Download">
          <Icon name="download" />
        </a>
      </header>

      <main className="w-left">
        <div className="w-stage">
          <video
            ref={videoRef}
            className="w-video"
            controls
            preload="metadata"
            crossOrigin="use-credentials"
            src={mediaUrl(sid, style, share)}
            onTimeUpdate={(e) => setT(Math.round(e.currentTarget.currentTime * 1000))}
            onSeeked={(e) => setT(Math.round(e.currentTarget.currentTime * 1000))}
            onPlay={() => {
              setPlaying(true);
              setPaused(false);
            }}
            onPause={() => setPlaying(false)}
            onLoadedMetadata={showCaptions}
          >
            <track kind="captions" src={captionsOf(sid, style, share)} srcLang="en" label="Captions"
              default={captions} onLoad={showCaptions} />
          </video>
        </div>
        {chapters.length > 0 && duration > 0 ? (
          <Strip chapters={chapters} duration={duration} t={t} on={chapterOn} seek={seek}
            captions={captions} setCaptions={setCaptions} />
        ) : (
          <div className="w-under">
            <span className="grow" />
            <CaptionsButton on={captions} set={setCaptions} />
          </div>
        )}
        {loadErr !== "" && (
          <div className="w-err" role="alert">
            {loadErr}
          </div>
        )}
      </main>

      <aside className="w-rail">
        <div className="w-tabs" role="tablist">
          {!share && (
            <TabButton id="explain" tab={tab} setTab={setTab} icon="stars">
              Explain
            </TabButton>
          )}
          <TabButton id="transcript" tab={tab} setTab={setTab} icon="chat-dots">
            Transcript
          </TabButton>
          <TabButton id="chapters" tab={tab} setTab={setTab} icon="list-ul">
            Chapters
          </TabButton>
        </div>
        {tab === "explain" && !share && (
          <Explain
            turns={turns}
            asking={asking}
            ready={script !== null}
            at={t}
            where={where}
            duration={duration}
            paused={paused && !playing}
            onAsk={ask}
            onSeek={seek}
            onResume={() => playIt(videoRef.current)}
            onClear={() => setTurns([])}
          />
        )}
        {tab === "transcript" && <Transcript lines={script?.lines ?? []} t={t} seek={seek} />}
        {tab === "chapters" && <Chapters chapters={chapters} scenes={scenes} t={t} seek={seek} />}
      </aside>
    </div>
  );
}

function TabButton({ id, tab, setTab, icon, children }: {
  id: Tab;
  tab: Tab;
  setTab: (t: Tab) => void;
  icon: string;
  children: string;
}) {
  return (
    <button className={tab === id ? "w-tab on" : "w-tab"} role="tab" aria-selected={tab === id}
      onClick={() => setTab(id)}>
      <Icon name={icon} />
      {children}
    </button>
  );
}

/** The chapters under the video: one track cut into a segment per chapter,
 * each as long as its chapter and filling as it plays (a hover names it, a
 * click goes there), with the chapter playing named once beneath it. Never
 * every title at once: eight titles in a row's width is a pile of words. */
function Strip({ chapters, duration, t, on, seek, captions, setCaptions }: {
  chapters: Script["chapters"];
  duration: number;
  t: number;
  on: number;
  seek: (ms: number) => void;
  captions: boolean;
  setCaptions: (on: boolean) => void;
}) {
  const now = chapters[on];
  return (
    <div className="w-chapters">
      <div className="w-track" role="group" aria-label="Chapters">
        {chapters.map((c, i) => {
          const len = Math.max(c.end_ms - c.start_ms, 1);
          const done = Math.min(Math.max((t - c.start_ms) / len, 0), 1);
          return (
            <button key={i} className={i === on ? "w-seg on" : "w-seg"} style={{ flexGrow: len }}
              aria-label={`${c.title}, at ${clock(c.start_ms)}`} onClick={() => seek(c.start_ms)}>
              <span className="w-seg-fill" style={{ width: `${done * 100}%` }} />
              <span className="w-seg-tip" aria-hidden="true">
                <b>{clock(c.start_ms)}</b> {c.title}
              </span>
            </button>
          );
        })}
      </div>
      <div className="w-under">
        {now && (
          <span className="w-chap-now">
            <span className="w-chap-n">{`Chapter ${on + 1} of ${chapters.length}`}</span>
            <span className="w-chap-t">{now.title}</span>
          </span>
        )}
        <span className="grow" />
        <span className="w-clock">{`${clock(t)} / ${clock(duration)}`}</span>
        <CaptionsButton on={captions} set={setCaptions} />
      </div>
    </div>
  );
}

function CaptionsButton({ on, set }: { on: boolean; set: (on: boolean) => void }) {
  return (
    <button className={on ? "w-cc on" : "w-cc"} aria-pressed={on} title="Captions (C)"
      aria-label="Captions" onClick={() => set(!on)}>
      <Icon name="badge-cc" />
    </button>
  );
}

function Explain({ turns, asking, ready, at, where, duration, paused, onAsk, onSeek, onResume, onClear }: {
  turns: Turn[];
  asking: boolean;
  ready: boolean;
  at: number;
  where: string;
  duration: number;
  paused: boolean;
  onAsk: (mode: Mode, question: string, says: string) => void;
  onSeek: (ms: number) => void;
  onResume: () => void;
  onClear: () => void;
}) {
  const [text, setText] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns.length, asking]);
  const send = () => {
    const q = text.trim();
    if (!q || asking || !ready) return;
    setText("");
    onAsk("ask", q, q);
  };
  // A moment in an answer is a button: a click seeks the video there.
  const onClick = (e: React.MouseEvent) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>("button.moment");
    if (b?.dataset.t) onSeek(Number(b.dataset.t));
  };
  return (
    <div className="w-explain">
      <div ref={logRef} className="w-log" role="log" aria-label="Questions and answers" onClick={onClick}>
        {turns.length === 0 && (
          <div className="w-hello">
            <Icon name="stars" />
            <p>
              Pause anywhere and ask. The tutor knows what was just said and drawn, and answers from your
              sources.
            </p>
          </div>
        )}
        {turns.map((m, i) => (
          <Answer key={i} m={m} duration={duration} onSeek={onSeek} />
        ))}
        {asking && (
          <div className="w-msg tutor">
            <span className="w-typing" aria-label="Thinking">
              <i />
              <i />
              <i />
            </span>
          </div>
        )}
      </div>
      <div className="w-ask">
        <div className="w-now">
          <span className="w-now-chip" title="The moment the tutor is asked about">
            {`At ${clock(at)}`}
            {where && <span className="w-now-where">{` · ${where}`}</span>}
          </span>
          {paused && (
            <button className="w-resume" onClick={onResume}>
              <Icon name="play-fill" />
              Resume
            </button>
          )}
          <span className="grow" />
          {turns.length > 0 && (
            <button className="w-clear" title="Clear the conversation" onClick={onClear}>
              Clear
            </button>
          )}
        </div>
        <div className="w-quick">
          {QUICK.map((q) => (
            <button key={q.mode} className="w-q" disabled={asking || !ready} onClick={() => onAsk(q.mode, "", q.says)}>
              {q.label}
            </button>
          ))}
        </div>
        <div className="w-composer">
          <textarea
            rows={1}
            placeholder="Ask about this moment…"
            aria-label="Ask about this moment"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          <button className="w-send" aria-label="Ask" title="Ask" disabled={asking || !ready || !text.trim()}
            onClick={send}>
            <Icon name="send-fill" />
          </button>
        </div>
      </div>
    </div>
  );
}

function Answer({ m, duration, onSeek }: { m: Turn; duration: number; onSeek: (ms: number) => void }) {
  const html = useMemo(() => {
    if (m.role === "user" || m.err) return "";
    const md = mdToHtml(m.text);
    return withMoments(m.cites.length ? withChips(md, m.cites) : md, duration);
  }, [m, duration]);
  if (m.role === "user")
    return (
      <div className="w-msg you">
        <button className="moment at" title={`Play from ${clock(m.t_ms)}`} onClick={() => onSeek(m.t_ms)}>
          {clock(m.t_ms)}
        </button>
        <div>{m.text}</div>
      </div>
    );
  if (m.err)
    return (
      <div className="w-msg tutor err" role="alert">
        {m.text}
      </div>
    );
  return (
    <div className="w-msg tutor">
      <div className="md" dangerouslySetInnerHTML={{ __html: html }} />
      {m.cites.length > 0 && (
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
      )}
    </div>
  );
}

/** The narration, following the voice. Scrolling away stops the following
 * until "Back to now". */
function Transcript({ lines, t, seek }: { lines: Script["lines"]; t: number; seek: (ms: number) => void }) {
  const on = onAt(lines, t);
  const [follow, setFollow] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!follow || on < 0) return;
    boxRef.current?.querySelector(`[data-i="${on}"]`)?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [on, follow]);
  return (
    <div className="w-pane">
      <div ref={boxRef} className="w-lines" onWheel={() => setFollow(false)} onTouchMove={() => setFollow(false)}>
        {lines.map((ln, i) => (
          <button key={i} data-i={i} className={i === on ? "w-line on" : i < on ? "w-line seen" : "w-line"}
            onClick={() => {
              setFollow(true);
              seek(ln.start_ms);
            }}>
            <span className="w-line-t">{clock(ln.start_ms)}</span>
            <span>{ln.text}</span>
          </button>
        ))}
      </div>
      {!follow && (
        <button className="w-follow" onClick={() => setFollow(true)}>
          <Icon name="arrow-down" />
          Back to now
        </button>
      )}
    </div>
  );
}

function Chapters({ chapters, scenes, t, seek }: {
  chapters: Script["chapters"];
  scenes: Script["scenes"];
  t: number;
  seek: (ms: number) => void;
}) {
  const on = onAt(chapters, t);
  const sceneOn = onAt(scenes, t);
  return (
    <div className="w-pane w-lines">
      {chapters.map((c, i) => (
        <Fragment key={`c${i}`}>
          <button className={i === on ? "w-chap on" : "w-chap"} onClick={() => seek(c.start_ms)}>
            <span className="w-line-t">{clock(c.start_ms)}</span>
            <span>{c.title}</span>
          </button>
          {scenes.map((sc, j) =>
            sc.start_ms >= c.start_ms && sc.start_ms < c.end_ms ? (
              <button key={`s${j}`} className={j === sceneOn ? "w-scene on" : "w-scene"} onClick={() => seek(sc.start_ms)}>
                <span className="w-line-t">{clock(sc.start_ms)}</span>
                <span>
                  {sc.title || "Scene"}
                  {(sc.labels ?? []).length > 0 && <span className="w-scene-l">{(sc.labels ?? []).slice(0, 4).join(" · ")}</span>}
                </span>
              </button>
            ) : null,
          )}
        </Fragment>
      ))}
      {chapters.length === 0 && <p className="w-none">This video has no chapters.</p>}
    </div>
  );
}
