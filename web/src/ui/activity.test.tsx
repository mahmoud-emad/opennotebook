import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ACTIVITY_DELAY_MS, ActivityBar } from "./activity";
import { PENDING, call } from "./api";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  PENDING.set(0);
});

const tick = (ms: number) => act(() => vi.advanceTimersByTime(ms));

describe("the activity bar", () => {
  it("stays away from a request that is answered quickly", () => {
    render(<ActivityBar />);
    act(() => PENDING.set(1));
    tick(ACTIVITY_DELAY_MS - 50);
    expect(screen.queryByRole("progressbar")).toBeNull();
    act(() => PENDING.set(0));
    tick(ACTIVITY_DELAY_MS);
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("shows once a request has waited, and goes when the last one is answered", () => {
    render(<ActivityBar />);
    act(() => PENDING.set(2));
    tick(ACTIVITY_DELAY_MS + 10);
    expect(screen.getByRole("progressbar").getAttribute("aria-busy")).toBe("true");
    act(() => PENDING.set(1));
    tick(10);
    expect(screen.getByRole("progressbar")).toBeTruthy();
    act(() => PENDING.set(0));
    tick(10);
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});

describe("counting requests", () => {
  it("counts a request from sent to answered, and an unanswered one too", async () => {
    vi.useRealTimers();
    let answer: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", () => new Promise<Response>((res) => (answer = res)));
    const done = call<unknown>("GET", "/x");
    expect(PENDING.get()).toBe(1);
    answer(new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
    await done;
    expect(PENDING.get()).toBe(0);

    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("Failed to fetch")));
    await expect(call("GET", "/y")).rejects.toThrow();
    expect(PENDING.get()).toBe(0);

    vi.stubGlobal("fetch", () =>
      Promise.resolve(new Response(JSON.stringify({ detail: "No." }), { status: 409, headers: { "Content-Type": "application/json" } })),
    );
    await expect(call("POST", "/z")).rejects.toThrow("No.");
    expect(PENDING.get()).toBe(0);
  });
});
