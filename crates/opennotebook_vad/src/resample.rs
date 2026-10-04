//! Rate conversion to the detector's 16 kHz, streaming and deterministic.
//!
//! Linear interpolation, on purpose. A capture device runs at 44.1 or 48 kHz
//! and the detector wants 16, so something has to convert, and the choice is
//! between a windowed-sinc resampler and this. Linear aliases above about
//! 8 kHz, which matters for audio a person will hear and does not matter here:
//! earshot's front end is a 40 band mel filterbank over a 1024 point FFT, and
//! the aliased energy lands in bands the detector already weights near zero.
//!
//! What it buys is determinism and size. No filter state beyond one sample, no
//! tables, identical output on wasm32 and x86_64 for identical input, which is
//! the property the replay harness depends on: a clip that ends a turn in the
//! browser must end it the same way offline.

/// Fixed-step linear resampler that survives across pushes.
pub struct Resampler {
    ratio: f64,
    /// Fractional read position inside the input stream, carried between
    /// pushes. Resetting this per buffer is the bug that makes a streaming
    /// resampler drift and click at every boundary.
    pos: f64,
    last: f32,
    primed: bool,
}

impl Resampler {
    pub fn new(input_rate: f32, output_rate: f32) -> Self {
        let ratio = if input_rate > 0.0 {
            input_rate as f64 / output_rate as f64
        } else {
            1.0
        };
        Self {
            ratio,
            pos: 0.0,
            last: 0.0,
            primed: false,
        }
    }

    pub fn reset(&mut self) {
        self.pos = 0.0;
        self.last = 0.0;
        self.primed = false;
    }

    /// True when input and output rates match, so `push` is a copy.
    pub fn is_passthrough(&self) -> bool {
        (self.ratio - 1.0).abs() < 1e-9
    }

    /// Append the converted form of `input` to `out`.
    pub fn push(&mut self, input: &[f32], out: &mut Vec<f32>) {
        if input.is_empty() {
            return;
        }
        if self.is_passthrough() {
            out.extend_from_slice(input);
            return;
        }
        if !self.primed {
            self.last = input[0];
            self.primed = true;
        }

        // `pos` is relative to the start of this buffer, and index -1 is the
        // last sample of the previous one, which is what `last` holds.
        while self.pos < input.len() as f64 {
            let i = self.pos.floor();
            let frac = (self.pos - i) as f32;
            let i = i as isize;

            let a = if i < 0 { self.last } else { input[i as usize] };
            let b = match usize::try_from(i + 1) {
                Ok(n) if n < input.len() => input[n],
                // The next sample has not arrived. Stop here and pick it up on
                // the following push rather than inventing it.
                _ => break,
            };

            out.push(a + (b - a) * frac);
            self.pos += self.ratio;
        }

        self.last = input[input.len() - 1];
        self.pos -= input.len() as f64;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passthrough_is_exact() {
        let mut r = Resampler::new(16_000.0, 16_000.0);
        let mut out = Vec::new();
        r.push(&[0.1, 0.2, 0.3], &mut out);
        assert_eq!(out, vec![0.1, 0.2, 0.3]);
    }

    #[test]
    fn three_to_one_keeps_length_and_rate() {
        // 48 kHz in, 16 kHz out: one output sample per three input samples.
        let mut r = Resampler::new(48_000.0, 16_000.0);
        let input: Vec<f32> = (0..4800).map(|i| (i as f32 / 100.0).sin()).collect();
        let mut out = Vec::new();
        r.push(&input, &mut out);
        assert!(
            (out.len() as i32 - 1600).abs() <= 1,
            "expected about 1600 samples, got {}",
            out.len()
        );
    }

    #[test]
    fn streaming_matches_one_shot() {
        // The property the replay harness rests on: how the buffers were cut up
        // must not change the output.
        let input: Vec<f32> = (0..3000).map(|i| (i as f32 / 7.0).sin()).collect();

        let mut one = Resampler::new(44_100.0, 16_000.0);
        let mut a = Vec::new();
        one.push(&input, &mut a);

        let mut many = Resampler::new(44_100.0, 16_000.0);
        let mut b = Vec::new();
        for chunk in input.chunks(128) {
            many.push(chunk, &mut b);
        }

        assert_eq!(a.len(), b.len(), "chunking changed the sample count");
        for (i, (x, y)) in a.iter().zip(b.iter()).enumerate() {
            assert!((x - y).abs() < 1e-6, "sample {i} differs: {x} vs {y}");
        }
    }
}
