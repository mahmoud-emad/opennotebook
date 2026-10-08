// Captions over the video, as the slides' player draws them: a phrase at a
// time, the words said so far bright, the word being said marked.

import { useCallback, useEffect, useState, type RefObject } from "react";
import { storage } from "../api";
import type { ScriptLine } from "./api";
import { onAt } from "./time";

// Captions are on unless turned off in this browser: the voice is the
// content, and a room is not always quiet (as the slides' player).
const CAPTIONS_KEY = "watch-captions";

function captionsWanted(): boolean {
  try {
    return storage()?.getItem(CAPTIONS_KEY) !== "off";
  } catch {
    return true;
  }
}

/** Whether captions are on, and turning them on or off for the next visit too. */
export function useCaptions(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(captionsWanted);
  const set = useCallback((next: boolean) => {
    setOn(next);
    try {
      storage()?.setItem(CAPTIONS_KEY, next ? "on" : "off");
    } catch {
      // Kept for this visit only.
    }
  }, []);
  return [on, set];
}

/** A word as said: the word, and when it starts and ends. */
type Timed = [string, number, number];

/** A line's words with their times: as the video kept them, or for a video
 * made before words were kept, its words spread evenly over the line. */
export function wordsOf(line: ScriptLine): Timed[] {
  if (line.words && line.words.length > 0) return line.words;
  const words = line.text.split(/\s+/).filter(Boolean);
  const each = (line.end_ms - line.start_ms) / Math.max(words.length, 1);
  return words.map((w, i) => [w, line.start_ms + i * each, line.start_ms + (i + 1) * each]);
}

// A caption is a phrase, not a paragraph: at most this many words, broken
// after a sentence's or a clause's end where one falls near.
const CAPTION_WORDS = 12;

/** A line's words cut into captions, as index ranges [from, to). */
export function phrases(words: string[]): [number, number][] {
  const out: [number, number][] = [];
  let from = 0;
  for (let i = 0; i < words.length; i++) {
    const n = i - from + 1;
    const end = /[.!?;:,]$/.test(words[i]!) && n >= 5;
    if (end || n >= CAPTION_WORDS || i === words.length - 1) {
      out.push([from, i + 1]);
      from = i + 1;
    }
  }
  return out;
}

/** The narration over the video, a phrase at a time. Its own clock reads
 * the video every frame, so a word lights as it is spoken. */
export function Captions({ lines, videoRef }: {
  lines: ScriptLine[];
  videoRef: RefObject<HTMLVideoElement | null>;
}) {
  const [now, setNow] = useState(0);
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const v = videoRef.current;
      if (v) setNow(Math.round(v.currentTime * 1000));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [videoRef]);
  const line = lines[onAt(lines, now)];
  // Nothing between lines, once the last word has been said.
  if (!line || now > line.end_ms + 600) return null;
  const words = wordsOf(line);
  const said = words.filter(([, start]) => start <= now).length - 1;
  const [from, to] = phrases(words.map(([w]) => w)).find(([, b]) => said < b) ?? [0, words.length];
  return (
    <div className="w-cap" aria-hidden="true">
      {words.slice(from, to).map(([w], k) => {
        const at = from + k;
        return (
          <span key={at} className={at < said ? "past" : at === said ? "now" : undefined}>
            {w}
          </span>
        );
      })}
    </div>
  );
}
