import { describe, expect, it } from "vitest";
import { audioDesc, offeredLengths } from "./audioFormats";

// Ported from the old app's collection.rs tests.
describe("an audio overview's format", () => {
  it("names an audio overview by its format and length", () => {
    expect(audioDesc("brief", "default")).toBe("Brief");
    expect(audioDesc("brief", "shorter")).toBe("Brief");
    expect(audioDesc("deep_dive", "shorter")).toBe("Deep Dive · shorter");
    expect(audioDesc("debate", "default")).toBe("Debate");
  });

  it("offers Brief no lengths and Deep Dive all three", () => {
    expect(offeredLengths("brief")).toEqual([]);
    expect(offeredLengths("deep_dive")).toEqual(["shorter", "default", "longer"]);
    expect(offeredLengths("nonsense")).toEqual([]);
  });
});
