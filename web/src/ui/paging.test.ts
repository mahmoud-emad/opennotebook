// Lists the server answers a page at a time are read whole: every page is
// asked for in turn, from where the page before said the next one starts.

import { afterEach, describe, expect, it, vi } from "vitest";
import { callAll, callAllBack, listCollections } from "./api";
import { chatHistory } from "./api-studio";

afterEach(() => {
  vi.unstubAllGlobals();
});

const page = (body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json", ...headers } });

/** A fetch that answers with `pages` in turn, and the paths it was asked. */
function serve(pages: Response[]) {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      asked.push(url.replace(/^.*\/api/, ""));
      const next = pages.shift();
      return next ? Promise.resolve(next) : Promise.reject(new Error("asked once too often"));
    }),
  );
  return asked;
}

describe("a paged list", () => {
  it("is read to its end, each page from where the last one said", async () => {
    const asked = serve([page([1, 2], { "X-Next-Offset": "2" }), page([3, 4], { "X-Next-Offset": "4" }), page([5])]);
    expect(await callAll<number>("/things?sort=newest")).toEqual([1, 2, 3, 4, 5]);
    expect(asked).toEqual(["/things?sort=newest", "/things?sort=newest&offset=2", "/things?sort=newest&offset=4"]);
  });

  it("is one call when it fits on one page", async () => {
    const asked = serve([page([1])]);
    expect(await callAll<number>("/things")).toEqual([1]);
    expect(asked).toEqual(["/things"]);
  });

  it("read from its newest page back keeps the oldest first", async () => {
    const asked = serve([page([3, 4], { "X-Next-Before": "m3" }), page([1, 2])]);
    expect(await callAllBack<number>("/chat")).toEqual([1, 2, 3, 4]);
    expect(asked).toEqual(["/chat", "/chat?before=m3"]);
  });

  it("stops at the first refusal", async () => {
    serve([page([1], { "X-Next-Offset": "1" }), new Response(JSON.stringify({ detail: "No." }), { status: 500 })]);
    await expect(callAll<number>("/things")).rejects.toThrow("No.");
  });
});

const collection = (id: string) => ({
  id,
  title: id,
  display_title: id,
  title_auto: false,
  created_at: "2026-10-05T00:00:00Z",
  updated_at: "2026-10-05T00:00:00Z",
  pinned: false,
  sources: 0,
  decks: 0,
  audios: 0,
  maps: 0,
  notes: 0,
  preparing: 0,
  failed: 0,
  cover_version: "v",
  reused_from: null,
  shared: false,
  reuses: 0,
  read_only: false,
  reused_from_title: null,
  busy: false,
  auto_named: false,
  name_note: null,
});

describe("the home's collections", () => {
  it("are all shown, past the first page", async () => {
    serve([page([collection("a")], { "X-Next-Offset": "1" }), page([collection("b")])]);
    expect((await listCollections()).map((c) => c.cid)).toEqual(["a", "b"]);
  });
});

describe("a conversation", () => {
  it("is read whole, its older pages ahead", async () => {
    const msg = (id: string, text: string) => ({ id, role: "user", text, steps: [], citations: [] });
    serve([page([msg("m2", "second")], { "X-Next-Before": "m2" }), page([msg("m1", "first")])]);
    expect((await chatHistory("c1")).map((m) => m.text)).toEqual(["first", "second"]);
  });
});
