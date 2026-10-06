import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { EstimateBanner } from "./cost";
import { CostDialog, LimitNote, type Estimate } from "./dialogs";

// A map's estimate as the server sends it: in a build's itemised shape, with
// its facts worded there.
const e: Estimate = {
  total_low_usd: 0.0004,
  total_typical_usd: 0.0004,
  total_high_usd: 0.0008,
  lines: [
    {
      group: "Mind map",
      step: "Draw the mind map",
      detail: "Reads every source whole in one call.",
      model: "google/gemini-2.5-flash-lite",
      via: "opennotebook · OpenRouter",
      calls_low: 1,
      calls_typical: 1,
      calls_high: 2,
      input_tokens: 12000,
      output_tokens_low: 900,
      output_tokens_typical: 900,
      output_tokens_high: 1800,
      cost_low_usd: 0.0004,
      cost_typical_usd: 0.0004,
      cost_high_usd: 0.0008,
      free: false,
      unpriced: false,
    },
  ],
  assumptions: ["Tokens are counted at about four characters each, the prompt included."],
  sources: 3,
  source_chars: 48000,
  slides: 0,
  speakers: 0,
  style: "",
  slides_tier: "",
  priced_at: "2026-10-05T10:00:00Z",
  minutes: 0,
  limit_usd: 0.5,
  over_limit: false,
  limit_note: null,
  model: "google/gemini-2.5-flash-lite",
  facts: ["3 sources · 48,000 characters", "by Gemini 2.5 Flash Lite"],
};

afterEach(cleanup);

describe("the cost of a tool", () => {
  it("says a map's cost the same way as a build's, limit included", () => {
    render(<EstimateBanner est={e} loading={false} failed={false} />);
    expect(screen.getByRole("status").textContent).toBe("Estimated $0.00040 – $0.00080 · within your $0.50 limit.");
  });

  it("names no limit when there is none", () => {
    render(<EstimateBanner est={{ ...e, limit_usd: 0 }} loading={false} failed={false} />);
    expect(screen.getByRole("status").textContent).toBe("Estimated $0.00040 – $0.00080.");
  });

  it("says when it is working the cost out, and when it could not", () => {
    const { rerender } = render(<EstimateBanner est={null} loading={true} failed={false} />);
    expect(screen.getByRole("status").textContent).toBe("Working out the cost…");
    rerender(<EstimateBanner est={null} loading={false} failed={true} />);
    expect(screen.getByRole("status").textContent).toBe("The cost could not be estimated.");
  });

  it("lists the facts the server words, as they come", () => {
    const { container } = render(
      <CostDialog
        est={e}
        verb="Make"
        loading={false}
        err=""
        onClose={() => {}}
        onRetry={() => {}}
        onBuild={() => {}}
      />,
    );
    const facts = [...container.querySelectorAll(".est-fact")].map((f) => f.textContent);
    expect(facts).toEqual(["3 sources · 48,000 characters", "by Gemini 2.5 Flash Lite"]);
    expect(screen.getByText("Draw the mind map")).toBeTruthy();
  });

  it("says over the limit in the server's words, with Settings as a link", () => {
    const note =
      "This could cost up to $0.81, over your $0.50 limit. Use fewer sources, or raise the limit in Settings › Costs & limits.";
    render(<LimitNote e={{ ...e, over_limit: true, limit_note: note }} className="opt-err" />);
    expect(screen.getByRole("alert").textContent).toBe(note);
    expect(screen.getByRole("button", { name: "Settings › Costs & limits" })).toBeTruthy();
  });

  it("marks a model with no price rather than failing the estimate", () => {
    const unpriced = { ...e, lines: [{ ...e.lines[0]!, unpriced: true }] };
    render(<EstimateBanner est={unpriced} loading={false} failed={false} />);
    expect(screen.getByRole("status").textContent).toContain("not counting a model with no price");
  });
});
