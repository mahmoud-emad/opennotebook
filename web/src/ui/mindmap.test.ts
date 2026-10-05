import { describe, expect, it } from "vitest";
import type { MindMapSummary } from "./api-studio";
import { coveringMap, fileStem } from "./mindmap";

// Ported from the old app's mindmap.rs tests.
describe("mind maps", () => {
  it("counts a map up to date only for exactly its sources", () => {
    const map = (id: string, focus: string, sources: string[]): MindMapSummary => ({
      id,
      focus,
      sources,
      title: "",
      node_count: 0,
      created_ms: 0,
    });
    const maps = [map("new", "", ["a.md", "b.md"]), map("old", "", ["a.md"])];
    // Same sources, any order: the newest such map.
    expect(coveringMap(maps, ["b.md", "a.md"])).toBe("new");
    expect(coveringMap(maps, ["a.md"])).toBe("old");
    // A source added since, or none at all: nothing is up to date.
    expect(coveringMap(maps, ["a.md", "b.md", "c.md"])).toBeNull();
    expect(coveringMap(maps, [])).toBeNull();
    // A focused map covers its focus, not the sources as a whole.
    expect(coveringMap([map("f", "latency", ["a.md"])], ["a.md"])).toBeNull();
  });

  it("makes a title a safe file name", () => {
    expect(fileStem("[2410.00037] Moshi: a model")).toBe("2410 00037 Moshi a model");
    expect(fileStem("///")).toBe("Mind map");
  });
});
