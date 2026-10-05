import { afterEach, describe, expect, it, vi } from "vitest";
import { followJob } from "./api-studio";

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

const job = (over: Record<string, unknown>) => ({
  id: "j1",
  kind: "research",
  status: "running",
  step: "",
  steps_done: 0,
  steps_total: 1,
  error: null,
  session_id: null,
  collection_id: "c1",
  created_at: "2026-10-05T10:00:00Z",
  waiting: null,
  ...over,
});

afterEach(() => vi.unstubAllGlobals());

describe("following background work", () => {
  it("passes on what the server says it is doing, and ends when it is done", async () => {
    const answers = [
      job({ status: "queued", waiting: "Waiting for the studio's worker to start." }),
      job({ step: "Searching: coral" }),
      job({ step: "Searching: coral" }),
      job({ step: "Reading example.org" }),
      job({ status: "done", step: "Reading example.org" }),
    ];
    const fetch = vi.fn(() => reply(200, answers.shift()));
    vi.stubGlobal("fetch", fetch);
    const said: string[] = [];
    expect(await followJob("j1", (s) => said.push(s), undefined, 0)).toBeNull();
    expect(said).toEqual(["Waiting for the studio's worker to start.", "Searching: coral", "Reading example.org"]);
    expect(fetch).toHaveBeenCalledTimes(5);
    expect((fetch.mock.calls[0] as unknown as [string])[0]).toBe("/api/jobs/j1");
  });

  it("says why at once when the work fails, in the server's words", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        reply(200, job({ status: "failed", error: "The web searches found nothing on that topic. Try wording it differently." })),
      ),
    );
    expect(await followJob("j1", () => {}, undefined, 0)).toBe(
      "The web searches found nothing on that topic. Try wording it differently.",
    );
  });

  it("tries a failed read again, and says so when the work is gone", async () => {
    const answers = [
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => reply(404, { detail: "That piece of work is not there. Reload the page." }),
    ];
    vi.stubGlobal("fetch", vi.fn(() => answers.shift()!()));
    expect(await followJob("j1", () => {}, undefined, 0)).toBe("That piece of work is not there. Reload the page.");
  });

  it("stops quietly when the page goes", async () => {
    const stop = new AbortController();
    stop.abort();
    const fetch = vi.fn(() => reply(200, job({})));
    vi.stubGlobal("fetch", fetch);
    expect(await followJob("j1", () => {}, stop.signal, 0)).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });
});
