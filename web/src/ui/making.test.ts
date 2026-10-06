import { afterEach, describe, expect, it, vi } from "vitest";
import { applyMaps, loadMaps, makeMap, mapEnded, newMapState } from "./mindmap";
import { mapSummaryOf } from "./api-studio";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const row = (id: string, state: "making" | "ready", job: string | null = null) => ({
  id,
  collection_id: "c1",
  title: id,
  display_title: id,
  focus: "",
  node_count: 1,
  sources: [],
  shape: [],
  created_at: "2026-10-01T00:00:00Z",
  state,
  job_id: job,
});

const job = (id: string, status: string, error: string | null = null) => ({
  id,
  kind: "mindmap",
  status,
  step: "",
  steps_done: 0,
  steps_total: 1,
  error,
  session_id: null,
  collection_id: "c1",
  created_at: "2026-10-01T00:00:00Z",
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a map the server makes", () => {
  it("says Making while the server lists one being made, and lists it once the stream says it is drawn", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(200, [row("m1", "making", "j1")]))));
    const st = newMapState();
    await loadMaps("c1", st);
    expect(st.making.get()).toBe(true);
    expect(st.maps.get()).toEqual([]);
    // Nothing is asked again: the collection's stream brings the list.
    applyMaps(st, [row("m1", "ready")].map(mapSummaryOf));
    mapEnded(st, "j1", null);
    expect(st.making.get()).toBe(false);
    expect(st.maps.get().map((m) => m.id)).toEqual(["m1"]);
    expect(st.err.get()).toBe("");
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });

  it("says why a map made in another tab failed", () => {
    const st = newMapState();
    applyMaps(st, [row("m1", "making", "j1")].map(mapSummaryOf));
    applyMaps(st, []);
    mapEnded(st, "j1", "The AI model gave back no usable mind map.");
    expect(st.err.get()).toBe("The AI model gave back no usable mind map.");
  });

  it("opens what it asked for once drawn, and says why when the job fails", async () => {
    let n = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          n++;
          return Promise.resolve(json(202, { job: job(`j${n}`, "queued"), mindmap: row(`m${n}`, "making", `j${n}`) }));
        }
        return Promise.resolve(json(200, n === 1 ? [row("m1", "ready")] : []));
      }),
    );
    const st = newMapState();
    const asked = makeMap("c1", st);
    expect(st.making.get()).toBe(true);
    await vi.waitFor(() => expect(st.mk.waiting.has("j1")).toBe(true));
    mapEnded(st, "j1", null);
    expect(await asked).toBe("m1");
    expect(st.making.get()).toBe(false);

    const failed = makeMap("c1", st);
    await vi.waitFor(() => expect(st.mk.waiting.has("j2")).toBe(true));
    mapEnded(st, "j2", "The AI model gave back no usable mind map.");
    expect(await failed).toBeNull();
    expect(st.err.get()).toBe("The AI model gave back no usable mind map.");
    expect(st.making.get()).toBe(false);
  });

  it("keeps an end heard before its ask came back", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) =>
        Promise.resolve(
          init?.method === "POST"
            ? json(202, { job: job("j1", "queued"), mindmap: row("m1", "making", "j1") })
            : json(200, [row("m1", "ready")]),
        ),
      ),
    );
    const st = newMapState();
    mapEnded(st, "j1", null);
    expect(await makeMap("c1", st)).toBe("m1");
  });

  it("asks how a job ended when the stream was down", async () => {
    const { recheck } = await import("./making");
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        if (init?.method === "POST")
          return Promise.resolve(json(202, { job: job("j1", "queued"), mindmap: row("m1", "making", "j1") }));
        if (url.includes("/jobs/j1"))
          return Promise.resolve(json(200, job("j1", "failed", "The AI model gave back no usable mind map.")));
        return Promise.resolve(json(200, []));
      }),
    );
    const st = newMapState();
    const asked = makeMap("c1", st);
    await vi.waitFor(() => expect(st.mk.waiting.has("j1")).toBe(true));
    await recheck(st.mk);
    expect(await asked).toBeNull();
    expect(st.err.get()).toBe("The AI model gave back no usable mind map.");
  });
});
