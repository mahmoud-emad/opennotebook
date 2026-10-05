import { describe, expect, it } from "vitest";
import { ICONS } from "./icons";
import { outputKind, reusedLine, shareHasContent } from "./share";

describe("sharing", () => {
  it("reads a reuse count as words", () => {
    expect(reusedLine(0)).toBe("Not reused yet");
    expect(reusedLine(1)).toBe("Reused once");
    expect(reusedLine(12)).toBe("Reused 12 times");
  });

  it("reads an output's kind from the wire's", () => {
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
