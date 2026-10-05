import { describe, expect, it } from "vitest";
import { errText } from "./api";
import { UNREACHABLE, unworded } from "./errors";

// The server words its own failures; the page words only what never reached it.
describe("errors the server could not word", () => {
  it("says the studio cannot be reached when a proxy answers for it", () => {
    for (const status of [502, 503, 504]) expect(unworded(status)).toBe(UNREACHABLE);
    expect(UNREACHABLE).toContain("then try again");
  });

  it("gives every other unworded refusal a sentence with what to do", () => {
    expect(unworded(413)).toContain("Split it");
    expect(unworded(500)).toBe("The studio did not say why that failed. Reload the page and try again.");
  });

  it("shows the server's sentence as it is, without rewording it", () => {
    const said = "The AI account is out of credit. Add credit at openrouter.ai, then try again.";
    expect(errText(new Error(said))).toBe(said);
    expect(errText("not an error")).toBe("Something went wrong. Reload the page and try again.");
  });
});
