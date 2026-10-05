import { describe, expect, it } from "vitest";
import { readable, tidy } from "./errors";

// Ported from the old app's errors.rs tests.
describe("readable", () => {
  it("says out of credit in words, with what to do", () => {
    const e =
      'the sources could not be asked: the AI account is out of credit: HTTP 402: {"error":{"message": "Insufficient credits. Add more using https://openrouter.ai/settings/credits","type":"insufficient_quota"}}';
    const m = readable(e);
    expect(m.startsWith("The AI account is out of credit")).toBe(true);
    expect(m.includes("{")).toBe(false);
    expect(readable('credits","type":"insufficient_quota"}}')).toBe(m);
  });

  it("gives the common failures one sentence each", () => {
    for (const [raw, starts] of [
      ["the AI provider refused the key: HTTP 401: User not found", "The AI provider refused"],
      ["rate limited: HTTP 429: slow down", "The AI provider is busy"],
      ["network error", "The studio cannot be reached"],
      ["HTTP 500", "The studio ran into a problem"],
      ["HTTP 404", "That is no longer there"],
    ] as const)
      expect(readable(raw).startsWith(starts)).toBe(true);
  });

  it("says the same when cleaning twice", () => {
    for (const raw of ["insufficient_quota", "failed to fetch", "HTTP 502", "map failed: the model said no"])
      expect(readable(readable(raw))).toBe(readable(raw));
  });

  it("tidies anything else without rewording it", () => {
    expect(readable("map failed: the model said no")).toBe("Map failed: the model said no.");
    expect(readable('parse: {"error":{"message":"bad shape"}}')).toBe("Parse: bad shape.");
    expect(readable('half: {"error":{"mess')).toBe("Half.");
    expect(tidy("could not keep it: Os { code: 28, kind: StorageFull }")).toBe("Could not keep it.");
    expect(tidy("   ")).toBe("Something went wrong. Try again.");
  });
});
