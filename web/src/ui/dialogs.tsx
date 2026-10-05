// The studio's own dialogs: the confirm/prompt every screen asks through, and
// the itemised cost of a build before it runs. A port of the old `dialogs.rs`.

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Icon } from "./Icon";
import { SettingsLink, modelName, styleLabel } from "./settings";
import { store, useStore } from "./store";

/** Money for a person, not a ledger. Sub-cent amounts keep two significant
 * digits so a paid step never rounds to a "$0.00" that reads as free. */
export function usd(x: number): string {
  if (x <= 0) return "$0.00";
  if (x < 0.0001) return "< $0.0001";
  if (x >= 1) return `$${x.toFixed(2)}`;
  const mag = Math.floor(Math.log10(x));
  return `$${x.toFixed(Math.max(1 - mag, 2))}`;
}

/** A range when the ends really differ, one "about" figure when they do not. */
export function usdRange(low: number, high: number): string {
  if (high <= 0) return "$0.00";
  if (high <= low * 1.5) return `about ${usd((low + high) / 2)}`;
  return `${usd(low)} – ${usd(high)}`;
}

/** The same range as a screen reader should say it. */
export function usdRangeSpoken(low: number, high: number): string {
  const r = usdRange(low, high).replace("< ", "less than ");
  const [a, b] = r.split(" – ");
  return b === undefined ? r : `between ${a} and ${b}`;
}

/** 850, 12.4k, 1.2M: a count someone can take in at a glance. */
export function countShort(n: number): string {
  if (n >= 1_000_000) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1e3).toFixed(0)}k`;
  if (n >= 1_000) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

/** Money rounded up to the next whole cent, for an amount stated against a limit. */
export function usdUp(x: number): string {
  return `$${(Math.ceil(x * 100 - 1e-6) / 100).toFixed(2)}`;
}

/** One step of a build's estimate, as the server itemises it. */
export type CostLine = {
  group: string;
  step: string;
  detail: string;
  model: string;
  via: string;
  calls_low: number;
  calls_typical: number;
  calls_high: number;
  input_tokens: number;
  output_tokens_low: number;
  output_tokens_typical: number;
  output_tokens_high: number;
  cost_low_usd: number;
  cost_typical_usd: number;
  cost_high_usd: number;
  free: boolean;
  unpriced: boolean;
  price_in_per_million?: number | null;
  price_out_per_million?: number | null;
};

export type Estimate = {
  total_low_usd: number;
  total_typical_usd: number;
  total_high_usd: number;
  lines: CostLine[];
  assumptions: string[];
  sources: number;
  source_chars: number;
  slides: number;
  speakers: number;
  style: string;
  slides_tier: string;
  priced_at: string;
  minutes: number;
  limit_usd: number;
  over_limit: boolean;
};

/** Whether a step's model has no price in the catalog. */
export const anyUnpriced = (e: Estimate) => e.lines.some((l) => l.unpriced);

/** Why a build over the spending limit would be refused, and what to change. */
export function limitLead(e: Estimate, audio: boolean): string {
  const fewer = audio ? "a shorter length" : "fewer slides, a shorter length";
  return `This could cost up to ${usdUp(e.total_high_usd)}, over your ${usd(e.limit_usd)} limit. Use ${fewer}, or raise the limit in `;
}

export function LimitNote({ e, audio, className }: { e: Estimate; audio: boolean; className: string }) {
  return (
    <div className={className} role="alert">
      {limitLead(e, audio)}
      <SettingsLink tab="costs" />.
    </div>
  );
}

/** Every step of a build and what it costs, before it runs. */
export function CostDialog({
  est,
  audio,
  loading,
  err,
  onClose,
  onRetry,
  onBuild,
  facts,
  verb = "Build",
}: {
  est: Estimate | null;
  audio: string | null;
  loading: boolean;
  err: string;
  onClose: () => void;
  onRetry: () => void;
  onBuild: () => void;
  /** Facts to show instead of a build's slides, voices and style: a map's or
   * notes' sources and model. */
  facts?: string[];
  /** What the button does: "Build" a deck or audio, "Make" a map or notes. */
  verb?: string;
}) {
  const [prices, setPrices] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => ref.current?.focus(), []);
  const groups: [string, CostLine[]][] = [];
  for (const l of est?.lines ?? []) {
    const g = groups.find(([k]) => k === l.group);
    if (g) g[1].push(l);
    else groups.push([l.group, [l]]);
  }
  const buildLabel = !est
    ? verb === "Build"
      ? "Start building"
      : `${verb} it`
    : anyUnpriced(est)
      ? `${verb} (cost unknown)`
      : est.total_typical_usd > 0
        ? `${verb} for about ${usd(est.total_typical_usd)}`
        : `${verb} (free)`;
  return (
    <>
      <div className="set-veil" onClick={onClose} />
      <div
        ref={ref}
        className="set-dialog est-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="est-title"
        tabIndex={-1}
        onKeyDown={(e) => e.key === "Escape" && onClose()}
      >
        <div className="set-head est-head">
          <h3 id="est-title">Estimated cost</h3>
          <button className="icon-btn" aria-label="Close" title="Close" onClick={onClose}>
            <Icon name="x-lg" />
          </button>
        </div>
        <div className="est-body">
          {loading && !est ? (
            <div className="set-note">
              <span className="mini-spin" /> Working out what this build will cost…
            </div>
          ) : err ? (
            <div className="set-note err">
              The cost could not be estimated: {err}
              <div>
                <button className="est-retry" onClick={onRetry}>
                  Try again
                </button>
              </div>
            </div>
          ) : est ? (
            <>
              <div className="est-total">
                <div className="est-amt" aria-label={usdRangeSpoken(est.total_low_usd, est.total_high_usd)}>
                  {usdRange(est.total_low_usd, est.total_high_usd)}
                </div>
                <div className="est-sub">
                  USD · most likely about <strong>{usd(est.total_typical_usd)}</strong>
                  {loading && <span className="dim"> · updating…</span>}
                </div>
              </div>
              <div className="est-facts">
                {facts ? (
                  facts.map((f) => (
                    <span key={f} className="est-fact">
                      {f}
                    </span>
                  ))
                ) : (
                  <>
                    {audio ? (
                      <span className="est-fact">{audio}</span>
                    ) : (
                      <span className="est-fact">{est.slides} slides</span>
                    )}
                    {est.minutes > 0 && <span className="est-fact">about {est.minutes} minutes</span>}
                    <span className="est-fact">{est.speakers === 1 ? "1 voice" : `${est.speakers} voices`}</span>
                    <span className="est-fact">
                      {est.sources === 1 ? "1 source" : `${est.sources} sources`}
                      {` · ${countShort(est.source_chars)} characters`}
                    </span>
                    {!audio && (
                      <>
                        <span className="est-fact">{styleLabel(est.style)} style</span>
                        <span className="est-fact">slides by {modelName(est.slides_tier)}</span>
                      </>
                    )}
                  </>
                )}
              </div>
              {est.over_limit ? (
                <LimitNote e={est} audio={!!audio} className="set-note err est-limit" />
              ) : est.limit_usd > 0 ? (
                <div className="est-fine est-limit">
                  Within your {usd(est.limit_usd)} limit per output. <SettingsLink tab="costs" />
                </div>
              ) : null}
              {groups.map(([group, lines]) => {
                const low = lines.reduce((a, l) => a + l.cost_low_usd, 0);
                const high = lines.reduce((a, l) => a + l.cost_high_usd, 0);
                const allFree = lines.every((l) => l.free);
                return (
                  <section key={group} className="est-group">
                    <div className="est-gh">
                      <h4>{group}</h4>
                      <span className="est-gsum">{allFree ? "Free" : usdRange(low, high)}</span>
                    </div>
                    <table className="est-tbl">
                      <colgroup>
                        <col className="c-step" />
                        <col className="c-model" />
                        <col className="c-tok" />
                        <col className="c-cost" />
                      </colgroup>
                      <thead>
                        <tr>
                          <th scope="col">Step</th>
                          <th scope="col" className="est-model-h">
                            Model
                          </th>
                          <th scope="col" className="num est-tok-h">
                            Tokens
                          </th>
                          <th scope="col" className="num">
                            Cost
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {lines.map((l) => (
                          <CostRow key={l.step} l={l} prices={prices} />
                        ))}
                      </tbody>
                    </table>
                  </section>
                );
              })}
              <label className="est-toggle">
                <input type="checkbox" checked={prices} onChange={(e) => setPrices(e.target.checked)} />
                Show unit prices
              </label>
              <details className="est-how">
                <summary>How this was worked out</summary>
                <ul>
                  {est.assumptions.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                </ul>
              </details>
            </>
          ) : null}
        </div>
        <div className="est-foot">
          <span className="est-fine">
            An estimate, not a quote: you pay for what the models actually use.
            {est && est.priced_at !== "" && ` Prices as of ${est.priced_at.slice(0, 10)}.`}
          </span>
          <div className="est-actions">
            <button onClick={onClose}>Close</button>
            <button className="primary" disabled={!est || est.over_limit} onClick={onBuild}>
              {buildLabel}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

/** One step: what it does and why that count, the model, the tokens, the cost. */
function CostRow({ l, prices }: { l: CostLine; prices: boolean }) {
  let cost;
  if (l.free) cost = <span className="est-free">Free</span>;
  else if (l.unpriced)
    cost = (
      <span className="est-unpriced" title="This model has no price in the catalog">
        no price
      </span>
    );
  else {
    const label =
      Math.abs(l.cost_high_usd - l.cost_low_usd) < 1e-9
        ? usd(l.cost_typical_usd)
        : l.cost_low_usd <= 0
          ? `up to ${usd(l.cost_high_usd)}`
          : `${usd(l.cost_low_usd)} – ${usd(l.cost_high_usd)}`;
    cost = <span aria-label={usdRangeSpoken(l.cost_low_usd, l.cost_high_usd)}>{label}</span>;
  }
  const calls = l.calls_low === l.calls_high ? String(l.calls_typical) : `${l.calls_low}–${l.calls_high}`;
  const out =
    l.output_tokens_low === l.output_tokens_high
      ? countShort(l.output_tokens_typical)
      : `${countShort(l.output_tokens_low)}–${countShort(l.output_tokens_high)}`;
  return (
    <tr className={l.free ? "free" : ""}>
      <td>
        <div className="est-step">{l.step}</div>
        <div className="est-dt">{l.detail}</div>
        {prices && !l.free && !l.unpriced && (
          <div className="est-price">
            {l.price_in_per_million != null &&
              l.price_out_per_million != null &&
              `$${l.price_in_per_million.toFixed(2)} / 1M in · $${l.price_out_per_million.toFixed(2)} / 1M out`}
          </div>
        )}
      </td>
      <td className="est-model">
        {l.model ? <code title={l.model}>{l.model}</code> : <span className="dim">—</span>}
        <div className="est-via">{l.via}</div>
      </td>
      <td className="num est-tok">
        {l.free ? (
          <span className="dim">{l.calls_typical > 0 ? `${calls} ×` : "—"}</span>
        ) : (
          <>
            <div>{countShort(l.input_tokens)} in</div>
            <div className="dim">{out} out</div>
            <div className="est-calls">
              {calls} call{calls !== "1" && "s"}
            </div>
          </>
        )}
      </td>
      <td className="num est-cost">{cost}</td>
    </tr>
  );
}

// ── the confirm/prompt every screen asks through ─────────────────────────────

/** A question put to the person in the studio's own dialog: a yes/no when
 * `input` is null, a one-line text answer when it holds the starting value. */
export type Ask = {
  title: string;
  body: string;
  okLabel: string;
  danger: boolean;
  input: string | null;
  onOk: (value: string) => void;
};

export const ASK = store<Ask | null>(null);

export function askConfirm(title: string, body: string, okLabel: string, onOk: (v: string) => void): void {
  ASK.set({ title, body, okLabel, danger: true, input: null, onOk });
}

export function askPrompt(title: string, current: string, okLabel: string, onOk: (v: string) => void): void {
  ASK.set({ title, body: "", okLabel, danger: false, input: current, onOk });
}

/** Whether keyboard focus is on a button (or a link), whose own Enter press
 * the browser turns into a click. */
export function focusOnButton(): boolean {
  const tag = document.activeElement?.tagName;
  return tag === "BUTTON" || tag === "A";
}

export function AskDialog() {
  const a = useStore(ASK);
  // Keyed on the question, so each one starts from its own value.
  return a ? <AskBody key={a.title + (a.input ?? "")} a={a} /> : null;
}

function AskBody({ a }: { a: Ask }) {
  const [value, setValue] = useState(a.input ?? "");
  const box = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const isPrompt = a.input !== null;
  useEffect(() => {
    if (isPrompt) field.current?.focus();
    else box.current?.focus();
  }, [isPrompt]);
  const close = () => ASK.set(null);
  const submit = () => {
    if (isPrompt && !value.trim()) return;
    ASK.set(null);
    a.onOk(value);
  };
  // Enter confirms only from the dialog itself or its text field. On a focused
  // button it is that button's own press: Enter on Cancel must cancel.
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") close();
    else if (e.key === "Enter" && !focusOnButton()) {
      e.preventDefault();
      submit();
    }
  };
  return (
    <>
      <div className="set-veil" onClick={close} />
      <div
        ref={box}
        className="set-dialog ask-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="ask-title"
        tabIndex={-1}
        onKeyDown={onKey}
      >
        <div className="set-head">
          <h3 id="ask-title">{a.title}</h3>
          <button className="icon-btn" aria-label="Close" title="Close" onClick={close}>
            <Icon name="x-lg" />
          </button>
        </div>
        {a.body && <p className="ask-body">{a.body}</p>}
        {isPrompt && (
          <input ref={field} type="text" value={value} onChange={(e) => setValue(e.target.value)} />
        )}
        <div className="est-actions ask-actions">
          <button onClick={close}>Cancel</button>
          <button
            className={a.danger ? "primary danger" : "primary"}
            disabled={isPrompt && !value.trim()}
            onClick={submit}
          >
            {a.okLabel}
          </button>
        </div>
      </div>
    </>
  );
}
