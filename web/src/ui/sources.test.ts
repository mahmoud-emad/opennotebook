import { describe, expect, it } from "vitest";
import { FETCHING, fileKind, origin, shortHost, srcFrom } from "./sources";

// Ported from the old app's main.rs source_kind_tests.
describe("sources", () => {
  it("says what a source without a page is", () => {
    expect(fileKind("my-note.md")).toBe("note");
    expect(fileKind("Report.pdf")).toBe("PDF");
    expect(fileKind("deck.pptx")).toBe("PPTX");
    expect(fileKind("plain")).toBe("note");
  });

  it("names a page by its site", () => {
    expect(shortHost("https://www.example.org/a/b")).toBe("example.org");
    expect(origin("https://www.example.org/a/b")).toBe("https://www.example.org");
  });

  it("turns what an add answered into a row", () => {
    const ok = srcFrom({ ok: true, url: "https://x.org/p", title: "", chars: 600, name: "x.md" });
    expect(ok).toMatchObject({ ok: true, name: "https://x.org/p", detail: "x.org · 100 words", file: "x.md" });
    const bad = srcFrom({ ok: false, url: "https://x.org/p", error: "" });
    expect(bad.ok).toBe(false);
    expect(bad.detail).not.toBe(FETCHING);
    expect(bad.detail.endsWith(".")).toBe(true);
  });
});
