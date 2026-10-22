import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { JobLog, since, type LogLine } from "./joblog";
import { Progress } from "./outputs";

afterEach(cleanup);

const T = 1_700_000_000_000;
const lines: LogLine[] = [
  { at: T, level: "info", text: "Started" },
  { at: T + 1_000, level: "step", text: "[2/5] Writing the script" },
  {
    at: T + 8_400,
    level: "warn",
    text: "claude-sonnet did not answer (8.0s): timeout",
  },
  { at: T + 75_000, level: "ok", text: "Script written: 6 parts, 80 lines" },
];

describe("a job's log", () => {
  it("says nothing until the work has said something", () => {
    const { container } = render(<JobLog lines={[]} />);
    expect(container.textContent).toBe("");
  });

  it("is closed to its latest line, and opens to every line with its time", () => {
    const { container } = render(<JobLog lines={lines} />);
    const details = container.querySelector("details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    // Closed: the count and the latest line.
    expect(screen.getByText("4")).toBeTruthy();
    expect(container.querySelector(".jl-last")?.textContent).toBe(
      "Script written: 6 parts, 80 lines",
    );
    // Opened: every line, timed from the first.
    details.open = true;
    fireEvent(details, new Event("toggle"));
    const rows = [...container.querySelectorAll(".jl-row")].filter((r) =>
      r.querySelector(".jl-x"),
    );
    expect(rows.map((r) => r.querySelector(".jl-t")?.textContent)).toEqual([
      "+0:00",
      "+0:01",
      "+0:08",
      "+1:15",
    ]);
    expect(rows.map((r) => r.className)).toEqual([
      "jl-row info",
      "jl-row step",
      "jl-row warn",
      "jl-row ok",
    ]);
    expect(container.querySelector(".jl-last")).toBeNull();
    expect(screen.getByRole("log")).toBeTruthy();
  });

  it("stops blinking once the work is over", () => {
    const { container, rerender } = render(<JobLog lines={lines} />);
    expect(container.querySelector(".jl-caret")).toBeTruthy();
    expect(container.querySelector("details")?.className).toContain("live");
    rerender(<JobLog lines={lines} live={false} />);
    expect(container.querySelector(".jl-caret")).toBeNull();
    expect(container.querySelector("details")?.className).not.toContain("live");
  });

  it("counts time in minutes and seconds", () => {
    expect(since(T, T - 5)).toBe("+0:00");
    expect(since(T, T + 59_400)).toBe("+0:59");
    expect(since(T, T + 600_000)).toBe("+10:00");
  });

  it("sits under a build's progress bar", () => {
    const at = {
      session_id: "s1",
      step: "script",
      label: "Writing the script",
      steps_done: 2,
      steps_total: 5,
      log: lines,
    };
    const { container } = render(<Progress at={at} />);
    expect(container.textContent).toContain("Writing the script · 2/5");
    expect(container.querySelector(".joblog")).toBeTruthy();
    // A stream that sends no log leaves the row as it was.
    cleanup();
    const bare = render(<Progress at={{ ...at, log: undefined }} />);
    expect(bare.container.querySelector(".joblog")).toBeNull();
  });
});
