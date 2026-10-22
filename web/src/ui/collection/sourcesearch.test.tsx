import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceSearch } from "./SourcesPanel";
import { pageState, staged, stagedCount } from "./state";
import type { Src } from "../sources";

const props = { cid: "c1", open: null, start: null, setCrumb: () => {}, onOpen: () => {}, onGone: () => {} };
const src = (file: string, more: Partial<Src> = {}): Src => ({
  name: file,
  detail: "",
  ok: true,
  url: "",
  icon: "",
  file,
  ...more,
});

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("what a build would read", () => {
  it("leaves out a source that is off, but the collection still counts it", () => {
    const srcs = [src("a.md"), src("b.md", { active: false }), src("c.md", { ok: false, file: "" })];
    expect(staged(srcs)).toEqual(["a.md"]);
    expect(stagedCount(srcs)).toBe(2);
  });
});

describe("the search over the sources", () => {
  it("shows the source list until something is typed, and no box with no source", () => {
    const S = pageState(props);
    const { container, rerender } = render(
      <SourceSearch S={S} cid="c1" onOpen={() => {}}>
        <p>the list</p>
      </SourceSearch>,
    );
    expect(container.querySelector(".src-search")).toBeNull();
    S.srcs.set([src("a.md")]);
    rerender(
      <SourceSearch S={S} cid="c1" onOpen={() => {}}>
        <p>the list</p>
      </SourceSearch>,
    );
    expect(screen.getByLabelText("Search the sources")).toBeTruthy();
    expect(screen.getByText("the list")).toBeTruthy();
  });

  it("shows the passages found and opens a source with the passage", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", (u: string) => {
      urls.push(u);
      return reply(200, [{ name: "a.md", title: "Reefs", excerpt: "Coral shelters fish.", start: 0, end: 20 }]);
    });
    const S = pageState(props);
    S.srcs.set([src("a.md")]);
    const onOpen = vi.fn();
    render(
      <SourceSearch S={S} cid="c1" onOpen={onOpen}>
        <p>the list</p>
      </SourceSearch>,
    );
    fireEvent.change(screen.getByLabelText("Search the sources"), { target: { value: "coral" } });
    expect(screen.queryByText("the list")).toBeNull();
    const hit = await screen.findByText("Coral shelters fish.");
    expect(urls[0]).toContain("/collections/c1/search?q=coral");
    fireEvent.click(hit);
    expect(onOpen).toHaveBeenCalledWith("a.md", "Coral shelters fish.");
  });

  it("says when nothing matches, and why the search failed", async () => {
    const fetch = vi.fn().mockImplementationOnce(() => reply(200, []));
    vi.stubGlobal("fetch", fetch);
    const S = pageState(props);
    S.srcs.set([src("a.md")]);
    render(
      <SourceSearch S={S} cid="c1" onOpen={() => {}}>
        <p>the list</p>
      </SourceSearch>,
    );
    const box = screen.getByLabelText("Search the sources");
    fireEvent.change(box, { target: { value: "zebra" } });
    expect(await screen.findByText(/Nothing in the sources matches “zebra”/)).toBeTruthy();
    fetch.mockImplementationOnce(() => reply(500, { detail: "The model is down." }));
    fireEvent.change(box, { target: { value: "otter" } });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("The search failed."));
  });

  it("goes back to the list when the box is cleared", async () => {
    vi.stubGlobal("fetch", () => reply(200, []));
    const S = pageState(props);
    S.srcs.set([src("a.md")]);
    render(
      <SourceSearch S={S} cid="c1" onOpen={() => {}}>
        <p>the list</p>
      </SourceSearch>,
    );
    const box = screen.getByLabelText("Search the sources");
    fireEvent.change(box, { target: { value: "x1" } });
    fireEvent.change(box, { target: { value: "" } });
    expect(screen.getByText("the list")).toBeTruthy();
  });

  it("searches again when a source is switched off or added", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", (u: string) => {
      urls.push(u);
      return reply(200, []);
    });
    const S = pageState(props);
    S.srcs.set([src("a.md")]);
    render(
      <SourceSearch S={S} cid="c1" onOpen={() => {}}>
        <p>the list</p>
      </SourceSearch>,
    );
    fireEvent.change(screen.getByLabelText("Search the sources"), { target: { value: "otters" } });
    await waitFor(() => expect(urls).toHaveLength(1));
    // Switched off: the same words are searched again.
    act(() => S.srcs.set([src("a.md", { active: false })]));
    await waitFor(() => expect(urls).toHaveLength(2));
    // One more added.
    act(() => S.srcs.set([src("a.md", { active: false }), src("b.md")]));
    await waitFor(() => expect(urls).toHaveLength(3));
    // A switch finishing: what it did is now on the server, so search again.
    act(() => S.rowBusy.set({ "src:a.md": "Switching…" }));
    await waitFor(() => expect(urls).toHaveLength(4));
    act(() => S.rowBusy.set({}));
    await waitFor(() => expect(urls).toHaveLength(5));
    // A change that is not one of those leaves the search alone.
    act(() => S.srcs.set([src("a.md", { active: false, detail: "x" }), src("b.md")]));
    await new Promise((r) => setTimeout(r, 450));
    expect(urls).toHaveLength(5);
  });
});
