import { describe, expect, it } from "vitest";
import { phaseLabel } from "./phases";

describe("one name for each step", () => {
  it("names every step the server's build reports, and nothing else", () => {
    // build/pipeline.py: PHASES = ("research", "ingest", "script", "deck", "validate")
    for (const step of ["research", "ingest", "script", "deck", "validate"]) expect(phaseLabel(step)).not.toBeNull();
    expect(phaseLabel("narrate")).toBeNull();
    expect(phaseLabel("")).toBeNull();
  });
});
