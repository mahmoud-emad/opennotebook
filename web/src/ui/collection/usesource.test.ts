import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatState } from "../chat";
import type { Src } from "../sources";
import { pageActions } from "./actions";
import { pageState } from "./state";

const props = { cid: "c1", open: null, start: null, setCrumb: () => {}, onOpen: () => {}, onGone: () => {} };
const src: Src = { name: "Reefs", detail: "", ok: true, url: "", icon: "", file: "reefs.md", active: true };

afterEach(() => vi.unstubAllGlobals());

describe("switching a source", () => {
  it("shows the change at once, before the server has answered", () => {
    vi.stubGlobal("fetch", () => new Promise(() => {}));
    const S = pageState(props);
    S.srcs.set([src]);
    pageActions("c1", S, {} as ChatState).useSource(src, false);
    expect(S.srcs.get()[0]?.active).toBe(false);
    // And says it is waiting, so the row can show it.
    expect(S.rowBusy.get()["src:reefs.md"]).toBe("Switching…");
  });

  it("puts it back, saying why, when the server refuses", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: "This is a read-only copy." }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const S = pageState(props);
    S.srcs.set([src]);
    pageActions("c1", S, {} as ChatState).useSource(src, false);
    await vi.waitFor(() => expect(S.rowErr.get()["src:reefs.md"]).toContain("It could not be switched off."));
    expect(S.rowErr.get()["src:reefs.md"]).toContain("This is a read-only copy.");
    expect(S.srcs.get()[0]?.active).toBe(true);
    expect(S.rowBusy.get()).toEqual({});
  });

  it("keeps it changed when the server agrees", async () => {
    const fetch = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ name: "reefs.md", active: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetch);
    const S = pageState(props);
    S.srcs.set([src]);
    pageActions("c1", S, {} as ChatState).useSource(src, false);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(S.rowBusy.get()).toEqual({}));
    expect(S.srcs.get()[0]?.active).toBe(false);
    expect(S.rowErr.get()).toEqual({});
  });
});
