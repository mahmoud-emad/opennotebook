import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WatchPage, clock, onAt, withMoments, type Script } from "./watch";

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

const SCRIPT: Script = {
  title: "How plants eat",
  duration_ms: 90_000,
  chapters: [
    { title: "Light", start_ms: 0, end_ms: 40_000 },
    { title: "Sugar", start_ms: 40_000, end_ms: 90_000 },
  ],
  lines: [
    { start_ms: 0, end_ms: 20_000, text: "Leaves catch light." },
    { start_ms: 40_000, end_ms: 60_000, text: "They make sugar." },
  ],
  scenes: [{ title: "Making sugar", start_ms: 40_000, end_ms: 90_000, labels: ["glucose"], claims: [] }],
};

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("time on the watch page", () => {
  it("says times as a player does, and finds what is on", () => {
    expect(clock(84_900)).toBe("1:24");
    expect(clock(3_723_000)).toBe("1:02:03");
    expect(onAt(SCRIPT.chapters, 39_999)).toBe(0);
    expect(onAt(SCRIPT.chapters, 40_000)).toBe(1);
    expect(onAt(SCRIPT.scenes, 1_000)).toBe(-1);
  });

  it("makes an answer's moments into buttons, not one past the end", () => {
    const html = withMoments("<p>See [1:24] and [9:00].</p>", 90_000);
    expect(html).toContain('data-t="84000"');
    expect(html).toContain("[9:00]");
  });
});

describe("the watch page", () => {
  it("asks the tutor about the moment, and seeks to a moment in the answer", async () => {
    const fetch = vi.fn((_url: string, init?: RequestInit) =>
      init?.method === "POST"
        ? reply(200, {
            answer: "Leaves make glucose [1]. It starts at [0:40].",
            citations: [{ n: 1, name: "a.md", title: "Botany", url: "", excerpt: "Glucose is made." }],
            t_ms: 0,
          })
        : reply(200, SCRIPT),
    );
    vi.stubGlobal("fetch", fetch);
    render(<WatchPage sid="s1" style="whiteboard" share={null} />);
    expect(await screen.findByText("How plants eat")).toBeTruthy();
    expect(fetch.mock.calls[0]![0]).toMatch(/\/api\/sessions\/s1\/video\/script\?style=whiteboard$/);
    await act(async () => {
      fireEvent.click(screen.getByText("Explain this part"));
    });
    const post = fetch.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(post[0]).toMatch(/\/api\/sessions\/s1\/video\/explain$/);
    expect(JSON.parse(String(post[1]!.body))).toMatchObject({ style: "whiteboard", mode: "explain", t_ms: 0 });
    expect(await screen.findByText(/Sources:/)).toBeTruthy();
    const moment = document.querySelector<HTMLButtonElement>('.w-log button.moment[data-t="40000"]')!;
    fireEvent.click(moment);
    expect(document.querySelector("video")!.currentTime).toBe(40);
    // The thread is kept for the next visit.
    expect(localStorage.getItem("watch:s1:whiteboard")).toContain("Leaves make glucose");
  });

  it("follows a share's video without the tutor", async () => {
    const fetch = vi.fn(() => reply(200, SCRIPT));
    vi.stubGlobal("fetch", fetch);
    render(<WatchPage sid="s1" style="slides" share="h2" />);
    expect(await screen.findByText("Leaves catch light.")).toBeTruthy();
    expect(screen.queryByText("Explain")).toBeNull();
    expect((fetch.mock.calls[0] as unknown as [string])[0]).toMatch(/\/api\/shares\/h2\/sessions\/s1\/video\/script/);
    expect(document.querySelector("video")!.getAttribute("src")).toMatch(/\/shares\/h2\/sessions\/s1\/video\?style=slides$/);
  });

  it("names only the chapter playing under the track, and turns captions on and off", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply(200, SCRIPT)));
    render(<WatchPage sid="s1" style="whiteboard" share={null} />);
    expect(await screen.findByText("Chapter 1 of 2")).toBeTruthy();
    // The other chapter's title is only in its hover tip, not on the page.
    expect(document.querySelector(".w-chap-t")?.textContent).toBe("Light");
    expect(document.querySelectorAll(".w-seg")).toHaveLength(2);
    const cc = screen.getByRole("button", { name: "Captions" });
    expect(cc.getAttribute("aria-pressed")).toBe("true");
    expect(document.querySelector("track")?.getAttribute("src")).toMatch(/\/video\/captions\?style=whiteboard$/);
    fireEvent.click(cc);
    expect(cc.getAttribute("aria-pressed")).toBe("false");
    expect(localStorage.getItem("watch-captions")).toBe("off");
    fireEvent.keyDown(window, { key: "c" });
    expect(cc.getAttribute("aria-pressed")).toBe("true");
  });
});
