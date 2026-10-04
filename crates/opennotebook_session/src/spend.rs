//! What a prep actually spent on model calls.
//!
//! Every model call returns `opennotebook_ai`'s `TokenUsage`: tokens in and
//! out and, when the provider reports it (OpenRouter's `usage.cost`), the
//! billed USD. A prep runs inside [`scope`], and each call made in it adds
//! one [`Call`] to the scope's [`Ledger`]; the prep then stores the total on
//! its session row and logs it by step.
//!
//! Task-local rather than a global, so a call made outside a prep — a mind
//! map, notes, a title — is never counted against a build running beside it.
//! Such a call is logged on its own line instead.

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::sync::{Arc, Mutex};

use opennotebook_ai::TokenUsage;

/// One model call.
#[derive(Debug, Clone, PartialEq)]
pub struct Call {
    pub step: String,
    pub model: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
    /// The provider's billed cost, when it reported one.
    pub cost_usd: Option<f64>,
    /// False when the response carried no usage at all.
    pub has_usage: bool,
}

/// USD per token (prompt, completion), by model id.
pub type Prices = HashMap<String, (f64, f64)>;

/// Every call made in one prep, and the prices that cost a call whose provider
/// reported none.
#[derive(Debug, Default)]
pub struct Ledger {
    calls: Mutex<Vec<Call>>,
    prices: Prices,
}

/// A step's share of the spend.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StepSpend {
    pub calls: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub usd: f64,
    /// Calls whose cost could not be known.
    pub unpriced: u64,
}

/// The ledger summed, in total and by step.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Totals {
    pub total: StepSpend,
    pub by_step: BTreeMap<String, StepSpend>,
}

impl Totals {
    /// The spend to store: `None` when any call could not be priced, since the
    /// sum would then claim less than was spent.
    pub fn known_usd(&self) -> Option<f64> {
        (self.total.unpriced == 0).then_some(self.total.usd)
    }

    /// One log line: total, tokens, and each step.
    pub fn log_line(&self) -> String {
        let steps: Vec<String> = self
            .by_step
            .iter()
            .map(|(step, s)| {
                format!(
                    "{step} ${:.4} ({} call{})",
                    s.usd,
                    s.calls,
                    if s.calls == 1 { "" } else { "s" }
                )
            })
            .collect();
        let mut line = format!(
            "spent ${:.4} on {} model call{} ({} tokens in, {} out)",
            self.total.usd,
            self.total.calls,
            if self.total.calls == 1 { "" } else { "s" },
            self.total.input_tokens,
            self.total.output_tokens
        );
        if self.total.unpriced > 0 {
            line.push_str(&format!(
                ", {} call{} not priced so the real spend is higher",
                self.total.unpriced,
                if self.total.unpriced == 1 { "" } else { "s" }
            ));
        }
        if !steps.is_empty() {
            line.push_str(": ");
            line.push_str(&steps.join(", "));
        }
        line
    }
}

impl Ledger {
    pub fn new(prices: Prices) -> Arc<Self> {
        Arc::new(Self {
            calls: Mutex::new(Vec::new()),
            prices,
        })
    }

    pub fn push(&self, call: Call) {
        self.calls
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .push(call);
    }

    /// The provider's cost when it gave one, else the tokens at the catalog
    /// price; `None` when neither is known.
    fn cost_of(&self, c: &Call) -> Option<f64> {
        if let Some(usd) = c.cost_usd {
            return Some(usd);
        }
        if !c.has_usage {
            return None;
        }
        let (prompt, completion) = self.prices.get(&c.model)?;
        Some(c.input_tokens as f64 * prompt + c.output_tokens as f64 * completion)
    }

    pub fn totals(&self) -> Totals {
        let calls = self.calls.lock().unwrap_or_else(|p| p.into_inner());
        let mut t = Totals::default();
        for c in calls.iter() {
            let cost = self.cost_of(c);
            for s in [&mut t.total, t.by_step.entry(c.step.clone()).or_default()] {
                s.calls += 1;
                s.input_tokens += c.input_tokens;
                s.output_tokens += c.output_tokens;
                match cost {
                    Some(usd) => s.usd += usd,
                    None => s.unpriced += 1,
                }
            }
        }
        t
    }
}

tokio::task_local! {
    static LEDGER: Arc<Ledger>;
}

/// Run `fut` with every model call in it recorded on `ledger`.
pub async fn scope<F: Future>(ledger: Arc<Ledger>, fut: F) -> F::Output {
    LEDGER.scope(ledger, fut).await
}

/// The totals of the ledger in scope, if there is one.
pub fn current() -> Option<Totals> {
    LEDGER.try_with(|l| l.totals()).ok()
}

/// Record one call. Inside a prep it goes on the prep's ledger; outside one it
/// is logged on its own, so a mind map's or a title's cost is still visible.
pub fn record(step: &str, model: &str, usage: Option<&TokenUsage>) {
    let call = Call {
        step: step.to_string(),
        model: model.to_string(),
        input_tokens: usage.and_then(|u| u.input_tokens).unwrap_or(0),
        output_tokens: usage.and_then(|u| u.output_tokens).unwrap_or(0),
        cost_usd: usage.and_then(|u| u.cost_usd),
        has_usage: usage.is_some(),
    };
    if LEDGER.try_with(|l| l.push(call.clone())).is_err() {
        let cost = call
            .cost_usd
            .map_or_else(|| "cost not reported".to_string(), |c| format!("${c:.4}"));
        eprintln!(
            "opennotebook: {step} on {model}: {cost} ({} tokens in, {} out)",
            call.input_tokens, call.output_tokens
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn usage(i: u64, o: u64, cost: Option<f64>) -> TokenUsage {
        TokenUsage {
            input_tokens: Some(i),
            output_tokens: Some(o),
            cost_usd: cost,
        }
    }

    #[tokio::test]
    async fn calls_in_scope_are_summed_by_step_and_priced_when_unreported() {
        let ledger = Ledger::new(Prices::from([("m".to_string(), (1e-6, 5e-6))]));
        scope(ledger.clone(), async {
            record("outline", "m", Some(&usage(1000, 200, Some(0.01))));
            // No reported cost: 2000 * 1e-6 + 400 * 5e-6 = 0.004.
            record("slides", "m", Some(&usage(2000, 400, None)));
            record("slides", "m", Some(&usage(2000, 400, None)));
        })
        .await;
        let t = ledger.totals();
        assert_eq!(t.total.calls, 3);
        assert!((t.total.usd - 0.018).abs() < 1e-12, "{}", t.total.usd);
        assert_eq!(t.by_step["slides"].calls, 2);
        assert_eq!(t.known_usd(), Some(t.total.usd));
        assert!(t.log_line().contains("slides $0.0080 (2 calls)"));
    }

    #[tokio::test]
    async fn an_unpriceable_call_makes_the_total_unknown() {
        let ledger = Ledger::new(Prices::new());
        scope(ledger.clone(), async {
            record("outline", "m", Some(&usage(1000, 200, Some(0.01))));
            record("slides", "unknown/model", Some(&usage(10, 10, None)));
            record("edit", "m", None);
        })
        .await;
        let t = ledger.totals();
        assert_eq!(t.total.unpriced, 2);
        assert_eq!(t.known_usd(), None);
        assert!(t.log_line().contains("2 calls not priced"));
    }

    #[tokio::test]
    async fn a_call_outside_a_prep_is_not_counted() {
        let ledger = Ledger::new(Prices::new());
        record("mindmap", "m", Some(&usage(1, 1, Some(1.0))));
        assert!(current().is_none());
        assert_eq!(ledger.totals().total.calls, 0);
    }
}
