// What a Studio tool will cost, said the same way for every tool: one banner
// over the Generate button, and one itemised dialog behind Estimate cost.

import { anyUnpriced, usd, usdRange, usdRangeSpoken, type Estimate } from "./dialogs";
import { Icon } from "./Icon";

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
              : est.limit_usd > 0 && est.total_high_usd > est.limit_usd && !est.over_limit
                ? // Over the limit yet allowed: a video overview, whose deck alone
                  // is held to the limit.
                  ` · the deck is within your ${usd(est.limit_usd)} limit; the video itself can cost more.`
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
