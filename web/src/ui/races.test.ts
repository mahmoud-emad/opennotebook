// Answers that land late or not at all: an older read never overwrites a
// newer one, a cancelled wait ends at once, and "not there" is told by the
// status the server answered with, not by its words.

import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getCollection, isAbort, sleep, sourceAddText } from "./api";
import { said } from "./api-studio";
import { loadMaps, newMapState } from "./mindmap";
import { PlayerEngine } from "./playerEngine";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("a refusal", () => {
  it("carries its status, and only a 404 is not there", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(404, { detail: "Gone for good." }))));
    expect(await getCollection("c1")).toEqual({ found: false, outputs: [] });
    // The same words with another status are a refusal, not a missing page.
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(403, { detail: "It is no longer there." }))));
    const e = await getCollection("c1").catch((x: unknown) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect((e as ApiError).status).toBe(403);
    expect((e as ApiError).message).toBe("It is no longer there.");
  });

  it("says so when an add answers with nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(200, []))));
    await expect(sourceAddText("c1", "a note")).rejects.toThrow(/did not say whether it was added/);
  });
});

describe("a wait", () => {
  it("ends at once when it is cancelled", async () => {
    vi.useFakeTimers();
    const ctrl = new AbortController();
    let done = false;
    const p = sleep(60_000, ctrl.signal).then(() => (done = true));
    ctrl.abort();
    await p;
    expect(done).toBe(true);
    // Already cancelled: it does not wait at all.
    await sleep(60_000, ctrl.signal);
  });

  it("knows a cancelled call from a failed one", () => {
    expect(isAbort(new DOMException("stop", "AbortError"))).toBe(true);
    expect(isAbort(new Error("stop"))).toBe(false);
  });
});

describe("a list read twice", () => {
  it("keeps the newer answer when the older one lands last", async () => {
    const answers: ((r: Response) => void)[] = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((r) => answers.push(r))));
    const st = newMapState();
    const row = (id: string) => ({ id, title: id, focus: "", node_count: 1, sources: [], created_at: null });
    const first = loadMaps("c1", st);
    const second = loadMaps("c1", st);
    answers[1]!(json(200, [row("new")]));
    await second;
    answers[0]!(json(200, [row("old")]));
    await first;
    expect(st.maps.get().map((m) => m.id)).toEqual(["new"]);
    expect(st.loaded.get()).toBe(true);
  });

  it("says a list that could not be read in the list, not in the banner", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(500, { detail: "The studio ran into a problem." }))));
    const st = newMapState();
    await loadMaps("c1", st);
    expect(st.loadErr.get()).toBe("The studio ran into a problem.");
    expect(st.loaded.get()).toBe(true);
  });
});

describe("the conversation's lines", () => {
  it("each have a key of their own that a change keeps", () => {
    const a = said("You", "hi", true);
    const b = said("You", "hi", true);
    expect(a.key).not.toBe(b.key);
    expect({ ...a, note: "changed" }.key).toBe(a.key);
  });
});

describe("the player, mounted twice", () => {
  it("follows a build in preparation once, not twice", async () => {
    const opened: string[] = [];
    vi.stubGlobal(
      "EventSource",
      class {
        constructor(url: string) {
          opened.push(url);
        }
        addEventListener() {}
        close() {}
        onerror: unknown = null;
      },
    );
    const answers: ((r: Response) => void)[] = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((r) => answers.push(r))));
    const preparing = {
      id: "s1",
      collection_id: "c1",
      kind: "slides",
      title: "",
      state: "preparing",
      speaker_list: [],
      speaker_names: {},
      slides: [],
      audio: null,
    };
    const engine = new PlayerEngine({ sid: "s1", share: null });
    const audio = document.createElement("audio");
    // Mount, unmount and mount again, as React does in development.
    const detach = engine.attach(audio);
    const first = engine.boot();
    detach();
    engine.dispose();
    engine.attach(audio);
    const second = engine.boot();
    answers[0]!(json(200, preparing));
    answers[1]!(json(200, preparing));
    await Promise.all([first, second]);
    expect(opened).toHaveLength(1);
    engine.dispose();
  });
});
