import { afterEach, describe, expect, it, vi } from "vitest";
import {
  askSources,
  commandMatches,
  greeting,
  initials,
  nearBottom,
  send,
  splitCommand,
  type ChatState,
  type ChatMade,
  type MenuCommand,
} from "./chat";
import { store } from "./store";
import type { Src } from "./sources";

// Ported from the old app's chat.rs stick_tests and command_tests. What the
// commands do, `/help` and the answer to an unknown one moved to the server
// (tests/test_chat.py); the page only splits what was typed and sends it.
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

const MENU: MenuCommand[] = ["slides", "audio", "mindmap", "notes", "search", "research", "ask", "help", "clear"].map(
  (name) => ({ name, arg: "", label: name, icon: "x" }),
);

describe("slash commands", () => {
  it("names a command after the slash, and the rest is its argument", () => {
    expect(splitCommand("/mindmap")).toEqual({ name: "mindmap", arg: "" });
    expect(splitCommand("  /Search  rust async  ")).toEqual({ name: "search", arg: "rust async" });
    expect(splitCommand("/nope x")).toEqual({ name: "nope", arg: "x" });
    expect(splitCommand("no slash")).toBeNull();
  });

  it("narrows the menu as the name is typed and closes it at a space", () => {
    expect(commandMatches("/", MENU).length).toBe(MENU.length);
    expect(commandMatches("/re", MENU).map((c) => c.name)).toEqual(["research"]);
    expect(commandMatches("/search rust", MENU)).toEqual([]);
    expect(commandMatches("hello", MENU)).toEqual([]);
  });
});

// ── a turn, against a server that streams ────────────────────────────────────

function chat(): ChatState {
  return { cid: "c1", msgs: store(greeting()), talking: store(false), thinking: store(false), stick: store(true) };
}

/** A server that answers every POST with these events, and keeps what it was
 * sent. */
function streams(...events: Record<string, unknown>[]) {
  const sent: { url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit) => {
      sent.push({ url, body: JSON.parse(init.body as string) as Record<string, unknown> });
      const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
      return Promise.resolve(new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }));
    }),
  );
  return sent;
}

afterEach(() => vi.unstubAllGlobals());

const PICKS = { output: "" as const };

describe("a turn", () => {
  it("shows the work as it comes, then the answer, and reports what was made", async () => {
    const sent = streams(
      { t: "thinking" },
      { t: "step", id: "a1", kind: "fetch", text: "Reading", detail: "example.com" },
      { t: "step_note", id: "a1", text: "half way" },
      { t: "step_done", id: "a1", ok: true, text: "added “Reefs” · 40 words" },
      { t: "source", src: { url: "https://example.com/r", ok: true, title: "Reefs", chars: 240, error: "", name: "reefs.md", icon: "" } },
      { t: "step", id: "a2", kind: "build", text: "Making a mind map", detail: "" },
      { t: "step_done", id: "a2", ok: true, text: "12 topics" },
      { t: "build", kind: "mindmap", title: "Reefs", id: "m1" },
      { t: "reply", text: "Done." },
      { t: "state", ready: true, title: "Reefs" },
    );
    const st = chat();
    const srcs: Src[] = [];
    const made: ChatMade[] = [];
    const changed = await send(st, "read https://example.com/r", PICKS, (s) => srcs.push(s), (m) => made.push(m));
    expect(changed).toBe(true);
    expect(sent[0]!.url).toMatch(/\/collections\/c1\/chat$/);
    expect(sent[0]!.body).toEqual({ output: "", text: "read https://example.com/r" });
    const lines = st.msgs.get().slice(1);
    expect(lines.map((m) => [m.who, m.kind, m.status, m.text])).toEqual([
      ["You", "", "", "read https://example.com/r"],
      ["Studio", "step", "ok", "Reading"],
      ["Studio", "step", "ok", "Making a mind map"],
      ["Studio", "", "", "Done."],
    ]);
    expect(lines[1]!.note).toBe("added “Reefs” · 40 words");
    expect(srcs.map((s) => s.name)).toEqual(["Reefs"]);
    expect(made).toEqual([{ kind: "mindmap", id: "m1", title: "Reefs" }]);
    expect(st.talking.get()).toBe(false);
  });

  it("sends a slash command to the server as a command, and a failed step says why", async () => {
    const sent = streams(
      { t: "step", id: "a1", kind: "build", text: "Writing study notes", detail: "" },
      { t: "step_done", id: "a1", ok: false, text: "This is a read-only copy of “Reefs”." },
      { t: "reply", text: "I could not make study notes." },
    );
    const st = chat();
    await send(st, "/Notes  kelp ", { output: "notes", style: "editorial" }, () => {}, () => {});
    expect(sent[0]!.url).toMatch(/\/collections\/c1\/chat\/commands$/);
    expect(sent[0]!.body).toEqual({ output: "notes", style: "editorial", name: "notes", arg: "kelp", text: "" });
    const step = st.msgs.get().find((m) => m.kind === "step")!;
    expect(step.status).toBe("bad");
    expect(step.note).toContain("read-only copy");
  });

  it("starts over when the server clears the conversation", async () => {
    streams({ t: "cleared" });
    const st = chat();
    st.msgs.set((v) => [...v, { ...v[0]!, who: "You", me: true, text: "old" }]);
    await send(st, "/clear", PICKS, () => {}, () => {});
    expect(st.msgs.get()).toEqual(greeting());
  });

  it("asks from a map with the question shown as it was asked", async () => {
    const sent = streams(
      { t: "step", id: "a1", kind: "read", text: "Reading your sources", detail: "Polyps" },
      { t: "step_done", id: "a1", ok: true, text: "1 passage cited" },
      { t: "reply", text: "Polyps [1].", citations: [{ n: 1, name: "r.md", title: "Reefs", url: "", excerpt: "Polyps build reefs." }] },
    );
    const st = chat();
    await askSources(st, "Polyps", PICKS);
    expect(sent[0]!.body).toMatchObject({ name: "ask", arg: "Polyps", text: "Polyps" });
    const [, you, , answer] = st.msgs.get();
    expect(you!.text).toBe("Polyps");
    expect(answer!.cites.map((c) => c.n)).toEqual([1]);
  });

  it("says in words when the server refuses the turn", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ detail: "That collection is no longer there. Reload the page to see what is." }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    const st = chat();
    await send(st, "hello", PICKS, () => {}, () => {});
    const last = st.msgs.get().at(-1)!;
    expect(last.text).toMatch(/^I could not answer\. That collection is no longer there/);
  });
});
