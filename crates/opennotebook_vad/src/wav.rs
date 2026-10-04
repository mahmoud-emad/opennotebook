//! Decoding the WAV the page uploads, for the native gate.
//!
//! Behind the `wav` feature, which the worklet build turns off. A wasm module
//! running on the audio thread is handed `f32` frames by the browser and has no
//! use for a container parser.
//!
//! `player.html` builds the body itself with `toWav(samples, micRate)`, so in
//! practice this sees 16 bit PCM mono at the capture device's rate. It does not
//! assume that: a WAV that arrives from anywhere else still has to decode or
//! fail by name, because the alternative is a gate that silently reads garbage
//! and calls it silence.

use crate::{Analysis, Config, analyze};

/// Mono `f32` in `[-1, 1]` plus the sample rate.
pub struct Pcm {
    pub samples: Vec<f32>,
    pub rate: f32,
}

/// Decode a WAV body.
///
/// Errors are strings because every caller here turns them into an SSE error
/// frame, and a typed error would be converted at exactly one place.
pub fn decode(bytes: &[u8]) -> Result<Pcm, String> {
    let mut r = hound::WavReader::new(std::io::Cursor::new(bytes))
        .map_err(|e| format!("WAV parse failed: {e}"))?;
    let spec = r.spec();
    if spec.channels == 0 {
        return Err("WAV parse failed: zero channels".into());
    }

    let samples: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => r.samples::<f32>().filter_map(Result::ok).collect(),
        hound::SampleFormat::Int => {
            // bits_per_sample is what the header claims; hound reads into i32
            // regardless, so scale by the claimed depth rather than by 32 bits.
            let scale = 1.0 / (1i64 << (spec.bits_per_sample.max(1) - 1)) as f32;
            r.samples::<i32>()
                .filter_map(Result::ok)
                .map(|s| s as f32 * scale)
                .collect()
        }
    };
    if samples.is_empty() {
        return Err("WAV parse failed: no samples".into());
    }

    // First channel, not an average: the page captures mono, and averaging a
    // stereo clip halves the level a threshold was tuned against.
    let ch = spec.channels as usize;
    let samples = if ch == 1 {
        samples
    } else {
        samples.iter().step_by(ch).copied().collect()
    };

    Ok(Pcm {
        samples,
        rate: spec.sample_rate as f32,
    })
}

/// Decode and gate in one call, which is all the server wants.
pub fn analyze_wav(bytes: &[u8], cfg: &Config) -> Result<Analysis, String> {
    let pcm = decode(bytes)?;
    Ok(analyze(&pcm.samples, pcm.rate, cfg))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wav16(samples: &[f32], rate: u32) -> Vec<u8> {
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut buf = std::io::Cursor::new(Vec::new());
        {
            let mut w = hound::WavWriter::new(&mut buf, spec).unwrap();
            for s in samples {
                w.write_sample((s * 32767.0) as i16).unwrap();
            }
            w.finalize().unwrap();
        }
        buf.into_inner()
    }

    #[test]
    fn silence_decodes_and_gates_as_silent() {
        let bytes = wav16(&vec![0.0; 24_000 * 2], 24_000);
        let a = analyze_wav(&bytes, &Config::DEFAULT).expect("decode");
        assert!(a.is_silent(), "silence was not gated as silent: {a:?}");
        assert!(a.input_ms >= 1_900, "input_ms looks wrong: {}", a.input_ms);
    }

    /// Malformed audio is the one case phase 2 §8 says is loud. Keep it loud.
    #[test]
    fn garbage_fails_by_name() {
        let e = analyze_wav(b"not a wav at all", &Config::DEFAULT).unwrap_err();
        assert!(e.contains("WAV parse failed"), "unhelpful error: {e}");
    }

    #[test]
    fn empty_body_fails_by_name() {
        assert!(analyze_wav(&[], &Config::DEFAULT).is_err());
    }
}
