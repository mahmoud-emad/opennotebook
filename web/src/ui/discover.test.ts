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

  it("shows a shared source as a row with no remove, as the server words it", () => {
    const s = sharedSrc({ name: "a.pdf", title: "", url: "", chars: 600, detail: "PDF · 100 words", icon: "" });
    expect(s).toEqual({ name: "a.pdf", detail: "PDF · 100 words", ok: true, url: "", icon: "", file: "" });
    const page = { name: "x.md", title: "Page", url: "https://www.example.com/a", chars: 60 };
    const row = sharedSrc({ ...page, detail: "example.com · 10 words", icon: "https://www.example.com/favicon.ico" });
    expect([row.name, row.detail, row.icon]).toEqual(["Page", "example.com · 10 words", "https://www.example.com/favicon.ico"]);
  });
});
