import { afterEach, describe, expect, it, vi } from "vitest";
import { newMapState, loadMaps, makeMap } from "./mindmap";

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
  vi.useRealTimers();
});

describe("a map the server makes", () => {
  it("says Making while the server lists one being made, and lists it once it is done", async () => {
    vi.useFakeTimers();
    let made = false;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/jobs/j1")) {
          made = true;
          return Promise.resolve(json(200, job("j1", "done")));
        }
        return Promise.resolve(json(200, [made ? row("m1", "ready") : row("m1", "making", "j1")]));
      }),
    );
    const st = newMapState();
    await loadMaps("c1", st);
    expect(st.making.get()).toBe(true);
    expect(st.maps.get()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1500);
    await vi.runOnlyPendingTimersAsync();
    expect(st.making.get()).toBe(false);
    expect(st.maps.get().map((m) => m.id)).toEqual(["m1"]);
  });

  it("opens what it asked for once drawn, and says why when the job fails", async () => {
    vi.useFakeTimers();
    let status = "done";
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        if (init?.method === "POST")
          return Promise.resolve(json(202, { job: job("j2", "queued"), mindmap: row("m2", "making", "j2") }));
        if (url.includes("/jobs/j2"))
          return Promise.resolve(
            json(200, job("j2", status, status === "failed" ? "The AI model gave back no usable mind map." : null)),
          );
        return Promise.resolve(json(200, status === "done" ? [row("m2", "ready")] : []));
      }),
    );
    const st = newMapState();
    const asked = makeMap("c1", st);
    expect(st.making.get()).toBe(true);
    await vi.advanceTimersByTimeAsync(1500);
    expect(await asked).toBe("m2");
    expect(st.making.get()).toBe(false);

    status = "failed";
    const failed = makeMap("c1", st);
    await vi.advanceTimersByTimeAsync(1500);
    expect(await failed).toBeNull();
    expect(st.err.get()).toBe("The AI model gave back no usable mind map.");
    expect(st.making.get()).toBe(false);
  });
});
