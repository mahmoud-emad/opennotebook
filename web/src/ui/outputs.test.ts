import { describe, expect, it } from "vitest";
import { removedLine } from "./outputs";

// Ported from the old app's outputs.rs tests.
describe("outputs", () => {
  it("words the delete question to agree with its kind", () => {
    expect(removedLine("study notes").startsWith("The study notes are removed.")).toBe(true);
    expect(removedLine("mind map").startsWith("The mind map is removed.")).toBe(true);
  });
});
