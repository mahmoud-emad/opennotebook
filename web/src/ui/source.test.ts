import { describe, expect, it } from "vitest";
import { markedParagraphs } from "./source";

// Ported from the old app's source.rs tests.
describe("the cited passage", () => {
  it("marks the passage's paragraphs and nothing else", () => {
    const text =
      "# Pyramids\n\nSource: https://w.org\n\nRamps were used to haul blocks up.\n\n" +
      "Menu\n\nWorkforce estimates vary widely.\n\nThe end.";
    const passage = "Ramps were used to haul blocks up.\n\nWorkforce estimates vary widely.";
    expect(markedParagraphs(text, passage).map((p) => p[1])).toEqual([false, false, true, false, true, false]);
  });

  it("lands near its words when the passage drifted", () => {
    const text = "Intro.\n\nRamps   were used to haul\nblocks up the side, it is thought.";
    const m = markedParagraphs(text, "Ramps were used to haul blocks up the side").map((p) => p[1]);
    expect(m).toEqual([false, true]);
  });
});
