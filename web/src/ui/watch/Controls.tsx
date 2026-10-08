// The controls under the video, as the slides' player has them: the
// chapters as one track, then previous, play and next, the clock, the
// chapter playing, the speed, captions and full screen.

import { useCallback, useEffect, useState, type RefObject } from "react";
import { Icon } from "../Icon";
import type { Script } from "./api";
import { clock } from "./time";

// The playback speeds the speed button steps through, as the slides' player.
const SPEEDS = [1, 1.25, 1.5, 2, 0.75];

/** The speed after `now` on the speed button. */
export const nextSpeed = (now: number) => SPEEDS[(SPEEDS.indexOf(now) + 1) % SPEEDS.length]!;

/** Whether `frame` is full screen, and putting it in or taking it out. */
export function useFullScreen(frame: RefObject<HTMLElement | null>): [boolean, () => void] {
  const [full, setFull] = useState(false);
  useEffect(() => {
    const onFull = () => setFull(document.fullscreenElement === frame.current);
    document.addEventListener("fullscreenchange", onFull);
    return () => document.removeEventListener("fullscreenchange", onFull);
  }, [frame]);
  const toggle = useCallback(() => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    else void frame.current?.requestFullscreen?.().catch(() => {});
  }, [frame]);
  return [full, toggle];
}

export function Controls(props: {
  chapters: Script["chapters"];
  duration: number;
  t: number;
  /** The index of the chapter playing, or -1. */
  on: number;
  seek: (ms: number) => void;
  playing: boolean;
  togglePlay: () => void;
  step: (d: number) => void;
  speed: number;
  cycleSpeed: () => void;
  captions: boolean;
  setCaptions: (on: boolean) => void;
  full: boolean;
  toggleFull: () => void;
}) {
  const { chapters, duration, t, on, playing, speed, captions, full } = props;
  const now = chapters[on];
  return (
    <div className="w-controls">
      <Track chapters={chapters} duration={duration} t={t} on={on} seek={props.seek} />
      <div className="w-ctl-row">
        <button className="w-btn" title="Previous chapter (←)" aria-label="Previous chapter"
          disabled={chapters.length === 0} onClick={() => props.step(-1)}>
          <Icon name="skip-start-fill" />
        </button>
        <button className="w-btn w-play" title={playing ? "Pause (K)" : "Play (K)"}
          aria-label={playing ? "Pause" : "Play"} onClick={props.togglePlay}>
          <Icon name={playing ? "pause-fill" : "play-fill"} />
        </button>
        <button className="w-btn" title="Next chapter (→)" aria-label="Next chapter"
          disabled={chapters.length === 0} onClick={() => props.step(1)}>
          <Icon name="skip-end-fill" />
        </button>
        <span className="w-clock">{`${clock(t)} / ${clock(duration)}`}</span>
        <span className="w-chap-now">
          {now && (
            <>
              <b>{`Chapter ${on + 1} of ${chapters.length}`}</b>
              {` · ${now.title}`}
            </>
          )}
        </span>
        <button className="w-speed" title="Playback speed" aria-label={`Playback speed, ${speed} times`}
          onClick={props.cycleSpeed}>
          {`${speed}×`}
        </button>
        <button className={captions ? "w-btn on" : "w-btn"} aria-pressed={captions} title="Captions (C)"
          aria-label="Captions" onClick={() => props.setCaptions(!captions)}>
          <Icon name="badge-cc" />
        </button>
        <button className="w-btn" title="Full screen (F)" aria-label="Full screen" onClick={props.toggleFull}>
          <Icon name={full ? "fullscreen-exit" : "fullscreen"} />
        </button>
      </div>
    </div>
  );
}

/** The chapters as one track cut into a segment each, sized by its length
 * and filling as it plays: a hover names one, a click goes there. A video
 * with no chapters has one segment, clicked where to go. */
function Track({ chapters, duration, t, on, seek }: {
  chapters: Script["chapters"];
  duration: number;
  t: number;
  on: number;
  seek: (ms: number) => void;
}) {
  return (
    <div className="w-track" role="group" aria-label="Chapters">
      {chapters.length > 0 ? (
        chapters.map((c, i) => {
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
        })
      ) : (
        <button className="w-seg on" style={{ flexGrow: 1 }} aria-label="Progress"
          onClick={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            if (r.width > 0 && duration > 0) seek(((e.clientX - r.left) / r.width) * duration);
          }}>
          <span className="w-seg-fill" style={{ width: `${duration ? (t / duration) * 100 : 0}%` }} />
        </button>
      )}
    </div>
  );
}
