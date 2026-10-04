//! The live half of the estimate: gather the real inputs, then price them with
//! `estimate`. Nothing here calls a model.
//!
//! - the sources: the collection's staged files, measured on disk;
//! - the script and slide models, the session length: the studio settings;
//! - the parts and voices: the build's [`Shape`], the rule the pipeline builds by;
//! - the prices: the model endpoint's catalog (OpenRouter's `/models`) — the
//!   same endpoint every model call in the build pays, Q&A extraction included.

use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::estimate::{self, Inputs, Price, Prices};
use crate::session::{CostLine, SessionEstimateOutput};
use crate::session_impl::{BuildPlan, Shape};

/// Prices change rarely; one catalog read serves every estimate for this long.
const PRICE_TTL: Duration = Duration::from_secs(600);

struct Cached {
    at: Instant,
    stamp: String,
    prices: Prices,
}

static CACHE: Mutex<Option<Cached>> = Mutex::new(None);

pub async fn estimate(sid: &str, plan: &BuildPlan) -> anyhow::Result<SessionEstimateOutput> {
    let dir = crate::create::staging_dir(sid);
    let source_chars: Vec<u64> = plan
        .staged
        .iter()
        .map(|name| chars_of(&dir.join(name)))
        .collect();
    // A build from the dialog never researches: research runs in the chat,
    // before the build, and is billed there.
    compute(source_chars, &plan.shape(), &plan.style, false).await
}

/// Refuse a build whose high estimate is over the studio's spending limit.
///
/// The HIGH end, not the typical one: the limit is a promise about the most a
/// session costs, and a build that usually fits but sometimes does not would
/// break it. `Ok` when there is no limit, and when the estimate itself cannot be
/// made — a price catalog that is down is not a reason to stop a build the
/// person asked for, and the build reads the same settings either way.
pub async fn check_limit(resource_dir: &Path, shape: &Shape, research: bool) -> Result<(), String> {
    let policy = opennotebook_session::settings::cost_policy().await;
    let Some(limit) = policy.max_build_usd else {
        return Ok(());
    };
    let source_chars: Vec<u64> = std::fs::read_dir(resource_dir)
        .map(|rd| {
            rd.flatten()
                .filter(|e| e.path().is_file())
                .map(|e| chars_of(&e.path()))
                .collect()
        })
        .unwrap_or_default();
    let e = match compute(source_chars, shape, "", research).await {
        Ok(e) => e,
        Err(err) => {
            eprintln!("opennotebook: no estimate for the spending limit ({err}); building anyway");
            return Ok(());
        }
    };
    if e.over_limit {
        return Err(over_limit_message(
            e.total_high_usd,
            limit,
            shape.audio.is_some(),
        ));
    }
    Ok(())
}

/// Why a build was refused, with what would bring it under the limit.
fn over_limit_message(high: f64, limit: f64, audio: bool) -> String {
    let fewer = if audio {
        "a shorter length"
    } else {
        "fewer slides, a shorter length"
    };
    let high = cent_up(high);
    format!(
        "This could cost up to ${high:.2}, over your ${limit:.2} limit. \
         Use {fewer}, or raise the limit in Settings › Costs & limits."
    )
}

/// Up to the next whole cent, so an amount over a limit never prints as the
/// limit itself ($0.2525 against $0.25 reads $0.26). The slack keeps float
/// noise ($0.81 stored as 0.8100000001) from adding a cent.
fn cent_up(x: f64) -> f64 {
    (x * 100.0 - 1e-6).ceil() / 100.0
}

/// Characters in a source file; a file that is not text counts by its size.
fn chars_of(path: &Path) -> u64 {
    std::fs::read_to_string(path)
        .map(|t| t.chars().count() as u64)
        .or_else(|_| std::fs::metadata(path).map(|m| m.len()))
        .unwrap_or(0)
}

async fn compute(
    source_chars: Vec<u64>,
    shape: &Shape,
    style: &str,
    research: bool,
) -> anyhow::Result<SessionEstimateOutput> {
    let policy = opennotebook_session::settings::cost_policy().await;
    // An audio overview runs as long as its format says, not the session
    // length setting.
    let minutes = shape
        .audio
        .as_ref()
        .map_or(policy.session_minutes, |a| a.minutes());
    let slides = shape.slides.max(1);
    let inputs = Inputs {
        source_chars,
        slides,
        speakers: shape.speakers.max(1),
        script_model: opennotebook_session::settings::script_model().await,
        slide_model: policy.slide_model.clone(),
        slide_narration: opennotebook_script::budget::slide_narration(minutes, slides as usize)
            as u64,
        minutes: minutes as u64,
        audio: shape.audio.is_some(),
        research,
    };
    let (prices, stamp) = prices().await?;
    let e = estimate::estimate(&inputs, &prices);
    let limit = policy.max_build_usd.unwrap_or(0.0);
    Ok(SessionEstimateOutput {
        total_low_usd: e.total[0],
        total_typical_usd: e.total[1],
        total_high_usd: e.total[2],
        lines: e.lines.iter().map(wire).collect(),
        assumptions: e.assumptions,
        sources: inputs.source_chars.len() as i64,
        source_chars: e.source_chars as i64,
        slides: inputs.slides as i64,
        speakers: inputs.speakers as i64,
        style: style.to_string(),
        slides_tier: inputs.slide_model.clone(),
        priced_at: stamp,
        minutes: inputs.minutes as i64,
        limit_usd: limit,
        over_limit: limit > 0.0 && e.total[2] > limit,
    })
}

fn wire(l: &estimate::Line) -> CostLine {
    let per_m = |x: f64| x * 1_000_000.0;
    CostLine {
        group: l.group.to_string(),
        step: l.step.clone(),
        detail: l.detail.clone(),
        model: l.model.clone(),
        via: l.via.to_string(),
        calls_low: l.calls.low as i64,
        calls_typical: l.calls.typical as i64,
        calls_high: l.calls.high as i64,
        input_tokens: l.input_tokens as i64,
        output_tokens_low: l.output_tokens.low as i64,
        output_tokens_typical: l.output_tokens.typical as i64,
        output_tokens_high: l.output_tokens.high as i64,
        cost_low_usd: l.cost[0],
        cost_typical_usd: l.cost[1],
        cost_high_usd: l.cost[2],
        free: l.free,
        unpriced: l.unpriced,
        price_in_per_million: l.price.map(|p| per_m(p.prompt)),
        price_out_per_million: l.price.map(|p| per_m(p.completion)),
        // No step generates images any more; the field stays on the wire.
    }
}

/// Every catalog price, (prompt, completion) per token, for pricing what a
/// prep spent on a call whose provider reported no cost. Empty when the
/// catalog is unreachable, which leaves such a call unpriced.
pub async fn spend_prices() -> opennotebook_session::spend::Prices {
    match prices().await {
        Ok((p, _)) => p
            .into_iter()
            .map(|(id, p)| (id, (p.prompt, p.completion)))
            .collect(),
        Err(e) => {
            eprintln!("opennotebook prep: no price catalog for the spend record ({e})");
            Default::default()
        }
    }
}

/// One model's price per token, (prompt, completion), from the same cached
/// catalog the session estimate reads. `None` when the catalog is unreachable
/// or does not list the model.
pub async fn price_of(model: &str) -> Option<(f64, f64)> {
    let (prices, _) = prices().await.ok()?;
    prices.get(model).map(|p| (p.prompt, p.completion))
}

/// The price catalogue for the settings dialog: the cached one, or a fresh
/// read that gives up after a few seconds so a dialog never waits on a provider
/// that is down. `None` when it cannot be had.
pub async fn catalogue() -> Option<Prices> {
    match tokio::time::timeout(Duration::from_secs(4), prices()).await {
        Ok(Ok((p, _))) => Some(p),
        _ => None,
    }
}

/// Whether the model endpoint's catalogue lists `model`. `Err` says why it could
/// not be asked, which is not a reason to refuse a model.
pub async fn model_listed(model: &str) -> Result<bool, String> {
    match tokio::time::timeout(Duration::from_secs(6), prices()).await {
        Ok(Ok((p, _))) => Ok(p.contains_key(model)),
        Ok(Err(e)) => Err(format!("{e:#}")),
        Err(_) => Err("the model endpoint did not answer in time".into()),
    }
}

/// A model's price as the settings dialog shows it: "$1 / $5 per M tokens",
/// input then output.
pub fn price_hint(p: &Price) -> String {
    let usd = |per_token: f64| {
        let m = per_token * 1_000_000.0;
        let s = if m >= 10.0 {
            format!("{m:.0}")
        } else if m >= 0.1 {
            format!("{m:.2}")
        } else {
            format!("{m:.3}")
        };
        let s = if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_string()
        } else {
            s
        };
        format!("${s}")
    };
    if p.prompt == 0.0 && p.completion == 0.0 {
        return "free".into();
    }
    format!("{} / {} per M tokens", usd(p.prompt), usd(p.completion))
}

async fn prices() -> anyhow::Result<(Prices, String)> {
    if let Some(c) = CACHE.lock().unwrap().as_ref()
        && c.at.elapsed() < PRICE_TTL
    {
        return Ok((c.prices.clone(), c.stamp.clone()));
    }
    // The catalog needs no key on OpenRouter, so a missing key does not stop
    // the estimate; a local server that wants one gets it.
    let provider = match opennotebook_session::ai::provider().await {
        Ok(p) => p,
        Err(_) => opennotebook_ai::Provider::new(opennotebook_session::ai::base_url().await, None),
    };
    let body = tokio::time::timeout(Duration::from_secs(15), provider.models())
        .await
        .map_err(|_| anyhow::anyhow!("model price catalog: no answer within 15s"))?
        .map_err(|e| anyhow::anyhow!("model price catalog: {e}"))?;
    let prices = parse_catalog(&body);
    if prices.is_empty() {
        // A local server (Ollama, LM Studio) lists models without prices.
        anyhow::bail!("the model endpoint's catalog has no prices");
    }
    let stamp = now_rfc3339();
    *CACHE.lock().unwrap() = Some(Cached {
        at: Instant::now(),
        stamp: stamp.clone(),
        prices: prices.clone(),
    });
    Ok((prices, stamp))
}

/// OpenRouter's catalog shape: `data[].pricing.{prompt,completion}`, each a
/// decimal string of USD per token.
fn parse_catalog(body: &serde_json::Value) -> Prices {
    let num = |v: Option<&serde_json::Value>| {
        v.and_then(|x| {
            x.as_str()
                .and_then(|s| s.parse::<f64>().ok())
                .or_else(|| x.as_f64())
        })
    };
    body.get("data")
        .and_then(|d| d.as_array())
        .map(|models| {
            models
                .iter()
                .filter_map(|m| {
                    let id = m.get("id")?.as_str()?.to_string();
                    let p = m.get("pricing")?;
                    Some((
                        id,
                        Price {
                            prompt: num(p.get("prompt"))?,
                            completion: num(p.get("completion"))?,
                        },
                    ))
                })
                .collect()
        })
        .unwrap_or_default()
}

fn now_rfc3339() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or_default() as i64;
    // Civil date from days since the epoch (Howard Hinnant's algorithm), so a
    // timestamp does not need a date crate.
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rem / 3_600,
        rem % 3_600 / 60,
        rem % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_catalog_is_read_as_openrouter_writes_it() {
        let body = serde_json::json!({"data": [
            {"id": "openai/gpt-5.4", "pricing": {"prompt": "0.0000025", "completion": "0.000015"}},
            {"id": "google/gemini-3-pro-image-preview",
             "pricing": {"prompt": "0.000002", "completion": "0.000012", "image_output": "0.00012"}},
            {"id": "broken", "pricing": {}}
        ]});
        let p = super::parse_catalog(&body);
        assert_eq!(p.len(), 2);
        assert_eq!(p["openai/gpt-5.4"].completion, 0.000015);
        assert_eq!(p["google/gemini-3-pro-image-preview"].prompt, 0.000002);
    }

    #[test]
    fn a_price_reads_per_million_tokens() {
        let p = Price {
            prompt: 0.000001,
            completion: 0.000005,
        };
        assert_eq!(price_hint(&p), "$1 / $5 per M tokens");
        let p = Price {
            prompt: 0.000000075,
            completion: 0.0000003,
        };
        assert_eq!(price_hint(&p), "$0.075 / $0.3 per M tokens");
        assert_eq!(price_hint(&Price::default()), "free");
    }

    #[test]
    fn the_limit_message_points_at_the_settings_tab() {
        let m = over_limit_message(0.81, 0.5, false);
        assert!(m.contains("$0.81") && m.contains("$0.50"), "{m}");
        assert!(m.contains("Settings › Costs & limits"), "{m}");
        assert!(over_limit_message(1.0, 0.5, true).contains("shorter length"));
    }

    #[test]
    fn an_amount_over_the_limit_never_reads_as_the_limit() {
        let m = over_limit_message(0.2525, 0.25, false);
        assert!(m.contains("up to $0.26, over your $0.25"), "{m}");
        assert_eq!(cent_up(0.81), 0.81);
        assert_eq!(cent_up(0.8100000001), 0.81);
        assert_eq!(cent_up(0.811), 0.82);
    }

    #[test]
    fn a_timestamp_is_rfc3339() {
        let s = super::now_rfc3339();
        assert_eq!(s.len(), 20, "{s}");
        assert!(s.ends_with('Z') && s.as_bytes()[10] == b'T');
    }
}
