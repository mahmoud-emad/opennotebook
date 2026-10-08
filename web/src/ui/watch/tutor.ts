// The thread with the tutor: asked about a moment of the video, answered
// from the sources, and kept in this browser for the next visit.

import { useEffect, useState } from "react";
import { errText, storage } from "../api";
import { citeFrom, type Cite } from "../cite";
import type { VideoStyle } from "../video";
import { explainAt, type Mode } from "./api";

/** One turn of the thread with the tutor. */
export type Turn = {
  role: "user" | "assistant";
  text: string;
  t_ms: number;
  cites: Cite[];
  err?: boolean;
};

// The most turns kept for the next visit.
const KEEP = 40;
const threadKey = (sid: string, style: VideoStyle) => `watch:${sid}:${style}`;

const citesOf = (raw: unknown[]): Cite[] => raw.map(citeFrom).filter((c): c is Cite => c !== null);

function loadThread(sid: string, style: VideoStyle): Turn[] {
  try {
    const raw = storage()?.getItem(threadKey(sid, style));
    const got: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(got)) return [];
    return got.flatMap((v: unknown): Turn[] => {
      if (!v || typeof v !== "object") return [];
      const o = v as Record<string, unknown>;
      if ((o.role !== "user" && o.role !== "assistant") || typeof o.text !== "string") return [];
      const cites = Array.isArray(o.cites) ? citesOf(o.cites) : [];
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

/** A video's thread with the tutor, and asking it about the moment `at`.
 * A failed answer shows in the thread but is neither sent back nor kept. */
export function useTutor(sid: string, style: VideoStyle) {
  const [turns, setTurns] = useState<Turn[]>(() => loadThread(sid, style));
  const [asking, setAsking] = useState(false);
  useEffect(() => saveThread(sid, style, turns), [sid, style, turns]);

  const ask = (mode: Mode, question: string, says: string, at: number) => {
    const history = turns.filter((x) => !x.err).map((x) => ({ role: x.role, text: x.text }));
    setTurns((xs) => [...xs, { role: "user", text: says, t_ms: at, cites: [] }]);
    setAsking(true);
    explainAt(sid, { style, t_ms: at, mode, question, history })
      .then((got) => {
        const cites = citesOf(got.citations);
        setTurns((xs) => [...xs, { role: "assistant", text: got.answer, t_ms: at, cites }]);
      })
      .catch((e) => setTurns((xs) => [...xs, { role: "assistant", text: errText(e), t_ms: at, cites: [], err: true }]))
      .finally(() => setAsking(false));
  };

  return { turns, asking, ask, clear: () => setTurns([]) };
}
