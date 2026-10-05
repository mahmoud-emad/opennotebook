import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SessionSummary } from "./api";
import { SessionRow } from "./outputs";

function reply(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

const failed: SessionSummary = {
  sid: "s1",
  title: "Editorial slides",
  display_title: "Editorial slides",
  state: "failed",
  slide_count: 0,
  speakers: 2,
  kind: "session",
  audio_format: "",
  audio_label: "",
  duration_ms: 0,
  description: "",
  created_ms: 0,
  pinned: false,
  collection: "c1",
  spent_usd: 0,
  spent_known: false,
  failure: "The AI account is out of credit. Add credit, then try again.",
  failure_detail: "HTTP 402: insufficient_quota",
  waiting: "",
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Retry on a failed output", () => {
  it("asks the server once to make it again, then reads the list back", async () => {
    const fetch = vi.fn(() => reply(202, { id: "s2" }));
    vi.stubGlobal("fetch", fetch);
    const changed = vi.fn();
    render(<SessionRow s={failed} onChanged={changed} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Retry/ }));
      await Promise.resolve();
    });
    // One call: the server keeps the options and removes the failed one.
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/sessions/s1/retry");
    expect(init.method).toBe("POST");
    expect(changed).toHaveBeenCalled();
    expect(screen.queryByText(/could not be started again/)).toBeNull();
  });

  it("says why under the row when the server refuses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => reply(422, { detail: "This could cost up to $0.30, over your $0.25 limit." })),
    );
    render(<SessionRow s={failed} onChanged={() => {}} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Retry/ }));
      await Promise.resolve();
    });
    expect(
      await screen.findByText(
        "It could not be started again. This could cost up to $0.30, over your $0.25 limit.",
      ),
    ).toBeTruthy();
  });
});
