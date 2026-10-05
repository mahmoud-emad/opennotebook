// The player's arithmetic, with no page and no audio: the session as the
// player reads it, the play order, time, the scrubber, captions, seeking, the
// transcript's order, the keys, and the words it says. A port of the plain
// functions in the old `player.html`, kept apart so each is tested.

import type * as Rest from "@/client/types.gen";

// ── the session as the player reads it ───────────────────────────────────────

export type Speaker = { speaker_id: string; voice_id: string; display_name: string };
export type Line = {
  line_id: string;
  speaker_id: string;
  ordinal: number;
  text: string;
  /** Measured from the line's WAV; null before it is voiced. */
  duration_ms: number | null;
};
export type Aspect = { width: number; height: number };
/** A slide of a deck, or a chapter of an audio overview. */
export type Part = { ordinal: number; title: string; aspect: Aspect | null; lines: Line[] };
export type SessionDoc = {
  sid: string;
  collection: string;
  title: string;
  state: "preparing" | "ready" | "failed";
  failure: string | null;
  /** An audio overview: chapters and voices, no slides. */
  audio: { format: string } | null;
  speakers: Speaker[];
  slides: Part[];
};
/** One line in play order, with the part it belongs to. */
export type Flat = { slide: Part; line: Line };

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => (v && typeof v === "object" ? (v as Obj) : {});
const str = (v: unknown): string => (typeof v === "string" ? v : "");
const int = (v: unknown, or: number): number => (typeof v === "number" && Number.isFinite(v) ? v : or);

function aspectOf(v: unknown): Aspect | null {
  const a = obj(v);
  const w = int(a.width, 0);
  const h = int(a.height, 0);
  return w > 0 && h > 0 ? { width: w, height: h } : null;
}

/** The server's output document as the player reads it. Parts and lines keep
 * their own ordinals, or take their place when they have none. */
export function sessionOf(d: Rest.SessionDetail): SessionDoc {
  const speakers = (d.speaker_list ?? []).map((raw) => {
    const s = obj(raw);
    return { speaker_id: str(s.speaker_id), voice_id: str(s.voice_id), display_name: str(s.display_name) };
  });
  const slides = (d.slides ?? []).map((raw, i): Part => {
    const s = obj(raw);
    const lines = Array.isArray(s.lines) ? (s.lines as unknown[]) : [];
    return {
      ordinal: int(s.ordinal, i),
      title: str(s.title),
      aspect: aspectOf(s.aspect),
      lines: lines.map((lr, k): Line => {
        const l = obj(lr);
        const ms = l.duration_ms;
        return {
          line_id: str(l.line_id),
          speaker_id: str(l.speaker_id),
          ordinal: int(l.ordinal, k),
          text: str(l.text),
          duration_ms: typeof ms === "number" && Number.isFinite(ms) ? ms : null,
        };
      }),
    };
  });
  const audio = d.kind === "audio" || d.audio ? { format: str(obj(d.audio).format) } : null;
  return {
    sid: d.id,
    collection: d.collection_id,
    title: d.title,
    state: d.state,
    failure: d.failure ?? null,
    audio,
    speakers,
    slides,
  };
}

/** The parts in order. */
export const partsOf = (s: SessionDoc | null): Part[] =>
  s ? [...s.slides].sort((a, b) => a.ordinal - b.ordinal) : [];

/** Every line, in play order. */
export function flatten(s: SessionDoc): Flat[] {
  const out: Flat[] = [];
  for (const slide of partsOf(s))
    for (const line of [...slide.lines].sort((a, b) => a.ordinal - b.ordinal)) out.push({ slide, line });
  return out;
}

// ── names ────────────────────────────────────────────────────────────────────

// A role word is not a name, so a speaker who only has one is called by their
// voice's first name: af_bella is Bella.
const GENERIC = new Set([
  "host",
  "expert",
  "narrator",
  "speaker",
  "speaker 1",
  "speaker 2",
  "voice",
  "presenter",
  "guest",
  "studio",
]);

export function voiceName(voice: string): string {
  const n = String(voice || "")
    .split("_")
    .slice(1)
    .join("_");
  return n ? n[0]!.toUpperCase() + n.slice(1) : "";
}

export function displayName(sp: Pick<Speaker, "display_name" | "voice_id"> | null | undefined): string {
  const name = ((sp && sp.display_name) || "").trim();
  if (!GENERIC.has(name.toLowerCase())) return name;
  return voiceName((sp && sp.voice_id) || "") || name;
}

export function speakerName(s: SessionDoc | null, id: string): string {
  const sp = (s?.speakers ?? []).find((x) => x.speaker_id === id);
  return (sp && displayName(sp)) || id || "";
}

/** 0 for the first speaker, 1 for any other: the colour of their rule. */
export function voiceIndex(s: SessionDoc, id: string): 0 | 1 {
  return s.speakers.findIndex((x) => x.speaker_id === id) > 0 ? 1 : 0;
}

export function slideName(s: Part | null | undefined, audio: boolean): string {
  return (s && s.title) || (s ? `${audio ? "Chapter" : "Slide"} ${s.ordinal + 1}` : "");
}

export const FORMATS: Record<string, string> = {
  deep_dive: "Deep Dive",
  brief: "Brief",
  critique: "Critique",
  debate: "Debate",
};

export const initialsOf = (n: string) =>
  String(n || "?")
    .split(/\s+/)
    .map((w) => w[0] || "")
    .join("")
    .slice(0, 2)
    .toUpperCase();

// ── time ─────────────────────────────────────────────────────────────────────

export function hhmmss(ms: number): string {
  const t = Math.max(0, Math.round(ms / 1000));
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

const dur = (f: Flat) => f.line.duration_ms || 0;

export const totalMs = (flat: Flat[]) => flat.reduce((a, f) => a + dur(f), 0);

/** How long one part's narration is. */
export const partMs = (flat: Flat[], ordinal: number) =>
  flat.filter((f) => f.slide.ordinal === ordinal).reduce((a, f) => a + dur(f), 0);

/** How far through the whole session: every line before the playhead, and
 * this line's audio up to its own length. */
export function doneMs(flat: Flat[], idx: number, currentSecs: number, clamp = true): number {
  let done = 0;
  for (let i = 0; i < idx && i < flat.length; i++) done += dur(flat[i]!);
  const at = (currentSecs || 0) * 1000;
  const c = idx >= 0 && idx < flat.length ? flat[idx]! : null;
  return done + (clamp ? Math.min(at, c ? dur(c) : 0) : at);
}

/** Each part's segment of the scrubber, filled by how much of it was heard,
 * as a percentage. */
export function fills(flat: Flat[], parts: Part[], done: number): number[] {
  let acc = 0;
  return parts.map((p) => {
    const ms = partMs(flat, p.ordinal);
    const pct = ms ? Math.min(1, Math.max(0, (done - acc) / ms)) : 0;
    acc += ms;
    return 100 * pct;
  });
}

/** The line and the offset into it of a point in the whole session. */
export function seekTarget(flat: Flat[], ms: number): [number, number] | null {
  let acc = 0;
  for (let i = 0; i < flat.length; i++) {
    const d = dur(flat[i]!);
    if (ms < acc + d || i === flat.length - 1) return [i, Math.max(0, ms - acc)];
    acc += d;
  }
  return null;
}

export const firstLineOf = (flat: Flat[], ordinal: number) => flat.findIndex((f) => f.slide.ordinal === ordinal);

/** Where previous or next goes. Previous goes to the start of this part first
 * when the playhead is past its first line, as every media player's does. */
export function stepTarget(flat: Flat[], parts: Part[], idx: number, dir: -1 | 1): number {
  if (!parts.length) return -1;
  const c = idx >= 0 && idx < flat.length ? flat[idx]! : null;
  if (dir < 0 && c && firstLineOf(flat, c.slide.ordinal) < idx) return firstLineOf(flat, c.slide.ordinal);
  const at = c ? parts.findIndex((s) => s.ordinal === c.slide.ordinal) : -1;
  const to = parts[Math.min(parts.length - 1, Math.max(0, at + dir))]!;
  return firstLineOf(flat, to.ordinal);
}

/** The meta line: parts, voices and length. */
export function metaOf(s: SessionDoc, flat: Flat[]): string {
  const n = s.slides.length;
  const voices = s.speakers.map(displayName).filter(Boolean);
  const unit = s.audio ? "chapter" : "slide";
  const total = totalMs(flat);
  return [`${n} ${unit}${n === 1 ? "" : "s"}`, voices.length ? voices.join(" & ") : "", total ? hhmmss(total) : ""]
    .filter(Boolean)
    .join(" · ");
}

/** A part's line in the Slides tab: its voices and length. */
export function tocLine(s: SessionDoc, flat: Flat[], ordinal: number): string {
  const lines = flat.filter((f) => f.slide.ordinal === ordinal);
  const who = lines.map((f) => speakerName(s, f.line.speaker_id)).filter((v, i, a) => a.indexOf(v) === i);
  return `${who.join(", ")} · ${hhmmss(partMs(flat, ordinal))}`;
}

// ── captions ─────────────────────────────────────────────────────────────────

/** A line cut into sentences, each with where it ends as a fraction of the
 * line. The audio carries no word timings, so a sentence's share of the line
 * is its share of the characters. */
export function captionParts(text: string): { parts: string[]; ends: number[] } {
  const parts = (text.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [text]).map((t) => t.trim()).filter(Boolean);
  const total = parts.reduce((a, t) => a + t.length, 0) || 1;
  let acc = 0;
  return { parts, ends: parts.map((t) => (acc += t.length) / total) };
}

/** The sentence being said `secs` into a line `durationMs` long. */
export function captionAt(c: { parts: string[]; ends: number[] }, secs: number, durationMs: number): string {
  const d = durationMs / 1000;
  const frac = d ? Math.min(1, (secs || 0) / d) : 0;
  let k = c.ends.findIndex((e) => frac <= e);
  if (k < 0) k = c.parts.length - 1;
  return c.parts[k] || "";
}

// ── letterboxing ─────────────────────────────────────────────────────────────

/** The slide's box inside the stage: the slide's own aspect, scaled to fit,
 * never an assumed one. An HTML deck is 1920x1080; a PNG deck 1376x768. */
export function fitBox(aspect: Aspect | null, stage: { width: number; height: number }) {
  const w = aspect && aspect.width ? aspect.width : 1920;
  const h = aspect && aspect.height ? aspect.height : 1080;
  const scale = Math.max(0, Math.min(stage.width / w, stage.height / h));
  return { width: w, height: h, scale, boxWidth: w * scale, boxHeight: h * scale };
}

/** How a slide's response is drawn, by its content type, not its body: an
 * HTML deck and a PNG deck are both a 200 with bytes. */
export function slideKind(contentType: string | null): "img" | "html" {
  const kind = (contentType || "").split(";")[0]!.trim();
  return kind === "image/png" || kind === "image/jpeg" ? "img" : "html";
}

// ── the transcript ───────────────────────────────────────────────────────────

/** A question, an answer or a notice in the transcript. `after` is the line
 * it happened after; -1 is before the first. */
export type Msg = {
  id: string;
  after: number;
  who: string;
  text: string;
  cls: string;
  /** A blinking caret while the text is still arriving. */
  live: boolean;
  title?: string;
};

export type Row =
  | { kind: "chap"; key: string; text: string }
  | { kind: "line"; key: string; i: number }
  | { kind: "msg"; key: string; msg: Msg };

/** The transcript in order: a heading per part, each line, and each message
 * straight after the line it happened on and after any earlier exchange on
 * it, before the next line or heading. */
export function transcript(flat: Flat[], msgs: Msg[], audio: boolean): Row[] {
  const rows: Row[] = [];
  const at = (i: number) => msgs.filter((m) => m.after === i).forEach((m) => rows.push({ kind: "msg", key: m.id, msg: m }));
  at(-1);
  let last = -1;
  flat.forEach((f, i) => {
    if (f.slide.ordinal !== last) {
      last = f.slide.ordinal;
      rows.push({ kind: "chap", key: `c${last}`, text: `${last + 1} · ${slideName(f.slide, audio)}` });
    }
    rows.push({ kind: "line", key: `L${i}`, i });
    at(i);
  });
  // A message placed on a line that is gone is still shown, at the end.
  msgs.filter((m) => m.after >= flat.length).forEach((m) => rows.push({ kind: "msg", key: m.id, msg: m }));
  return rows;
}

// ── speed and keys ───────────────────────────────────────────────────────────

export const SPEEDS = [1, 1.25, 1.5, 2, 0.75];

export function nextSpeed(speed: number): number {
  return SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length]!;
}

export type KeyAction = "playpause" | "prev" | "next" | "captions" | "fullscreen" | "ask" | "stay" | null;

/** A video player's keys: K plays and pauses, the arrows move a part, C is
 * captions, F is full screen, Space is the microphone, and Escape closes the
 * leave question. Nothing is taken from a field being typed in or from a
 * shortcut with a modifier. */
export function keyAction(
  e: { key: string; code: string; repeat: boolean; ctrlKey: boolean; metaKey: boolean; altKey: boolean },
  tag: string,
  confirmOpen: boolean,
): KeyAction {
  if (e.key === "Escape" && confirmOpen) return "stay";
  if (tag === "INPUT" || tag === "TEXTAREA") return null;
  if (e.code === "Space") return e.repeat ? null : "ask";
  if (e.ctrlKey || e.metaKey || e.altKey || confirmOpen) return null;
  const k = e.key.toLowerCase();
  if (k === "k") return "playpause";
  if (e.key === "ArrowLeft") return "prev";
  if (e.key === "ArrowRight") return "next";
  if (k === "c") return "captions";
  if (k === "f") return "fullscreen";
  return null;
}

// ── the conversation's settings and words ────────────────────────────────────

/** The browser recogniser's tag for the studio's output language. */
const LANGUAGE_TAGS: Record<string, string> = {
  English: "en-US",
  Arabic: "ar-EG",
  Chinese: "zh-CN",
  Dutch: "nl-NL",
  French: "fr-FR",
  German: "de-DE",
  Hindi: "hi-IN",
  Italian: "it-IT",
  Japanese: "ja-JP",
  Portuguese: "pt-BR",
  Spanish: "es-ES",
};

export const languageTag = (name: string | undefined) => LANGUAGE_TAGS[name || ""] ?? "en-US";

/** How long a pause that sounds unfinished is given, by the patience setting. */
export function extendMs(patience: string | undefined): number {
  if (patience === "quick") return 1000;
  if (patience === "patient") return 4000;
  return 2000;
}

// Semantic end of turn: a pause after "and", "the" or "um" is someone
// thinking, not someone done.
const UNFINISHED = new Set(
  (
    "and or but so because the a an of to in on at for with from about " +
    "like um uh uhm erm hmm er is are was were what how why which that this my your their our " +
    "if then when where who can could would should do does did will than as into i we you"
  ).split(" "),
);

export function soundsUnfinished(text: string): boolean {
  const w = (text || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z' ]/g, "")
    .split(/\s+/)
    .filter(Boolean);
  return w.length > 0 && UNFINISHED.has(w[w.length - 1]!);
}

/** The six phases a build runs, as the card says them. */
export const PHASE_LABEL: Record<string, string> = {
  ingest: "reading your resources",
  script: "writing the script",
  deck: "building the slides",
  measure: "measuring the narration",
  narrate: "recording the voices",
  validate: "checking it renders",
};

export const say = {
  blocked: "The browser blocked playback. Press play again.",
  cannotPlay: "This part could not be played. Press play to try again; if it keeps failing, reload the page.",
  tooShort: (secs: number) =>
    `That recording was ${secs.toFixed(1)}s of audio, too short to be a question. ` +
    `Nothing was sent. Try holding the mic a moment longer.`,
  silent: (inputMs: number) =>
    `I could not hear any speech in that ${(inputMs / 1000).toFixed(1)}s recording, so I did not answer it. ` +
    `If you did speak, the microphone may be picking up the room instead of you.`,
  noAnswer: (why: string) => `I could not answer that. ${why}`,
  degraded:
    "Voice detection did not load, so the microphone listens for loudness instead. " +
    "In a noisy room your question may not send on its own: press send when you are done. " +
    "Reloading the page usually brings it back.",
  sharedAsk: "Asking out loud works in your own collections. Reuse this collection, then ask there.",
  mic: (name: string | undefined) =>
    name === "NotAllowedError" || name === "SecurityError"
      ? "The microphone is blocked. Allow it for this page in the browser's address bar, then try again."
      : name === "NotFoundError" || name === "OverconstrainedError"
        ? "No microphone was found. Connect one, then try again."
        : name === "NotReadableError"
          ? "The microphone is in use by another app. Close it there, then try again."
          : "The microphone could not be started. Reload the page and try again.",
  leaveWhy: (ordinal: number, n: number) =>
    `You are on slide ${ordinal + 1} of ${n}. Your place is not kept, so coming back starts from the beginning.`,
};

// ── the bytes of a question and an answer ────────────────────────────────────

/** 16 kHz mono WAV, decimated from whatever the device gave. This is speech
 * for recognition, so size on the way up matters more than fidelity. */
export function toWav(samples: Float32Array, rateIn: number): Blob {
  const rateOut = 16000;
  const ratio = rateIn / rateOut;
  const outLen = Math.floor(samples.length / ratio);
  const pcm = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const v = samples[Math.floor(i * ratio)] || 0;
    pcm[i] = Math.max(-1, Math.min(1, v)) * 0x7fff;
  }
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const dv = new DataView(buf);
  const text = (off: number, t: string) => {
    for (let i = 0; i < t.length; i++) dv.setUint8(off + i, t.charCodeAt(i));
  };
  text(0, "RIFF");
  dv.setUint32(4, 36 + pcm.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true);
  dv.setUint16(22, 1, true);
  dv.setUint32(24, rateOut, true);
  dv.setUint32(28, rateOut * 2, true);
  dv.setUint16(32, 2, true);
  dv.setUint16(34, 16, true);
  text(36, "data");
  dv.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  return new Blob([buf], { type: "audio/wav" });
}

export function b64ToPcm16(b64: string): Int16Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Int16Array(bytes.buffer, 0, bytes.length >> 1);
}

/** Complete server-sent event frames out of `buf`, and what is left over.
 * Only one space after `data:` is the field's; the payload keeps the rest. */
export function sseFrames(buf: string): { frames: { ev: string | null; data: string }[]; rest: string } {
  const frames: { ev: string | null; data: string }[] = [];
  let cut: number;
  while ((cut = buf.indexOf("\n\n")) >= 0) {
    const frame = buf.slice(0, cut);
    buf = buf.slice(cut + 2);
    let ev: string | null = null;
    const data: string[] = [];
    for (const ln of frame.split("\n")) {
      if (ln.startsWith("event:")) ev = ln.slice(6).trim();
      else if (ln.startsWith("data:")) data.push(ln.slice(5).replace(/^ /, ""));
    }
    frames.push({ ev, data: data.join("\n") });
  }
  return { frames, rest: buf };
}
