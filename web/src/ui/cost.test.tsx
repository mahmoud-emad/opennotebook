import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { EstimateBanner, quickEstimate, quickFacts } from "./cost";

const q = {
  priced: true,
  cost_usd: 0.0004,
  cost_high_usd: 0.0008,
  model: "google/gemini-2.5-flash-lite",
  input_tokens: 12000,
  output_tokens: 900,
  sources: 3,
  chars: 48000,
  limit_usd: 0.5,
  over_limit: false,
};

afterEach(cleanup);

describe("the cost of a tool", () => {
  it("puts a map's estimate in the itemised shape a build has", () => {
    const e = quickEstimate(q, "mindmap", "");
    expect([e.total_low_usd, e.total_high_usd]).toEqual([0.0004, 0.0008]);
    expect(e.lines).toHaveLength(1);
    expect(e.lines[0]!.calls_high).toBe(2);
    // A map is checked against the same limit as a build.
    expect([e.limit_usd, e.over_limit]).toEqual([0.5, false]);
    expect(quickFacts(q)).toEqual(["3 sources · 48,000 characters", "by Gemini 2.5 Flash Lite"]);
  });

  it("says a map's cost the same way as a build's, limit included", () => {
    render(<EstimateBanner est={quickEstimate(q, "notes", "")} loading={false} failed={false} />);
    expect(screen.getByRole("status").textContent).toBe("Estimated $0.00040 – $0.00080 · within your $0.50 limit.");
  });

  it("names no limit when there is none", () => {
    render(<EstimateBanner est={quickEstimate({ ...q, limit_usd: 0 }, "notes", "")} loading={false} failed={false} />);
    expect(screen.getByRole("status").textContent).toBe("Estimated $0.00040 – $0.00080.");
  });

  it("says when it is working the cost out, and when it could not", () => {
    const { rerender } = render(<EstimateBanner est={null} loading={true} failed={false} />);
    expect(screen.getByRole("status").textContent).toBe("Working out the cost…");
    rerender(<EstimateBanner est={null} loading={false} failed={true} />);
    expect(screen.getByRole("status").textContent).toBe("The cost could not be estimated.");
  });
});
