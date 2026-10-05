// What a Studio tool will cost, said the same way for every tool: one banner
// over the Generate button, and one itemised dialog behind Estimate cost.

import type { QuickEstimate } from "./api-studio";
import { anyUnpriced, usd, usdRange, usdRangeSpoken, type Estimate } from "./dialogs";
import { Icon } from "./Icon";
import { modelName } from "./settings";

/** "Estimated $0.057 – $0.15 · within your $0.50 limit." Every tool is
 * checked against the limit, so every tool names it. */
export function EstimateBanner({ est, loading, failed }: { est: Estimate | null; loading: boolean; failed: boolean }) {
  return (
    <div className="est-banner" role="status" aria-label="Estimated cost">
      <span className="est-i" aria-hidden="true">
        <Icon name="info-circle" />
      </span>
      <div className="est-bt">
        {loading && est === null ? (
          <span className="dim">Working out the cost…</span>
        ) : est !== null ? (
          <>
            <span>Estimated </span>
            <strong aria-label={`${usdRangeSpoken(est.total_low_usd, est.total_high_usd)} US dollars`}>
              {usdRange(est.total_low_usd, est.total_high_usd)}
            </strong>
            {anyUnpriced(est)
              ? ", not counting a model with no price, so the real cost is unknown."
              : est.limit_usd > 0
                ? ` · within your ${usd(est.limit_usd)} limit.`
                : "."}
          </>
        ) : failed ? (
          <span className="dim">The cost could not be estimated.</span>
        ) : null}
      </div>
    </div>
  );
}

/** A map's or notes' estimate in the itemised shape a build's has, so the
 * same banner and dialog show it: one step, one model, one or two calls. */
export function quickEstimate(q: QuickEstimate, what: "mindmap" | "notes", pricedAt: string): Estimate {
  const step = what === "mindmap" ? "Draw the mind map" : "Write the study notes";
  const high = q.cost_high_usd > 0 ? q.cost_high_usd : q.cost_usd;
  return {
    total_low_usd: q.cost_usd,
    total_typical_usd: q.cost_usd,
    total_high_usd: high,
    lines: [
      {
        group: what === "mindmap" ? "Mind map" : "Study notes",
        step,
        detail: "Reads every source whole in one call; a second call when the first answer comes back too thin.",
        model: q.model,
        via: "the AI endpoint",
        calls_low: 1,
        calls_typical: 1,
        calls_high: high > q.cost_usd ? 2 : 1,
        input_tokens: q.input_tokens,
        output_tokens_low: q.output_tokens,
        output_tokens_typical: q.output_tokens,
        output_tokens_high: high > q.cost_usd ? q.output_tokens * 2 : q.output_tokens,
        cost_low_usd: q.cost_usd,
        cost_typical_usd: q.cost_usd,
        cost_high_usd: high,
        free: false,
        unpriced: !q.priced,
      },
    ],
    assumptions: [
      "Tokens are counted at about four characters each, the prompt included.",
      "Prices are the model's list prices on the AI endpoint.",
    ],
    sources: q.sources,
    source_chars: q.chars,
    slides: 0,
    speakers: 0,
    style: "",
    slides_tier: "",
    priced_at: pricedAt,
    minutes: 0,
    // Checked against the same spending limit as a build.
    limit_usd: q.limit_usd,
    over_limit: q.over_limit,
  };
}

/** The facts a map's or notes' dialog shows in place of a build's slides and
 * voices. */
export function quickFacts(q: QuickEstimate): string[] {
  return [`${q.sources === 1 ? "1 source" : `${q.sources} sources`} · ${q.chars.toLocaleString("en-US")} characters`, `by ${modelName(q.model)}`];
}
