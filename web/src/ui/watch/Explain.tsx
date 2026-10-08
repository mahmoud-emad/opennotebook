// The Explain tab: the thread with the tutor, the moment it is asked about,
// the quick prompts and a box for a question of one's own.

import { Fragment, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { citeGroups } from "../cite";
import { Icon } from "../Icon";
import { mdToHtml, withChips } from "../markdown";
import type { Mode } from "./api";
import { clock } from "./time";
import type { Turn } from "./tutor";

const QUICK: { mode: Exclude<Mode, "ask">; label: string; says: string }[] = [
  { mode: "explain", label: "Explain this part", says: "Explain this part." },
  { mode: "example", label: "Give me an example", says: "Give me an example." },
  { mode: "why", label: "Why does it matter?", says: "Why does it matter?" },
  { mode: "quiz", label: "Quiz me", says: "Quiz me on what I've watched." },
];

/** An answer's HTML with each [m:ss] a button that seeks the video there. */
export function withMoments(html: string, duration: number): string {
  return html.replace(/\[(\d{1,3}):([0-5]\d)\]/g, (whole, m: string, s: string) => {
    const t = (Number(m) * 60 + Number(s)) * 1000;
    if (t > duration) return whole;
    return `<button type="button" class="moment" data-t="${t}" title="Play from ${m}:${s}">${m}:${s}</button>`;
  });
}

export function Explain({ turns, asking, ready, at, where, duration, paused, onAsk, onSeek, onResume, onClear }: {
  turns: Turn[];
  asking: boolean;
  /** The script has come, so the tutor can be asked. */
  ready: boolean;
  /** The moment the tutor is asked about, and the chapter or scene it is in. */
  at: number;
  where: string;
  duration: number;
  /** A question paused the video: Resume is offered. */
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
  const onLogClick = (e: MouseEvent) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>("button.moment");
    if (b?.dataset.t) onSeek(Number(b.dataset.t));
  };
  return (
    <div className="w-explain">
      <div ref={logRef} className="w-log" role="log" aria-label="Questions and answers" onClick={onLogClick}>
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
          <Message key={i} m={m} duration={duration} onSeek={onSeek} />
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

/** One turn of the thread: a question with the moment it was asked at, an
 * answer with its sources, or why there was none. */
function Message({ m, duration, onSeek }: { m: Turn; duration: number; onSeek: (ms: number) => void }) {
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
