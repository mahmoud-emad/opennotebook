import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VideoLine, VideoOptions, type Video } from "./video";

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

const none = (style: "whiteboard" | "slides"): Video => ({ style, state: "none" });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("a video on an output's row", () => {
  it("offers a video of a finished output that has none, and starts one", async () => {
    const fetch = vi.fn((_url: string, init?: RequestInit) =>
      init?.method === "POST"
        ? reply(202, { style: "whiteboard", state: "rendering", job_id: "j1" })
        : reply(200, [none("slides"), none("whiteboard")]),
    );
    vi.stubGlobal("fetch", fetch);
    render(<VideoLine sid="s1" ready={true} ro={false} />);
    const make = await screen.findByText("Make a video of this");
    await act(async () => {
      fireEvent.click(make);
    });
    const post = fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post?.[0]).toMatch(/\/api\/sessions\/s1\/video$/);
    // In the theme last chosen in this browser: the whiteboard, here.
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ style: "whiteboard", theme: "whiteboard" });
  });

  it("says nothing for a read-only copy, or one still being made", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply(200, [none("slides"), none("whiteboard")])));
    const { container } = render(<VideoLine sid="s1" ready={false} ro={false} />);
    await act(async () => {});
    expect(container.textContent).toBe("");
  });

  it("shows a ready video with its checks, and links to its watch page", async () => {
    const ready: Video = {
      style: "whiteboard", state: "ready", playable: true, duration_ms: 188_430, claims: 38,
      supported: 38,
    };  // prettier-ignore
    vi.stubGlobal("fetch", vi.fn(() => reply(200, [none("slides"), ready])));
    render(<VideoLine sid="s1" ready={true} ro={false} />);
    expect(await screen.findByText(/Whiteboard video · 188 s · 38\/38 claims checked/)).toBeTruthy();
    expect(screen.getByText("Watch").closest("a")?.getAttribute("href")).toMatch(
      /\/ui\/watch\/s1\?style=whiteboard$/,
    );
  });

  it("says why a video failed, and offers to try again", async () => {
    const failed: Video = { style: "whiteboard", state: "failed", failure: "The encoder is missing." };
    vi.stubGlobal("fetch", vi.fn(() => reply(200, [none("slides"), failed])));
    render(<VideoLine sid="s1" ready={true} ro={false} />);
    expect(await screen.findByText("The encoder is missing.")).toBeTruthy();
    expect(screen.getByText("Try again")).toBeTruthy();
  });
});

const THEMES = [
  { id: "whiteboard", label: "Whiteboard", family: "drawn" },
  { id: "chalkboard", label: "Chalkboard", family: "drawn" },
  { id: "watercolor", label: "Watercolor", family: "illustrated" },
];

describe("the Video overview tool", () => {
  it("offers the whiteboard's themes, sends the one chosen, and remembers it", async () => {
    localStorage.clear();
    const fetch = vi.fn((url: string, init?: RequestInit) =>
      init?.method === "POST"
        ? reply(202, { session: { id: "s9" }, video: { style: "whiteboard", state: "waiting" } })
        : url.endsWith("/video/themes")
          ? reply(200, THEMES)
          : reply(404, {}),
    );
    vi.stubGlobal("fetch", fetch);
    render(<VideoOptions cid="c1" onMade={() => {}} onCancel={() => {}} />);
    const chalk = await screen.findByRole("radio", { name: /Chalkboard/ });
    expect(screen.getByText("+ about 30 cents")).toBeTruthy();
    fireEvent.click(chalk);
    await act(async () => {
      fireEvent.click(screen.getByText("Make video"));
    });
    const post = fetch.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(JSON.parse(String(post[1]!.body))).toMatchObject({ style: "whiteboard", theme: "chalkboard" });
    expect(localStorage.getItem("video-theme:c1")).toBe("chalkboard");
    // Slides have no theme to choose.
    fireEvent.click(screen.getByText("Slides"));
    expect(screen.queryByRole("radiogroup", { name: "Theme" })).toBeNull();
  });


  it("makes an overview of the collection in the chosen style and length", async () => {
    const fetch = vi.fn(() =>
      reply(202, { session: { id: "s9" }, video: { style: "slides", state: "waiting" } }),
    );
    vi.stubGlobal("fetch", fetch);
    const made = vi.fn();
    render(<VideoOptions cid="c1" onMade={made} onCancel={() => {}} />);
    fireEvent.click(screen.getByText("Slides"));
    fireEvent.click(screen.getByText("Shorter"));
    await act(async () => {
      fireEvent.click(screen.getByText("Make video"));
    });
    const calls = fetch.mock.calls as unknown as [string, RequestInit | undefined][];
    const [url, init] = calls.find(([, i]) => i?.method === "POST")!;
    expect(url).toMatch(/\/api\/collections\/c1\/videos$/);
    expect(JSON.parse(String(init!.body))).toEqual({ style: "slides", length: "short" });
    expect(made).toHaveBeenCalledOnce();
  });

  it("says why it could not start", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => reply(503, { detail: "The studio cannot make videos: ffmpeg is not installed." })),
    );
    render(<VideoOptions cid="c1" onMade={() => {}} onCancel={() => {}} />);
    await act(async () => {
      fireEvent.click(screen.getByText("Make video"));
    });
    expect(screen.getByRole("alert").textContent).toMatch(/ffmpeg is not installed/);
  });
});
