import { describe, expect, it } from "vitest";
import type * as Rest from "@/client/types.gen";
import {
  captionAt,
  captionParts,
  displayName,
  doneMs,
  extendMs,
  fills,
  fitBox,
  flatten,
  hhmmss,
  keyAction,
  languageTag,
  metaOf,
  nextSpeed,
  partsOf,
  seekTarget,
  sessionOf,
  slideKind,
  soundsUnfinished,
  stepTarget,
  toWav,
  tocLine,
  totalMs,
  transcript,
  voiceName,
  type Msg,
} from "./playerModel";
import { sseFrames } from "./sse";

const line = (id: string, spk: string, ms: number | null, text = `Line ${id}.`, ordinal?: number) => ({
  line_id: id,
  speaker_id: spk,
  text,
  duration_ms: ms,
  ...(ordinal === undefined ? {} : { ordinal }),
});

function detail(over: Partial<Rest.SessionDetail> = {}): Rest.SessionDetail {
  return {
    id: "s1",
    collection_id: "c1",
    kind: "slides",
    title: "Reefs",
    description: "",
    state: "ready",
    failure: null,
    parts: 2,
    speakers: 2,
    audio_format: "",
    duration_ms: 0,
    pinned: false,
    spent_usd: null,
    spent_known: true,
    created_at: "2026-10-05T00:00:00Z",
    style: null,
    speaker_list: [
      { speaker_id: "a", voice_id: "af_bella", display_name: "Host" },
      { speaker_id: "b", voice_id: "am_adam", display_name: "Dr Reef" },
    ],
    // Out of order on purpose: the player sorts by ordinal.
    slides: [
      { ordinal: 1, title: "", aspect: { width: 1376, height: 768 }, lines: [line("s1l0", "a", 3000)] },
      {
        ordinal: 0,
        title: "Intro",
        aspect: { width: 1920, height: 1080 },
        lines: [line("s0l1", "b", 2000, "Two.", 1), line("s0l0", "a", 1000, "One.", 0)],
      },
    ],
    audio: null,
    ...over,
  };
}

const doc = sessionOf(detail());
const flat = flatten(doc);
const parts = partsOf(doc);

describe("the session", () => {
  it("plays parts and lines in their order", () => {
    expect(flat.map((f) => f.line.line_id)).toEqual(["s0l0", "s0l1", "s1l0"]);
    expect(parts.map((p) => p.ordinal)).toEqual([0, 1]);
    expect(totalMs(flat)).toBe(6000);
  });

  it("is an audio overview by its kind or its audio", () => {
    expect(doc.audio).toBeNull();
    expect(sessionOf(detail({ kind: "audio" })).audio).toEqual({ format: "" });
    expect(sessionOf(detail({ audio: { format: "debate" } })).audio).toEqual({ format: "debate" });
  });

  it("reads what the build left out as the gap it is", () => {
    const d = sessionOf(detail({ slides: [{ lines: [{ line_id: "x" }] }], speaker_list: [] }));
    expect(d.slides[0]).toEqual({
      ordinal: 0,
      title: "",
      aspect: null,
      lines: [{ line_id: "x", speaker_id: "", ordinal: 0, text: "", duration_ms: null }],
    });
  });

  it("calls a speaker with only a role by their voice", () => {
    expect(voiceName("af_bella")).toBe("Bella");
    expect(voiceName("en-US-AvaMultilingualNeural")).toBe("Ava");
    expect(voiceName("en-GB-RyanNeural")).toBe("Ryan");
    expect(voiceName("")).toBe("");
    expect(displayName({ display_name: "Host", voice_id: "af_bella" })).toBe("Bella");
    expect(displayName({ display_name: "Dr Reef", voice_id: "am_adam" })).toBe("Dr Reef");
    expect(displayName({ display_name: "Speaker 2", voice_id: "" })).toBe("Speaker 2");
  });

  it("says what it is in one line", () => {
    expect(metaOf(doc, flat)).toBe("2 slides · Bella & Dr Reef · 00:06");
    expect(tocLine(doc, flat, 0)).toBe("Bella, Dr Reef · 00:03");
    const audio = sessionOf(detail({ kind: "audio", slides: [detail().slides[0]!] }));
    expect(metaOf(audio, flatten(audio))).toBe("1 chapter · Bella & Dr Reef · 00:03");
  });
});

describe("time", () => {
  it("counts minutes and seconds", () => {
    expect(hhmmss(0)).toBe("00:00");
    expect(hhmmss(61_499)).toBe("01:01");
    expect(hhmmss(-5)).toBe("00:00");
  });

  it("is how far through the whole session, not the line", () => {
    expect(doneMs(flat, -1, 0)).toBe(0);
    expect(doneMs(flat, 1, 0.5)).toBe(1500);
    // The line's own length caps it, except on the clock.
    expect(doneMs(flat, 1, 9)).toBe(3000);
    expect(doneMs(flat, 1, 9, false)).toBe(10000);
  });

  it("fills each part's segment by how much of it was heard", () => {
    expect(fills(flat, parts, 0)).toEqual([0, 0]);
    expect(fills(flat, parts, 1500)).toEqual([50, 0]);
    expect(fills(flat, parts, 4500)).toEqual([100, 50]);
    expect(fills(flat, parts, 99999)).toEqual([100, 100]);
  });

  it("seeks to the line and the offset a point in the session falls on", () => {
    expect(seekTarget(flat, 0)).toEqual([0, 0]);
    expect(seekTarget(flat, 1200)).toEqual([1, 200]);
    expect(seekTarget(flat, 6000)).toEqual([2, 3000]);
    expect(seekTarget([], 10)).toBeNull();
  });

  it("goes back to the start of this part before the previous one", () => {
    expect(stepTarget(flat, parts, -1, 1)).toBe(0);
    expect(stepTarget(flat, parts, 0, 1)).toBe(2);
    expect(stepTarget(flat, parts, 1, -1)).toBe(0);
    expect(stepTarget(flat, parts, 0, -1)).toBe(0);
    expect(stepTarget(flat, parts, 2, -1)).toBe(0);
    expect(stepTarget(flat, parts, 2, 1)).toBe(2);
    expect(stepTarget(flat, [], 0, 1)).toBe(-1);
  });
});

describe("captions", () => {
  it("show the sentence being said, not the paragraph", () => {
    const c = captionParts("First one. Second, longer one! Third?");
    expect(c.parts).toEqual(["First one.", "Second, longer one!", "Third?"]);
    expect(captionAt(c, 0, 3000)).toBe("First one.");
    expect(captionAt(c, 1.5, 3000)).toBe("Second, longer one!");
    expect(captionAt(c, 3, 3000)).toBe("Third?");
    expect(captionAt(c, 99, 3000)).toBe("Third?");
    // No length known: the first sentence.
    expect(captionAt(c, 2, 0)).toBe("First one.");
    expect(captionParts("No stop at the end").parts).toEqual(["No stop at the end"]);
    expect(captionParts('He said "go." Then left.').parts).toEqual(['He said "go."', "Then left."]);
  });
});

describe("the stage", () => {
  // Letterbox, do not assume 16:9: an HTML deck is 1920x1080, a PNG deck
  // 1376x768.
  it("fits the slide's own aspect into the stage", () => {
    expect(fitBox({ width: 1920, height: 1080 }, { width: 960, height: 1000 })).toMatchObject({
      scale: 0.5,
      boxWidth: 960,
      boxHeight: 540,
    });
    const png = fitBox({ width: 1376, height: 768 }, { width: 2000, height: 384 });
    expect(png.scale).toBe(0.5);
    expect(png.boxWidth).toBe(688);
    expect(fitBox(null, { width: 192, height: 108 }).scale).toBeCloseTo(0.1);
    expect(fitBox(null, { width: 0, height: 0 }).boxWidth).toBe(0);
  });

  it("picks the render by content type, not by the body", () => {
    expect(slideKind("image/png")).toBe("img");
    expect(slideKind("image/jpeg; q=1")).toBe("img");
    expect(slideKind("text/html; charset=utf-8")).toBe("html");
    expect(slideKind(null)).toBe("html");
  });
});

describe("the transcript", () => {
  const msg = (id: string, after: number): Msg => ({ id, after, who: "You", text: id, cls: "you", live: false });

  it("puts a question where it happened, after any earlier exchange on that line", () => {
    const rows = transcript(flat, [msg("m1", 0), msg("m2", -1), msg("m3", 0), msg("m4", 1)], false);
    expect(rows.map((r) => r.key)).toEqual(["m2", "c0", "L0", "m1", "m3", "L1", "m4", "c1", "L2"]);
    const heads = rows.filter((r) => r.kind === "chap").map((r) => (r.kind === "chap" ? r.text : ""));
    expect(heads).toEqual(["1 · Intro", "2 · Slide 2"]);
    expect(transcript(flat, [], true).find((r) => r.key === "c1")).toMatchObject({ text: "2 · Chapter 2" });
  });
});

describe("keys", () => {
  const key = (k: string, more: Partial<KeyboardEvent> = {}) => ({
    key: k,
    code: k === " " ? "Space" : `Key${k.toUpperCase()}`,
    repeat: false,
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    ...more,
  });

  it("are a video player's, with Space for asking", () => {
    expect(keyAction(key("k"), "BODY", false)).toBe("playpause");
    expect(keyAction(key("K"), "BODY", false)).toBe("playpause");
    expect(keyAction({ ...key("ArrowLeft"), code: "ArrowLeft" }, "BODY", false)).toBe("prev");
    expect(keyAction({ ...key("ArrowRight"), code: "ArrowRight" }, "BODY", false)).toBe("next");
    expect(keyAction(key("c"), "BUTTON", false)).toBe("captions");
    expect(keyAction(key("f"), "BODY", false)).toBe("fullscreen");
    expect(keyAction(key(" "), "BUTTON", false)).toBe("ask");
    expect(keyAction(key("x"), "BODY", false)).toBeNull();
  });

  it("leave typing, shortcuts, held keys and the leave question alone", () => {
    expect(keyAction(key("k"), "INPUT", false)).toBeNull();
    expect(keyAction(key(" "), "TEXTAREA", false)).toBeNull();
    expect(keyAction(key("k", { metaKey: true }), "BODY", false)).toBeNull();
    expect(keyAction(key(" ", { repeat: true }), "BODY", false)).toBeNull();
    expect(keyAction(key("k"), "BODY", true)).toBeNull();
    expect(keyAction({ ...key("Escape"), code: "Escape" }, "BODY", true)).toBe("stay");
    expect(keyAction({ ...key("Escape"), code: "Escape" }, "BODY", false)).toBeNull();
  });

  it("cycle the speed through its steps", () => {
    expect([1, 1.25, 1.5, 2, 0.75].map(nextSpeed)).toEqual([1.25, 1.5, 2, 0.75, 1]);
    expect(nextSpeed(3)).toBe(1);
  });
});

describe("the conversation", () => {
  it("gives a pause that sounds unfinished longer", () => {
    expect(soundsUnfinished("what happens to the")).toBe(true);
    expect(soundsUnfinished("so I wonder, um")).toBe(true);
    expect(soundsUnfinished("why are reefs dying?")).toBe(false);
    expect(soundsUnfinished("")).toBe(false);
    expect(extendMs("quick")).toBe(1000);
    expect(extendMs("patient")).toBe(4000);
    expect(extendMs(undefined)).toBe(2000);
    expect(languageTag("French")).toBe("fr-FR");
    expect(languageTag("Klingon")).toBe("en-US");
  });

  it("sends a question as a 16 kHz mono WAV", async () => {
    const wav = toWav(new Float32Array(48000).fill(0.5), 48000);
    const b = new DataView(await wav.arrayBuffer());
    const tag = (o: number) => String.fromCharCode(b.getUint8(o), b.getUint8(o + 1), b.getUint8(o + 2), b.getUint8(o + 3));
    expect(wav.type).toBe("audio/wav");
    expect([tag(0), tag(8), tag(12), tag(36)]).toEqual(["RIFF", "WAVE", "fmt ", "data"]);
    expect(b.getUint16(22, true)).toBe(1);
    expect(b.getUint32(24, true)).toBe(16000);
    expect(b.getUint32(40, true)).toBe(16000 * 2);
    expect(b.byteLength).toBe(44 + 32000);
    expect(b.getInt16(44, true)).toBe(Math.trunc(0.5 * 0x7fff));
  });

  it("reads the answer's events across chunk boundaries", () => {
    const a = sseFrames('event: said\ndata: {"t":"Hi"}\n\nevent: audio\ndata: AAAA\n\nevent: he');
    expect(a.frames).toEqual([
      { ev: "said", data: '{"t":"Hi"}' },
      { ev: "audio", data: "AAAA" },
    ]);
    const b = sseFrames(a.rest + 'ard\ndata:  {"t": " two spaces"}\n\n');
    expect(b.frames).toEqual([{ ev: "heard", data: ' {"t": " two spaces"}' }]);
    expect(b.rest).toBe("");
  });
});
