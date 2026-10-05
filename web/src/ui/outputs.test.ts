import { describe, expect, it } from "vitest";
import { prepFailureText, removedLine } from "./outputs";

// Ported from the old app's outputs.rs tests.
describe("outputs", () => {
  it("words the delete question to agree with its kind", () => {
    expect(removedLine("study notes").startsWith("The study notes are removed.")).toBe(true);
    expect(removedLine("mind map").startsWith("The mind map is removed.")).toBe(true);
  });

  it("says a failed prep plainly and keeps its own words back", () => {
    expect(prepFailureText("")).toEqual(["Something went wrong while making this. Your sources are kept.", ""]);
    const [plain, detail] = prepFailureText("deck came back failed after 600s (0/5 rendered)");
    expect(plain).toContain("The slides could not be drawn");
    expect(detail).toBe("deck came back failed after 600s (0/5 rendered)");
    expect(prepFailureText("HTTP 402 insufficient credit")[0]).toContain("out of credit");
  });

  it("shows the server's own sentence as the message", () => {
    const said = "The AI provider refused the studio's API key. Check the key, then try again.";
    expect(prepFailureText(said)).toEqual([said, ""]);
  });
});
