import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sharedItemOf, wireKind, type SharedItem, type SharedOutput } from "./api-share";
import { ItemTile, SharedRow, itemFrom, sharedFacts } from "./discover";
import { ICONS } from "./icons";
import { FEED_KINDS, appendItems, feedKindLabel, itemAction } from "./share";
import { outputIcon } from "./shell";

// Its names as the server sends them: shown as titled unless a test says.
const item = (over: Partial<SharedItem> = {}): SharedItem => ({
  key: "session:d1",
  kind: "session",
  id: "d1",
  title: "Reef deck",
  display_title: over.title ?? "Reef deck",
  collection_display_title: over.collection_title ?? "Coral reefs",
  slide_count: 12,
  duration_ms: 522_000,
  created_ms: 0,
  share_id: "sh1",
  cid: "c1",
  collection_title: "Coral reefs",
  cover_version: "f-1",
  shared_by: "Sam",
  mine: false,
  reuses: 0,
  ...over,
});

beforeEach(() => {
  // The cover measures its box; jsdom has nothing to measure.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Discover's kinds", () => {
  it("offers All and the four kinds, each with its icon", () => {
    expect(FEED_KINDS.map((k) => feedKindLabel[k])).toEqual([
      "All",
      "Narrated slides",
      "Audio overviews",
      "Mind maps",
      "Study notes",
    ]);
    for (const k of FEED_KINDS) if (k !== "all") expect(ICONS[outputIcon[k]]).toBeTruthy();
  });

  it("asks the server for a kind by its wire name, and reads an item back", () => {
    expect(["session", "audio", "mindmap", "notes"].map((k) => wireKind(k as "session"))).toEqual([
      "slides",
      "audio",
      "mindmap",
      "notes",
    ]);
    const it = sharedItemOf({
      key: "session:d1",
      kind: "slides",
      id: "d1",
      title: "Deck",
      display_title: "Deck",
      collection_display_title: "Coral reefs",
      parts: 12,
      duration_ms: 522_000,
      created_at: "2026-10-01T00:00:00Z",
      share_id: "sh1",
      collection_id: "c1",
      collection_title: "Reefs",
      cover_version: "g-1",
      shared_by: "",
      mine: true,
      reuses: 2,
    });
    expect(it.kind).toBe("session");
    expect(it.slide_count).toBe(12);
    expect(it.cid).toBe("c1");
    expect(it.created_ms).toBe(Date.parse("2026-10-01T00:00:00Z"));
  });

  it("says what opening each kind does", () => {
    expect(itemAction("session")).toBe("Play");
    expect(itemAction("audio")).toBe("Listen");
    expect(itemAction("mindmap")).toBe("Open");
    expect(itemAction("notes")).toBe("Open");
  });

  it("says an item's length, name and origin", () => {
    expect(sharedFacts(item())).toBe("12 slides · 8:42");
    expect(sharedFacts(item({ kind: "audio", duration_ms: 61_000 }))).toBe("Audio overview · 1:01");
    expect(itemFrom(item())).toEqual({ collection: "Coral reefs", by: "Shared by Sam" });
    expect(itemFrom(item({ mine: true }))).toEqual({ collection: "Coral reefs", by: "Shared by you" });
    expect(itemFrom(item({ collection_display_title: "Untitled collection", shared_by: "" }))).toEqual({
      collection: "Untitled collection",
      by: "",
    });
  });

  it("keeps each item once however the pages overlap", () => {
    const a = item({ key: "notes:1" });
    const b = item({ key: "notes:2" });
    const c = item({ key: "notes:2", share_id: "sh2" });
    expect(appendItems([a, b], [b, c]).map((i) => `${i.share_id}/${i.key}`)).toEqual([
      "sh1/notes:1",
      "sh1/notes:2",
      "sh2/notes:2",
    ]);
  });
});

describe("an item card", () => {
  it("plays a deck in the player, through its share", () => {
    render(<ItemTile it={item()} onOpen={() => {}} />);
    const play = screen.getByRole("link", { name: "Play: Reef deck" });
    expect(play.getAttribute("href")).toBe("/ui/play/d1?share=sh1");
    const coll = screen.getByRole("link", { name: "Coral reefs" });
    expect(coll.getAttribute("href")).toBe("/ui/shared/sh1");
    expect(screen.getByText("12 slides · 8:42")).toBeTruthy();
    expect(screen.getByText("Shared by Sam")).toBeTruthy();
  });

  it("opens a map where it is, read through its share", () => {
    const onOpen = vi.fn();
    render(<ItemTile it={item({ kind: "mindmap", key: "mindmap:m1", id: "m1", title: "Map" })} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "Open: Map" }));
    expect(onOpen).toHaveBeenCalledWith({ kind: "mindmap", id: "m1", title: "Map", shareId: "sh1", cid: "c1" });
  });
});

describe("a shared collection's outputs", () => {
  const out = (over: Partial<SharedOutput>): SharedOutput => ({
    key: "session:d1",
    kind: "session",
    title: "Deck",
    display_title: over.title ?? "Deck",
    sid: "d1",
    id: "",
    slide_count: 4,
    duration_ms: 0,
    created_ms: 0,
    ...over,
  });

  it("says Play, Listen or Open on every row", () => {
    const { container, rerender } = render(<SharedRow o={out({})} shareId="sh1" on={false} onOpen={() => {}} />);
    expect(screen.getByRole("link", { name: "Play: Deck" }).getAttribute("href")).toBe("/ui/play/d1?share=sh1");
    expect(container.querySelector(".out-act")?.textContent).toBe("Play");
    rerender(<SharedRow o={out({ kind: "audio", title: "Talk" })} shareId="sh1" on={false} onOpen={() => {}} />);
    expect(screen.getByRole("link", { name: "Listen: Talk" })).toBeTruthy();
    const onOpen = vi.fn();
    rerender(
      <SharedRow
        o={out({ key: "notes:n1", kind: "notes", title: "", display_title: "Untitled study notes", sid: "", id: "n1" })}
        shareId="sh1"
        on={false}
        onOpen={onOpen}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Open: Untitled study notes" }));
    expect(onOpen).toHaveBeenCalled();
  });
});
