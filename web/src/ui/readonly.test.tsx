import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cardActions, OutputBadges } from "./home";
import { ItemRow } from "./outputs";
import { ShareDialog, copyModeFact, readOnlyLine, reuseHint, reusedLine } from "./share";
import { SrcRow, type Src } from "./sources";

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("reuse counts and copy modes, in words", () => {
  it("says what a share lets people do with their copies", () => {
    expect(copyModeFact(true)).toBe("Editable copies");
    expect(copyModeFact(false)).toBe("Read-only copies");
    expect(reuseHint(true)).toContain("never touches the original");
    expect(reuseHint(false)).toContain("did not allow edits");
  });

  it("names the original in a read-only copy's note, while it is there", () => {
    expect(readOnlyLine("Reefs")).toBe("Read-only copy of “Reefs” — its author did not allow edits.");
    expect(readOnlyLine(null)).toBe("Read-only copy of a shared collection — its author did not allow edits.");
  });

  it("puts how often it was reused beside Shared, and only on a shared one", () => {
    const { container, rerender } = render(<OutputBadges decks={0} audios={0} maps={0} notes={0} shared reuses={3} />);
    expect(container.textContent).toBe(`Shared${reusedLine(3)}`);
    rerender(<OutputBadges decks={0} audios={0} maps={0} notes={0} shared reuses={0} />);
    expect(container.textContent).toContain("Not reused yet");
    rerender(<OutputBadges decks={0} audios={0} maps={0} notes={0} reuses={4} />);
    expect(container.textContent).toBe("");
  });
});

describe("a read-only copy", () => {
  const of = (acts: ReturnType<typeof cardActions>) => acts.map((a) => a[0]);

  it("offers only pin, the original and delete in its card's menu", () => {
    expect(of(cardActions({ pinned: false, shared: false, read_only: true }, true, false))).toEqual([
      "pin",
      "original",
      "delete",
    ]);
    expect(of(cardActions({ pinned: true, shared: false, read_only: true }, false, false))).toEqual(["pin", "delete"]);
    // An editable copy, or any collection of one's own, offers everything.
    expect(of(cardActions({ pinned: false, shared: true, read_only: false }, true, false))).toEqual([
      "rename",
      "pin",
      "share",
      "original",
      "cover",
      "delete",
    ]);
  });

  it("draws its sources and outputs with no way to remove, rename or delete", () => {
    const src: Src = { name: "Reefs note", detail: "Note · 12 words", ok: true, url: "", icon: "", file: "reefs.md" };
    const { container, rerender } = render(<SrcRow s={src} />);
    expect(container.querySelector(".src-x")).toBeNull();
    rerender(<SrcRow s={src} onRemove={() => {}} />);
    expect(container.querySelector(".src-x")).not.toBeNull();

    const row = (more: object) => (
      <ItemRow icon="diagram-3" what="mind map" title="Map" facts="3 topics" whenMs={0} onOpen={() => {}} {...more} />
    );
    rerender(row({}));
    expect(container.querySelectorAll("button")).toHaveLength(1);
    rerender(row({ onRename: () => {}, onDelete: () => {} }));
    expect(container.querySelectorAll("button").length).toBeGreaterThan(1);
  });
});

describe("the share dialog", () => {
  let posted: Record<string, unknown> | null;
  let share: Record<string, unknown> | null;

  const summary = {
    id: "c1",
    title: "Reefs",
    title_auto: false,
    pinned: false,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    cover_version: "f-1",
    sources: 2,
    decks: 0,
    audios: 0,
    maps: 0,
    notes: 0,
    preparing: 0,
    failed: 0,
    reused_from: null,
    shared: false,
    reuses: 0,
    read_only: false,
  };
  const shareOut = (allow: boolean) => ({
    id: "s1",
    collection_id: "c1",
    include_sources: true,
    outputs: [],
    note: "",
    reuses: 0,
    allow_edits: allow,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
  });

  beforeEach(() => {
    posted = null;
    share = null;
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "POST") {
        posted = JSON.parse(String(init!.body)) as Record<string, unknown>;
        return reply(200, shareOut(posted.allow_edits === true));
      }
      if (url.endsWith("/share")) return reply(200, share);
      if (url.endsWith("/mindmaps") || url.endsWith("/notes")) return reply(200, []);
      return reply(200, { collection: summary, outputs: [] });
    });
  });

  const edits = () => screen.getByRole("switch", { name: "Let people edit their copy and share it again" });

  it("leaves edits off unless the owner turns them on, and sends the choice", async () => {
    render(<ShareDialog cid="c1" title="Reefs" onClose={() => {}} />);
    await act(async () => {});
    expect(edits().getAttribute("aria-checked")).toBe("false");
    expect(screen.getByText("Off: a reused copy is read-only. On: it is theirs to change and publish.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Share" }));
    await act(async () => {});
    expect(posted?.allow_edits).toBe(false);

    fireEvent.click(edits());
    expect(edits().getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Share" }));
    await act(async () => {});
    expect(posted?.allow_edits).toBe(true);
  });

  it("opens on what the share says", async () => {
    share = shareOut(true);
    render(<ShareDialog cid="c1" title="Reefs" onClose={() => {}} />);
    await act(async () => {});
    expect(edits().getAttribute("aria-checked")).toBe("true");
    fireEvent.click(edits());
    fireEvent.click(screen.getByRole("button", { name: "Update share" }));
    await act(async () => {});
    expect(posted?.allow_edits).toBe(false);
  });
});
