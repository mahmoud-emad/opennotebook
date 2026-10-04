//! The gate that ships today, replicated exactly, so it can be measured.
//!
//! This is not used in production by anything. It exists so `tests/replay.rs`
//! can put the current behaviour and the new detector through the same clips
//! and report the difference, rather than asserting an improvement.
//!
//! From `player.html`:
//!
//! ```text
//! SILENCE_PEAK = 0.06          // above this is "speech"
//! HANG_MS      = 2000
//! AnalyserNode fftSize = 512   // getByteTimeDomainData
//! setInterval(..., 60)         // polled every 60 ms
//! peak = max(|v - 128| / 128)
//! ```
//!
//! Two properties of that are reproduced here because they change the answer
//! and are easy to miss:
//!
//! * **It samples, it does not integrate.** 512 samples every 60 ms is 21.3 ms
//!   at 24 kHz, so 39 ms out of every 60 is never looked at. A short transient
//!   between polls is invisible; one inside a poll counts for the whole poll.
//! * **It is 8 bit.** `getByteTimeDomainData` quantises to 1/128, so the
//!   effective threshold is `ceil(0.06 * 128) / 128`, and peaks are rounded
//!   before they are compared.

/// `SILENCE_PEAK` in `player.html`.
pub const SILENCE_PEAK: f32 = 0.06;
/// `HANG_MS` in `player.html`.
pub const HANG_MS: u32 = 2000;
/// `AnalyserNode.fftSize`.
pub const FFT_SIZE: usize = 512;
/// The `setInterval` period driving the meter.
pub const POLL_MS: u32 = 60;

/// Outcome of replaying a clip through the shipped gate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct PeakOutcome {
    /// Whether the gate ever decided speech had started.
    pub heard_speech: bool,
    /// Millisecond offset at which the turn would have been sent, if it was.
    pub sent_at_ms: Option<u32>,
    /// Polls whose quantised peak cleared the threshold.
    pub loud_polls: u32,
    pub total_polls: u32,
}

/// Replay `samples` at `rate` through the shipped peak gate.
///
/// `armed_at_ms` mirrors the page: nothing counts until the narrator has
/// stopped, so the first poll considered is the one at or after that offset.
pub fn replay(samples: &[f32], rate: f32, armed_at_ms: u32) -> PeakOutcome {
    let mut out = PeakOutcome::default();
    if rate <= 0.0 || samples.is_empty() {
        return out;
    }

    let step = ((rate as f64) * (POLL_MS as f64) / 1000.0).max(1.0) as usize;
    let armed_at = ((armed_at_ms as f64 / 1000.0) * rate as f64) as usize;

    let mut quiet_since: Option<u32> = None;
    let mut i = 0usize;
    while i + FFT_SIZE <= samples.len() {
        let now_ms = ((i as u64 * 1000) / rate as u64) as u32;
        if i < armed_at {
            i += step;
            continue;
        }
        out.total_polls += 1;

        // The 8 bit round trip, before the comparison, exactly as the browser
        // does it.
        let mut peak = 0.0f32;
        for v in &samples[i..i + FFT_SIZE] {
            let q = (v * 128.0).round().abs() / 128.0;
            if q > peak {
                peak = q;
            }
        }

        if peak > SILENCE_PEAK {
            out.loud_polls += 1;
            out.heard_speech = true;
            quiet_since = None;
        } else if out.heard_speech {
            let began = *quiet_since.get_or_insert(now_ms);
            if now_ms.saturating_sub(began) >= HANG_MS && out.sent_at_ms.is_none() {
                out.sent_at_ms = Some(now_ms);
                return out;
            }
        }
        i += step;
    }
    out
}
