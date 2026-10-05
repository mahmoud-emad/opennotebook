import { describe, expect, it } from "vitest";
import { COMMANDS, commandMatches, helpText, initials, nearBottom, parseCommand } from "./chat";

// Ported from the old app's chat.rs stick_tests and command_tests.
describe("the chat thread", () => {
  /** The thread follows new lines only while the person is at its end. */
  it("takes the bottom as the end, give or take a flick", () => {
    // 1,000 px of chat in a 400 px box: the end is scroll_top 600.
    expect(nearBottom(600, 1000, 400)).toBe(true);
    expect(nearBottom(560, 1000, 400)).toBe(true);
    expect(nearBottom(500, 1000, 400)).toBe(false);
    expect(nearBottom(0, 1000, 400)).toBe(false);
    // A chat shorter than its box is always at its end.
    expect(nearBottom(0, 300, 400)).toBe(true);
  });

  it("marks a speaker by their initials", () => {
    expect(initials("Studio")).toBe("S");
    expect(initials("ada  lovelace byron")).toBe("AL");
  });
});

describe("slash commands", () => {
  it("names a command after the slash, and the rest is its argument", () => {
    expect(parseCommand("/mindmap")).toEqual({ cmd: { make: "mindmap" }, arg: "" });
    expect(parseCommand("  /Search  rust async  ")).toEqual({ cmd: "search", arg: "rust async" });
    expect(parseCommand("/nope x")).toEqual({ unknown: "nope" });
    expect(parseCommand("no slash")).toBeNull();
  });

  it("narrows the menu as the name is typed and closes it at a space", () => {
    expect(commandMatches("/", COMMANDS).length).toBe(COMMANDS.length);
    expect(commandMatches("/re", COMMANDS).map((c) => c.name)).toEqual(["research"]);
    expect(commandMatches("/search rust", COMMANDS)).toEqual([]);
    expect(commandMatches("hello", COMMANDS)).toEqual([]);
  });

  it("lists every command in its help", () => {
    const h = helpText();
    for (const c of COMMANDS) expect(h).toContain(`/${c.name}`);
  });
});
