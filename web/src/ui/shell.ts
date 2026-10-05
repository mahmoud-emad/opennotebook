// The page-wide pieces any screen reports through: the error banner under the
// top bar, the notice that something worked, the snackbar, and what a
// collection card or row can make. A port of the shared half of the old app's
// `main.rs`.

import { store } from "./store";

/** The page's one error banner, under the top bar. Every action that can fail
 * without a pane of its own to say so reports here, so nothing fails silently. */
export const FLASH = store("");

/** Say that something failed, in the shared banner. */
export function report(msg: string): void {
  FLASH.set(msg);
}

/** The page's one note that something worked and changed where things are:
 * a collection shared, or reused into a new one. It sits where the error
 * banner does, says so politely, and goes by itself after a few seconds. */
export const NOTICE = store("");

/** How long a notice stays, in ms. */
export const NOTICE_MS = 6000;

/** Say that something worked, in the shared notice. */
export function notify(msg: string): void {
  NOTICE.set(msg);
  // Not tied to the caller: it is often a dialog or a page that closes the
  // moment it has said this. Only this one is cleared: a newer notice keeps
  // its own few seconds.
  setTimeout(() => {
    if (NOTICE.get() === msg) NOTICE.set("");
  }, NOTICE_MS);
}

/** A short message at the bottom of the screen that goes on its own. Each
 * message has a number, so the timer of an older one never clears a newer. */
export const SNACK = store<[number, string] | null>(null);

/** How long a snackbar stays, in ms. */
export const SNACK_MS = 6000;

export function snack(msg: string): void {
  const n = (SNACK.get()?.[0] ?? 0) + 1;
  SNACK.set([n, msg]);
}

/** What a collection can make: the four tiles of its Studio. */
export type Output = "session" | "mindmap" | "notes" | "audio";

/** The four, in the order the tiles show them. */
export const OUTPUTS: Output[] = ["session", "audio", "mindmap", "notes"];

export const outputLabel: Record<Output, string> = {
  session: "Narrated slides",
  mindmap: "Mind map",
  notes: "Study notes",
  audio: "Audio overview",
};

export const outputIcon: Record<Output, string> = {
  session: "easel",
  mindmap: "diagram-3",
  notes: "journal-text",
  audio: "soundwave",
};

/** One line on its tile: what it is. */
export const outputBlurb: Record<Output, string> = {
  session: "Slides with a spoken script you can interrupt.",
  mindmap: "The topics as a tree. Click one to ask about it.",
  notes: "Key ideas, a quiz and a glossary, all cited.",
  audio: "A conversation about your sources, to listen to.",
};

/** Time and cost, said plainly before the choice. */
export const outputHint: Record<Output, string> = {
  session: "A few minutes · tens of cents",
  mindmap: "Seconds · under a cent",
  notes: "Under a minute · about a cent or less",
  audio: "A few minutes · tens of cents",
};

/** A build in the background, as opposed to one call that answers. */
export const isBuild = (o: Output) => o === "session" || o === "audio";

/** A length of audio as m:ss. */
export function mmss(ms: number): string {
  const s = Math.floor((Math.max(ms, 0) + 500) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** A date somebody reads, not a timestamp. */
export function when(ms: number): string {
  if (ms <= 0) return "";
  const mins = Math.max(0, Math.floor((Date.now() - ms) / 60_000));
  if (mins === 0) return "just now";
  if (mins === 1) return "a minute ago";
  if (mins <= 59) return `${mins} minutes ago`;
  if (mins <= 119) return "an hour ago";
  if (mins <= 1439) return `${Math.floor(mins / 60)} hours ago`;
  if (mins <= 2879) return "yesterday";
  if (mins <= 10079) return `${Math.floor(mins / 1440)} days ago`;
  return new Date(ms).toLocaleDateString("en-GB");
}

/** Move keyboard focus to the element with this id, if there is one. */
export function focusId(id: string): void {
  document.getElementById(id)?.focus();
}
