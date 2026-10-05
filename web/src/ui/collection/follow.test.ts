import { afterEach, describe, expect, it, vi } from "vitest";
import { followCollection } from "../api";
import type { ChatState } from "../chat";
import { pageActions } from "./actions";
import { pageState } from "./state";

/** A stand-in for the browser's EventSource: the test says what the server
 * sends. */
class FakeSource {
  static last: FakeSource | null = null;
  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private on = new Map<string, ((e: MessageEvent) => void)[]>();
  constructor(url: string) {
    this.url = url;
    FakeSource.last = this;
  }
  addEventListener(name: string, fn: (e: MessageEvent) => void) {
    this.on.set(name, [...(this.on.get(name) ?? []), fn]);
  }
  send(name: string, data: unknown) {
    for (const fn of this.on.get(name) ?? []) fn(new MessageEvent(name, { data: JSON.stringify(data) }));
  }
  close() {
    this.closed = true;
  }
}

const summary = {
  id: "c1",
  title: "",
  display_title: "Untitled collection",
  title_auto: true,
  pinned: false,
  created_at: "2026-10-05T10:00:00Z",
  updated_at: "2026-10-05T10:00:00Z",
  cover_version: "f-1",
  sources: 1,
  decks: 1,
  audios: 0,
  maps: 0,
  notes: 0,
  preparing: 1,
  failed: 0,
  reused_from: null,
  shared: false,
  reuses: 0,
  read_only: false,
  reused_from_title: null,
  busy: true,
  auto_named: true,
  name_note: "Naming it from its sources…",
};

const output = {
  id: "s1",
  collection_id: "c1",
  kind: "slides",
  title: "Editorial slides",
  description: "",
  state: "preparing",
  failure: null,
  parts: 0,
  speakers: 1,
  audio_format: "",
  audio_label: "",
  duration_ms: 0,
  pinned: false,
  spent_usd: null,
  spent_known: true,
  created_at: "2026-10-05T10:00:00Z",
  waiting: null,
};

const props = { cid: "c1", open: null, start: null, setCrumb: () => {}, onOpen: () => {}, onGone: () => {} };

afterEach(() => {
  vi.unstubAllGlobals();
  FakeSource.last = null;
});

describe("following a collection", () => {
  it("hands on each part the server sends, in the page's shapes", () => {
    vi.stubGlobal("EventSource", FakeSource);
    const got: string[] = [];
    const close = followCollection("c1", {
      collection: (c) => got.push(`collection ${c.cid} ${c.display_title} busy=${c.busy}`),
      outputs: (o) => got.push(`outputs ${o.map((x) => x.sid).join(",")}`),
      progress: (p) => got.push(`progress ${p.session_id} ${p.steps_done}/${p.steps_total}`),
      sources: (s) => got.push(`sources ${s.map((x) => x.title).join(",")}`),
      gone: () => got.push("gone"),
      up: (ok) => got.push(`up ${ok}`),
    });
    const es = FakeSource.last!;
    expect(es.url).toBe("/api/collections/c1/events");
    es.onopen?.();
    es.send("collection", summary);
    es.send("outputs", [output]);
    es.send("progress", { session_id: "s1", step: "script", steps_done: 2, steps_total: 5 });
    es.send("sources", [{ name: "a.md", kind: "text", title: "Reefs", url: "", chars: 600, created_at: "" }]);
    es.onerror?.();
    es.send("gone", { collection_id: "c1" });
    expect(got).toEqual([
      "up true",
      "collection c1 Untitled collection busy=true",
      "outputs s1",
      "progress s1 2/5",
      "sources Reefs",
      "up false",
      "gone",
    ]);
    expect(es.closed).toBe(true);
    close();
  });

  it("says the stream is down where there is no EventSource at all", () => {
    vi.stubGlobal("EventSource", undefined);
    const up = vi.fn();
    followCollection("c1", { up });
    expect(up).toHaveBeenCalledWith(false);
  });

  it("puts what the stream says on the page, and closes it when the page goes", () => {
    vi.stubGlobal("EventSource", FakeSource);
    const S = pageState(props);
    const A = pageActions("c1", S, {} as ChatState);
    const stop = A.follow();
    const es = FakeSource.last!;
    es.send("collection", summary);
    es.send("outputs", [output]);
    es.send("progress", { session_id: "s1", step: "script", steps_done: 2, steps_total: 5 });
    es.send("sources", [{ name: "a.md", kind: "text", title: "Reefs", url: "", chars: 600, created_at: "" }]);
    expect(S.summary.get()?.name_note).toBe("Naming it from its sources…");
    expect(S.outputs.get().map((o) => o.sid)).toEqual(["s1"]);
    expect(S.loaded.get()).toBe(true);
    expect(S.progress.get().s1?.steps_done).toBe(2);
    expect(S.srcs.get().map((s) => [s.name, s.file])).toEqual([["Reefs", "a.md"]]);
    es.send("gone", {});
    expect(S.missing.get()).toBe(true);
    stop();
    expect(es.closed).toBe(true);
  });
});
