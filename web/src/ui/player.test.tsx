// The play page, drawn against a stubbed studio, and the rules the old page's
// tests (`player.rs`) kept: its controls, its icons, where its addresses come
// from, how it letterboxes, how it captures, and that a turn never vanishes
// without a word.

import { readFileSync } from "node:fs";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ICONS } from "./icons";
import { PlayerPage } from "./player";
import { episodeUrl, headsFor, lineAudioUrl, outputPath, slideUrl, voiceUrl } from "./playerApi";
import engineSrc from "./playerEngine.ts?raw";
import { say } from "./playerModel";
import pageSrc from "./player.tsx?raw";
import voiceSrc from "./playerVoice.ts?raw";
import { CAPTURE_WORKLET_SRC } from "./playerVoice";
import { parseRoute, routePath } from "./routes";

// Read from disk: vitest blanks a stylesheet imported as `?raw`.
const cssSrc = readFileSync("src/styles/player.css", "utf8");

const SESSION = {
  id: "s1",
  collection_id: "c1",
  kind: "slides",
  title: "Coral reefs",
  description: "",
  state: "ready",
  failure: null,
  parts: 2,
  speakers: 2,
  audio_format: "",
  duration_ms: 5000,
  pinned: false,
  spent_usd: null,
  spent_known: true,
  created_at: "2026-10-05T00:00:00Z",
  style: null,
  speaker_list: [
    { speaker_id: "a", voice_id: "af_bella", display_name: "Host" },
    { speaker_id: "b", voice_id: "am_adam", display_name: "Expert" },
  ],
  slides: [
    {
      ordinal: 0,
      title: "Intro",
      aspect: { width: 1920, height: 1080 },
      lines: [
        { line_id: "s0l0", speaker_id: "a", ordinal: 0, text: "Reefs are alive.", duration_ms: 2000 },
        { line_id: "s0l1", speaker_id: "b", ordinal: 1, text: "They are.", duration_ms: 1000 },
      ],
    },
    {
      ordinal: 1,
      title: "Threats",
      aspect: { width: 1920, height: 1080 },
      lines: [{ line_id: "s1l0", speaker_id: "a", ordinal: 0, text: "Heat bleaches them.", duration_ms: 2000 }],
    },
  ],
  audio: null,
};

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

let calls: string[] = [];

function stub(session: unknown, status = 200) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(String(url));
      if (String(url).includes("/slides/"))
        return new Response("<p>the slide</p>", { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
      return json(status, session);
    }),
  );
}

beforeEach(() => {
  window.history.replaceState(null, "", "/ui/play/s1");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  try {
    localStorage.clear();
  } catch {
    // No storage.
  }
});

describe("the play page", () => {
  it("has a video player's controls, a transcript and the slide in srcdoc", async () => {
    stub(SESSION);
    const { container } = render(<PlayerPage sid="s1" share={null} />);
    await screen.findByText("Reefs are alive.");
    for (const id of ["playpause", "prev", "next", "timeline", "cap", "toc", "poster", "endcard"])
      expect(container.querySelector(`#${id}`), id).not.toBeNull();
    // One play/pause, not separate play and pause buttons.
    expect(container.querySelector("#play")).toBeNull();
    expect(container.querySelector("#title")!.textContent).toBe("Coral reefs");
    expect(container.querySelector("#sub")!.textContent).toBe("2 slides · Bella & Adam · 00:05");
    // A speaker with only a role is called by their voice.
    expect(container.querySelector("#L0 .nm")!.textContent).toBe("Bella");
    expect([...container.querySelectorAll(".chap")].map((c) => c.textContent)).toEqual(["1 · Intro", "2 · Threats"]);
    expect(container.querySelectorAll("#timeline .seg")).toHaveLength(2);
    // Ready: the poster is up, the card is gone, and the slide is drawn.
    expect(container.querySelector("#poster")!.hasAttribute("hidden")).toBe(false);
    expect(container.querySelector("#prep")!.hasAttribute("hidden")).toBe(true);
    expect(container.querySelector<HTMLButtonElement>("#ask")!.disabled).toBe(false);
    await waitFor(() => expect(container.querySelector("#slide")!.getAttribute("srcdoc")).toBe("<p>the slide</p>"));
    expect(calls).toContain("/api/sessions/s1");
    expect(calls).toContain("/api/sessions/s1/slides/0");
    // The player is a page of its own: no app header.
    expect(container.querySelector(".nav")).toBeNull();
  });

  it("toggles captions on C and keeps the choice", async () => {
    stub(SESSION);
    const { container } = render(<PlayerPage sid="s1" share={null} />);
    await screen.findByText("Reefs are alive.");
    expect(container.querySelector("#app")!.classList.contains("no-cap")).toBe(false);
    act(() => void fireEvent.keyDown(window, { key: "c", code: "KeyC" }));
    expect(container.querySelector("#app")!.classList.contains("no-cap")).toBe(true);
    expect(container.querySelector("#cc")!.getAttribute("aria-pressed")).toBe("false");
    expect(localStorage.getItem("opennotebook_cc")).toBe("0");
  });

  it("wears an audio overview's words and offers its download", async () => {
    stub({ ...SESSION, kind: "audio", audio: { format: "debate", length: "default", focus: "" } });
    const { container } = render(<PlayerPage sid="s1" share={null} />);
    await screen.findByText("Reefs are alive.");
    expect(container.querySelector(".player-page")!.classList.contains("audio")).toBe(true);
    expect(container.querySelector("#as-fmt")!.textContent).toBe("Debate · audio overview");
    expect(container.querySelector('.tab[data-pane="toc"]')!.textContent).toBe("Chapters");
    expect(container.querySelector("#prev")!.getAttribute("aria-label")).toBe("Previous chapter");
    expect(container.querySelector("#dl")!.getAttribute("href")).toBe("/api/sessions/s1/episode");
    expect(container.querySelector("#endcard h2")!.textContent).toBe("That's the episode");
    expect(container.querySelector("#slide")!.hasAttribute("hidden")).toBe(true);
    expect(calls.some((c) => c.includes("/slides/"))).toBe(false);
  });

  it("says why when the output cannot be opened", async () => {
    stub({ detail: "That output is no longer there. Reload the page to see what is." }, 404);
    const { container } = render(<PlayerPage sid="s1" share={null} />);
    await screen.findByText("That output is no longer there. Reload the page to see what is.");
    expect(container.querySelector("#bar")!.classList.contains("idle")).toBe(false);
    expect(container.querySelector<HTMLButtonElement>("#playpause")!.disabled).toBe(true);
  });

  it("says a failed build could not be made", async () => {
    stub({ ...SESSION, state: "failed", failure: "The AI account is out of credit." });
    render(<PlayerPage sid="s1" share={null} />);
    await screen.findByText("This could not be made");
    screen.getByText("Go back to the studio to see why, then try again.");
  });

  it("reads a shared output through its share, and asks nothing of a share", async () => {
    stub(SESSION);
    const { container } = render(<PlayerPage sid="s1" share="sh1" />);
    await screen.findByText("Reefs are alive.");
    expect(calls).toContain("/api/shares/sh1/sessions/s1");
    await waitFor(() => expect(calls).toContain("/api/shares/sh1/sessions/s1/slides/0"));
    fireEvent.click(container.querySelector("#ask")!);
    await screen.findByText(say.sharedAsk);
  });
});

describe("addresses", () => {
  it("come from the page's own mount, never an absolute /api", () => {
    window.history.replaceState(null, "", "/studio/ui/play/s1");
    const mine = { sid: "s1", share: null };
    const theirs = { sid: "s1", share: "sh1" };
    expect(lineAudioUrl(mine, "s0 l0")).toBe("/studio/api/sessions/s1/audio/s0%20l0");
    expect(slideUrl(theirs, 2)).toBe("/studio/api/shares/sh1/sessions/s1/slides/2");
    expect(episodeUrl(theirs)).toBe("/studio/api/shares/sh1/sessions/s1/episode");
    expect(voiceUrl("s1", new URLSearchParams({ line: "x" }))).toBe("/studio/api/sessions/s1/voice?line=x");
    expect(outputPath(mine)).toBe("/sessions/s1");
    for (const src of [pageSrc, engineSrc, voiceSrc]) expect(src).not.toMatch(/["`]\/api\//);
  });

  it("route /ui/play/<sid>, with a share in the query", () => {
    expect(parseRoute("/play/s1")).toEqual({ kind: "play", sid: "s1", share: null });
    expect(parseRoute("/play/s1/", "?share=sh1")).toEqual({ kind: "play", sid: "s1", share: "sh1" });
    expect(parseRoute("/play/s1", "?share=a%20b")).toEqual({ kind: "play", sid: "s1", share: null });
    expect(parseRoute("/play/")).toEqual({ kind: "discover" });
    expect(routePath({ kind: "play", sid: "s1", share: null })).toBe("/ui/play/s1");
    expect(routePath({ kind: "play", sid: "s1", share: "sh1" })).toBe("/ui/play/s1?share=sh1");
  });

  it("keep a shared output's place in this browser, never on the server", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const heads = headsFor({ sid: "s1", share: "sh1" });
    expect((await heads.get()).state).toBe("idle");
    await heads.put({ slide_ordinal: 1, line_id: "s1l0", offset_ms: 3477, state: "paused" });
    expect(await heads.get()).toEqual({ slide_ordinal: 1, line_id: "s1l0", offset_ms: 3477, state: "paused" });
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("what the old page's tests kept", () => {
  it("uses the studio's icons, every one of them there, and no emoji", () => {
    const names = [...pageSrc.matchAll(/<Icon name="([a-z0-9-]+)"/g)].map((m) => m[1]!);
    for (const n of ["mic-fill", "send-fill", "x-lg", "badge-cc", "collection-play"]) expect(names).toContain(n);
    for (const n of names) expect(ICONS[n], n).toBeDefined();
    for (const n of ["pause-fill", "play-fill", "fullscreen", "fullscreen-exit"]) expect(ICONS[n], n).toBeDefined();
    for (const src of [pageSrc, cssSrc]) {
      expect(src).not.toMatch(/\p{Extended_Pictographic}/u);
      expect(src).not.toContain("cdn.");
    }
  });

  it("letterboxes rather than assuming 16:9", () => {
    for (const src of [pageSrc, engineSrc, cssSrc]) {
      expect(src).not.toContain("16/9");
      expect(src).not.toContain("56.25%");
    }
  });

  it("scopes every rule to the player, so the app's pages are untouched", () => {
    expect(cssSrc.length).toBeGreaterThan(5000);
    const selectors = cssSrc
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/@keyframes[^{]*\{(?:[^{}]*\{[^}]*\})*[^}]*\}/g, "")
      .replace(/@media[^{]*\{/g, "")
      .match(/[^{}]+(?=\{)/g)!
      // Commas outside parentheses: `:is(a, b)` is one selector.
      .flatMap((s) => s.split(/,(?![^(]*\))/))
      .map((s) => s.trim())
      .filter(Boolean);
    for (const sel of selectors) expect(sel.startsWith(".player-page") || sel === "body.player-open", sel).toBe(true);
  });

  it("has no reaction bar", () => {
    expect(pageSrc).not.toContain('className="react"');
    expect(cssSrc).not.toContain("floater");
  });

  it("captures in an AudioWorklet that never reaches the speakers", () => {
    expect(voiceSrc).toContain("audioWorklet.addModule");
    expect(voiceSrc).not.toContain("createScriptProcessor");
    expect(voiceSrc).toContain("src.connect(node)");
    expect(voiceSrc).not.toMatch(/\bnode\.connect\(/);
    expect(CAPTURE_WORKLET_SRC).not.toContain("`");
    expect(CAPTURE_WORKLET_SRC).toContain('registerProcessor("opennotebook-mic"');
  });

  it("opens the microphone exactly once a turn, and releases it in one place", () => {
    const at = engineSrc.indexOf("async askStart");
    const body = engineSrc.slice(at, engineSrc.indexOf("\n  private listen", at));
    expect(body.match(/this\.mic\.start\(\)/g)).toHaveLength(1);
    const start = voiceSrc.slice(voiceSrc.indexOf("async start()"));
    expect(start.indexOf("this.teardown();")).toBeLessThan(start.indexOf("getUserMedia"));
  });

  it("loads the detector only when someone asks", () => {
    for (const src of [pageSrc, engineSrc]) expect(src).not.toMatch(/from "@ricky0123|from "onnxruntime/);
    expect(voiceSrc).not.toMatch(/^import .*(@ricky0123|onnxruntime)/m);
    expect(voiceSrc).toContain('import("@ricky0123/vad-web/dist/models/silero")');
  });

  it("never lets a turn vanish without a word", () => {
    expect(say.tooShort(0.2)).toContain("too short to be a question");
    expect(say.silent(1500)).toContain("could not hear any speech in that 1.5s recording");
    expect(say.noAnswer("This is not available in the new studio yet.")).toBe(
      "I could not answer that. This is not available in the new studio yet.",
    );
    // A detector that did not load says so on the page, not only in a console.
    expect(engineSrc).toContain('this.appendMsg({ who: "Studio", cls: "err", text: say.degraded })');
    expect(say.degraded).toContain("Voice detection did not load");
  });
});
