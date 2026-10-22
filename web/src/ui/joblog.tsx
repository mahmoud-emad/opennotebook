// What a job says while it runs, as a terminal under its progress bar: one
// line a thing the work does (a phase beginning, a model asked, a scene
// recorded, a repair made), collapsed to its latest line until opened.

import { useEffect, useRef, useState } from "react";
import { getJob } from "./api-studio";
import { Icon } from "./Icon";

/** One line of a job's log, as the server says it. */
export type LogLine = {
  /** When it was said, in milliseconds since the epoch. */
  at: number;
  /** `step`: a phase beginning; `ok`: something done; `warn`: something that
   * did not go to plan but did not stop the work; `bad`: something failed. */
  level: "info" | "step" | "ok" | "warn" | "bad";
  text: string;
};

/** The mark in front of each kind of line. */
const GLYPH: Record<LogLine["level"], string> = {
  info: "›",
  step: "▸",
  ok: "✓",
  warn: "!",
  bad: "✕",
};

/** Seconds since the first line, as +m:ss. */
export function since(first: number, at: number): string {
  const s = Math.max(0, Math.round((at - first) / 1000));
  return `+${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** How close to the bottom still counts as following the log, in pixels. */
const STICK_PX = 28;

/** The log, closed to its latest line and opened to every line. While `live`
 * it follows the newest line, unless the reader scrolled up to read. */
export function JobLog({
  lines,
  live = true,
}: {
  lines: LogLine[];
  live?: boolean;
}) {
  const body = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [open, setOpen] = useState(false);
  const n = lines.length;

  useEffect(() => {
    const el = body.current;
    if (open && el && stick.current) el.scrollTop = el.scrollHeight;
  }, [open, n]);

  if (n === 0) return null;
  const first = lines[0]?.at ?? 0;
  const last = lines[n - 1];
  return (
    <details
      className={live ? "joblog live" : "joblog"}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>
        <Icon name="chevron-right" className="jl-chev" />
        <span className="jl-title">Logs</span>
        <span className="jl-n num">{n}</span>
        {!open && last && (
          <span className={`jl-last ${last.level}`}>{last.text}</span>
        )}
      </summary>
      <div
        ref={body}
        className="jl-body"
        role="log"
        aria-label="What it is doing"
        // A terminal is read by scrolling it.
        tabIndex={0}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
        }}
      >
        {lines.map((l, i) => (
          <div key={`${l.at}-${i}`} className={`jl-row ${l.level}`}>
            <span className="jl-t num">{since(first, l.at)}</span>
            <span className="jl-g" aria-hidden="true">
              {GLYPH[l.level]}
            </span>
            <span className="jl-x">{l.text}</span>
          </div>
        ))}
        {live && (
          <div className="jl-row" aria-hidden="true">
            <span className="jl-t" />
            <span className="jl-caret" />
          </div>
        )}
      </div>
    </details>
  );
}

/** A job's log as the server has it, read again while `on`: for work whose
 * progress no stream carries. Empty until the first read. */
export function useJobLog(
  jobId: string | null | undefined,
  on: boolean,
  every = 1500,
): LogLine[] {
  const [lines, setLines] = useState<LogLine[]>([]);
  useEffect(() => {
    if (!jobId || !on) return;
    const ac = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async () => {
      try {
        const j = await getJob(jobId, ac.signal);
        if (!ac.signal.aborted) setLines(j.log as LogLine[]);
      } catch {
        // Read again on the next beat.
      }
      if (!ac.signal.aborted) timer = setTimeout(read, every);
    };
    void read();
    return () => {
      ac.abort();
      if (timer) clearTimeout(timer);
    };
  }, [jobId, on, every]);
  return lines;
}
