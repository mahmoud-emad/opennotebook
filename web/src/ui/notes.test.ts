import { describe, expect, it } from "vitest";
import type { StudyNotesSummary } from "./api-studio";
import { citedInline, counts, coveringNotes } from "./notes";

// Ported from the old app's notes.rs tests.
describe("study notes", () => {
  it("leaves out of the counts what is not there", () => {
    expect(counts(6, 10, 18)).toBe("6 ideas · 10 questions · 18 terms");
    expect(counts(1, 0, 1)).toBe("1 idea · 1 term");
    expect(counts(0, 0, 0)).toBe("");
  });

  it("takes an inline line out of its paragraph and keeps its chip", () => {
    const cites = [{ n: 1, title: "Moshi", url: "", name: "", excerpt: "Moshi is…" }];
    const h = citedInline("Why does **Moshi** skip text? [1]", cites);
    expect(h.startsWith("<p>")).toBe(false);
    expect(h).toContain("<strong>Moshi</strong>");
    expect(h).toContain('class="cite"');
  });

  it("counts notes up to date only for exactly their sources", () => {
    const s = (id: string, focus: string, sources: string[]): StudyNotesSummary => ({
      id,
      title: "t",
      display_title: "t",
      focus,
      created_ms: 0,
      sources,
      ideas: 1,
      questions: 1,
      terms: 1,
      state: "ready",
      job_id: null,
    });
    const list = [s("a", "latency", ["x.md"]), s("b", "", ["x.md", "y.md"])];
    expect(coveringNotes(list, ["y.md", "x.md"])).toBe("b");
    // A focused set is not the plain one.
    expect(coveringNotes(list, ["x.md"])).toBeNull();
    expect(coveringNotes(list, [])).toBeNull();
  });
});
