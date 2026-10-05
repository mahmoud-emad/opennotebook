import { describe, expect, it } from "vitest";
import { sharedFacts, sharedSrc } from "./discover";

const out = (kind: string, slides: number, ms: number) => ({ kind, slide_count: slides, duration_ms: ms });

describe("a shared collection", () => {
  it("says what a shared output is", () => {
    expect(sharedFacts(out("session", 6, 0))).toBe("6 slides");
    expect(sharedFacts(out("session", 6, 125_000))).toBe("6 slides · 2:05");
    expect(sharedFacts(out("audio", 0, 61_000))).toBe("Audio overview · 1:01");
    expect(sharedFacts(out("audio", 0, 0))).toBe("Audio overview");
    expect(sharedFacts(out("mindmap", 0, 0))).toBe("Mind map");
    expect(sharedFacts(out("notes", 0, 0))).toBe("Study notes");
  });

  it("shows a shared source as a row with no remove", () => {
    const s = sharedSrc("a.pdf", "", "", 600);
    expect(s).toEqual({ name: "a.pdf", detail: "PDF · 100 words", ok: true, url: "", icon: "", file: "" });
    expect(sharedSrc("x.md", "Page", "https://www.example.com/a", 60).detail).toBe("example.com · 10 words");
  });
});
