import { describe, expect, it } from "vitest";
import { ICONS } from "./icons";
import { outputKey, outputKind, reusedLine, shareHasContent } from "./share";

describe("sharing", () => {
  it("reads a reuse count as words", () => {
    expect(reusedLine(0)).toBe("Not reused yet");
    expect(reusedLine(1)).toBe("Reused once");
    expect(reusedLine(12)).toBe("Reused 12 times");
  });

  // The keys are the server's: a deck and an audio overview are both a
  // session.
  it("keys outputs as the server keys them", () => {
    expect(outputKey("session", "s1")).toBe("session:s1");
    expect(outputKey("audio", "s2")).toBe("session:s2");
    expect(outputKey("mindmap", "m1")).toBe("mindmap:m1");
    expect(outputKey("notes", "n1")).toBe("notes:n1");
    expect(outputKind("audio")).toBe("audio");
    expect(outputKind("mindmap")).toBe("mindmap");
    expect(outputKind("notes")).toBe("notes");
    expect(outputKind("session")).toBe("session");
  });

  // An empty share is refused before it is asked for.
  it("must hold something", () => {
    expect(shareHasContent(false, 3, 0)).toBe(false);
    expect(shareHasContent(true, 0, 0)).toBe(false);
    expect(shareHasContent(true, 2, 0)).toBe(true);
    expect(shareHasContent(false, 0, 1)).toBe(true);
  });

  // A name that is not vendored draws nothing at all, an invisible button.
  it("names only icons that are vendored", () => {
    for (const n of ["compass", "copy", "share", "collection", "link-45deg", "pencil", "check-circle-fill"])
      expect(ICONS[n], n).toBeTruthy();
  });
});
