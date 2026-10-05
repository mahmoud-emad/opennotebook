import { describe, expect, it } from "vitest";
import { phaseLabel } from "./phases";
import { audioFormatName } from "./audioFormats";

describe("one name for each step and format", () => {
  it("names every step the server's build reports, and nothing else", () => {
    // build/pipeline.py: PHASES = ("research", "ingest", "script", "deck", "validate")
    for (const step of ["research", "ingest", "script", "deck", "validate"]) expect(phaseLabel(step)).not.toBeNull();
    expect(phaseLabel("narrate")).toBeNull();
    expect(phaseLabel("")).toBeNull();
  });

  it("names the four audio formats, and leaves an unknown one to its caller", () => {
    expect(audioFormatName("deep_dive")).toBe("Deep Dive");
    expect(audioFormatName("critique")).toBe("Critique");
    expect(audioFormatName("")).toBeNull();
  });
});
