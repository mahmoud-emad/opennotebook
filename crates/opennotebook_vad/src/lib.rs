//! Voice activity detection for opennotebook.
//!
//! # Two questions, not one
//!
//! The phase 2 spec records that the page's peak gate and the VAD gate answer
//! different questions and that neither covers the other. This crate answers
//! both, from one state machine, so the answers cannot drift apart:
//!
//! * **When does the turn end?** [`Turn`], live, in an `AudioWorklet`, because
//!   that is where the microphone is. Streaming, one frame at a time, no
//!   allocation after construction.
//! * **Did this clip hold speech worth spending an answer on?** [`analyze`],
//!   offline, on the server, over the uploaded WAV. This is the one the spec
//!   calls "said something I could not resolve", and nothing answered it
//!   before.
//!
//! [`Turn::push`] and [`analyze`] both drive the same [`Gate`] over the same
//! resampler with the same [`Config`]. Feed one recording to both and they
//! agree by construction, which is what makes `tests/replay.rs` a statement
//! about the browser and not only about the server.
//!
//! # Which detector, and what was rejected
//!
//! [`earshot`](https://github.com/pykeio/earshot) 1.2, MIT/Apache-2.0. A small
//! neural detector: 40 mel bands over a 1024 point FFT with three frames of
//! context, and 39,940 bytes of weights compiled into the crate. Zero runtime
//! dependencies, `no_std` capable, nothing to fetch at start up, which is what
//! makes it viable inside a worklet.
//!
//! Rejected, with the reason:
//!
//! * **Silero v4 through `sherpa-onnx`**, which local speech servers commonly
//!   run. sherpa-onnx is C++ and does not cross to wasm32, so it cannot be the
//!   live detector. Keeping it server-side and using something else in the page
//!   means two detectors and two thresholds, which is the drift this crate
//!   exists to prevent.
//! * **Silero v4 through `tract`**, the pure Rust ONNX engine, which does reach
//!   wasm32. Rejected on payload: tract plus a 643 KB model is megabytes of
//!   wasm to load into an audio thread, against about a hundred kilobytes here.
//!   "No model download at runtime" rules out fetching it instead.
//! * **`voice_activity_detector` and `silero-vad-rust`**, which read as pure
//!   Rust ports and are not: both depend on `ort`, so both carry an ONNX
//!   runtime.
//! * **`webrtc-vad`**, a C binding, last released 2019.
//!
//! Two corrections to what the brief expected, both found by reading the source
//! rather than the README. earshot is not a WebRTC GMM port; that was 0.x, and
//! since 1.0 it is the neural model above. And the published 1.2.2 is *behind*
//! the README that documents it: `main` has a `segments()` segmenter and
//! exported frame constants, and the crates.io release has neither. Everything
//! here is written against the published API.
//!
//! No claim is made that this detector beats Silero. The author's benchmarks
//! say so; `tests/replay.rs` is what decides it here, on recordings, reporting
//! both error rates.

use earshot::Detector;

pub mod peak;
mod resample;

#[cfg(feature = "wav")]
pub mod wav;

#[cfg(target_arch = "wasm32")]
mod wasm;

pub use resample::Resampler;

/// Samples in one detector frame. earshot requires exactly this and the
/// published crate does not export it.
pub const FRAME_SIZE: usize = 256;
/// The rate earshot requires.
pub const SAMPLE_RATE: u32 = 16_000;
/// Milliseconds of audio in one frame. 16.
pub const FRAME_MS: u32 = (FRAME_SIZE as u32 * 1000) / SAMPLE_RATE;

/// Every threshold in one place, shared by the live and offline paths.
///
/// The defaults are a starting point, not a tuned answer. `tests/replay.rs`
/// moves them, and the two rates it reports are what justifies a move.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Config {
    /// Exponential smoothing on the raw per-frame probability, in `[0, 1)`.
    /// 0 disables it. Raw earshot output is jumpy frame to frame and an
    /// unsmoothed threshold chatters.
    pub smoothing: f32,
    /// Smoothed probability at or above which a frame starts speech.
    pub attack: f32,
    /// Smoothed probability below which a frame stops counting toward
    /// `speech_ms`. Deliberately under `attack`: one threshold with no gap
    /// flips on every frame sitting near it.
    ///
    /// It does NOT gate the hang countdown. Only a frame at or above `attack`
    /// restarts that, so audio parked between the two thresholds can keep a
    /// turn alive for `speech_ms` purposes without keeping it open forever.
    pub decay: f32,
    /// Consecutive attacking frames before the turn counts as speaking. This is
    /// what rejects a door slam, which the peak gate cannot do at all.
    pub onset_frames: u16,
    /// Trailing quiet that ends the turn. The listener's thinking pause lives
    /// or dies here: this is the CUT OFF EARLY against NEVER SENDS dial.
    pub hang_ms: u32,
    /// A clip holding less speech than this did not contain a question.
    pub min_speech_ms: u32,
}

impl Config {
    /// The shipped defaults.
    ///
    /// `hang_ms` is 2000 to match what `player.html` already does, so the first
    /// deployment changes the detector and not the feel of the turn. Changing
    /// both at once makes a regression impossible to attribute.
    pub const DEFAULT: Self = Self {
        smoothing: 0.4,
        attack: 0.6,
        decay: 0.5,
        onset_frames: 2,
        hang_ms: 2000,
        min_speech_ms: 300,
    };
}

impl Default for Config {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// What the live detector says after a push.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    /// Nothing worth calling speech has happened yet.
    Idle,
    /// The listener is talking, or is inside a pause short enough to keep.
    Speaking,
    /// Trailing quiet passed `hang_ms` after real speech. Send the turn.
    Ended,
}

/// The frame-level decision, with no audio handling at all.
///
/// Both paths run this and nothing else decides. Kept public so the replay
/// harness can drive it from a recorded probability track and compare
/// thresholds without re-running the detector.
#[derive(Debug, Clone)]
pub struct Gate {
    cfg: Config,
    smoothed: f32,
    run: u16,
    speech_frames: u32,
    quiet_ms: u32,
    started: bool,
    ended_at_frame: Option<u32>,
    frame: u32,
}

impl Gate {
    pub fn new(cfg: Config) -> Self {
        Self {
            cfg,
            smoothed: 0.0,
            run: 0,
            speech_frames: 0,
            quiet_ms: 0,
            started: false,
            ended_at_frame: None,
            frame: 0,
        }
    }

    pub fn reset(&mut self) {
        let cfg = self.cfg;
        *self = Self::new(cfg);
    }

    /// Feed one frame's raw probability. Returns the state after it.
    pub fn step(&mut self, raw: f32) -> Event {
        if self.ended_at_frame.is_some() {
            return Event::Ended;
        }
        self.frame += 1;

        self.smoothed = if self.cfg.smoothing > 0.0 {
            self.smoothed * self.cfg.smoothing + raw * (1.0 - self.cfg.smoothing)
        } else {
            raw
        };

        if self.smoothed >= self.cfg.attack {
            self.run = self.run.saturating_add(1);
            if self.run >= self.cfg.onset_frames {
                self.started = true;
                self.quiet_ms = 0;
                self.speech_frames += 1;
            }
        } else {
            self.run = 0;
            if self.started {
                // EVERYTHING below `attack` counts toward the hang, including
                // the band between `decay` and `attack`.
                //
                // The band used to reset `quiet_ms`, which reads as harmless
                // hysteresis and is not: a room that parks the smoothed
                // probability at 0.55 restarts the countdown on every frame and
                // the turn listens forever. That is the studio1 report, and
                // `a_noisy_room_still_ends_the_turn` is it at frame level.
                //
                // The band still counts as speech for `speech_ms`, because it
                // probably is speech. It just may not hold the floor.
                if self.smoothed >= self.cfg.decay {
                    self.speech_frames += 1;
                }
                self.quiet_ms += FRAME_MS;
                // The min_speech_ms floor stops a cough plus two seconds of
                // quiet from sending an empty clip, which is what the peak gate
                // does today.
                if self.quiet_ms >= self.cfg.hang_ms && self.speech_ms() >= self.cfg.min_speech_ms {
                    self.ended_at_frame = Some(self.frame);
                }
            }
        }

        if self.ended_at_frame.is_some() {
            Event::Ended
        } else if self.started {
            Event::Speaking
        } else {
            Event::Idle
        }
    }

    pub fn speech_ms(&self) -> u32 {
        self.speech_frames * FRAME_MS
    }

    pub fn heard_speech(&self) -> bool {
        self.started
    }

    /// Millisecond offset at which the turn ended, if it did.
    pub fn ended_at_ms(&self) -> Option<u32> {
        self.ended_at_frame.map(|f| f * FRAME_MS)
    }
}

/// The live question: has the listener stopped talking?
///
/// Feed it whatever the capture device produces, at whatever rate. It resamples
/// to 16 kHz, buffers to exact detector frames, and returns an [`Event`] per
/// push. After [`Event::Ended`] it keeps returning `Ended` until [`Turn::reset`].
pub struct Turn {
    det: Detector,
    gate: Gate,
    rs: Resampler,
    /// Partial frame carried between pushes. Capture buffers are not frame
    /// aligned and pretending otherwise drops samples at every boundary.
    pending: Vec<f32>,
}

impl Turn {
    pub fn new(input_rate: f32, cfg: Config) -> Self {
        Self {
            det: Detector::default(),
            gate: Gate::new(cfg),
            rs: Resampler::new(input_rate, SAMPLE_RATE as f32),
            pending: Vec::with_capacity(FRAME_SIZE * 4),
        }
    }

    /// Back to the state of a fresh turn, keeping the resampler's rate.
    pub fn reset(&mut self) {
        self.det.reset();
        self.gate.reset();
        self.rs.reset();
        self.pending.clear();
    }

    /// Milliseconds of speech so far. The same quantity [`Analysis::speech_ms`]
    /// reports, so page and server can be compared on one recording.
    pub fn speech_ms(&self) -> u32 {
        self.gate.speech_ms()
    }

    pub fn heard_speech(&self) -> bool {
        self.gate.heard_speech()
    }

    pub fn push(&mut self, samples: &[f32]) -> Event {
        self.rs.push(samples, &mut self.pending);

        let mut at = 0;
        let mut ev = if self.gate.heard_speech() {
            Event::Speaking
        } else {
            Event::Idle
        };
        while self.pending.len() - at >= FRAME_SIZE {
            let raw = self.det.predict_f32(&self.pending[at..at + FRAME_SIZE]);
            at += FRAME_SIZE;
            ev = self.gate.step(raw);
            if ev == Event::Ended {
                break;
            }
        }
        self.pending.drain(..at);
        ev
    }
}

/// What the offline question returns.
///
/// Field for field the shape a `/v1/audio/vad` endpoint commonly returns, so
/// code written against one reads the same names here.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Analysis {
    pub input_ms: u32,
    pub speech_ms: u32,
    pub segment_count: u32,
    /// Where [`Turn`] would have ended the turn on this clip, if it would have.
    /// `None` means the live gate would still be listening at the end of the
    /// recording, which is the NEVER SENDS failure.
    pub would_end_at_ms: Option<u32>,
}

impl Analysis {
    /// The gate the phase 2 spec decided and nothing implemented: there is
    /// nothing here worth an answer-model call.
    pub fn is_silent(&self) -> bool {
        self.speech_ms == 0
    }
}

/// The offline question: did this clip hold speech worth spending an answer on?
///
/// `samples` is mono in `[-1, 1]` at `input_rate`. Runs the same [`Gate`] the
/// live path runs, to the end of the clip rather than stopping at the first
/// end of turn, so one call reports both what the server needs and what the
/// browser would have done.
pub fn analyze(samples: &[f32], input_rate: f32, cfg: &Config) -> Analysis {
    let mut rs = Resampler::new(input_rate, SAMPLE_RATE as f32);
    let mut at16 = Vec::with_capacity(samples.len());
    rs.push(samples, &mut at16);

    let input_ms = ((at16.len() as u64 * 1000) / SAMPLE_RATE as u64) as u32;
    let mut det = Detector::default();
    let mut gate = Gate::new(*cfg);

    let mut segment_count = 0u32;
    let mut in_segment = false;
    let mut would_end_at_ms = None;

    for frame in at16.chunks_exact(FRAME_SIZE) {
        let raw = det.predict_f32(frame);
        // Drive the gate past the end of turn so the whole clip is measured,
        // recording where the live path would have stopped.
        let before = gate.speech_ms();
        let ev = gate.step(raw);
        if would_end_at_ms.is_none() && ev == Event::Ended {
            would_end_at_ms = gate.ended_at_ms();
            gate.ended_at_frame = None; // keep counting
        }
        let grew = gate.speech_ms() > before;
        if grew && !in_segment {
            segment_count += 1;
        }
        in_segment = grew;
    }

    Analysis {
        input_ms,
        speech_ms: gate.speech_ms().min(input_ms),
        segment_count,
        would_end_at_ms,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Silence is silence. The property the server gate rests on.
    #[test]
    fn silence_holds_no_speech() {
        let quiet = vec![0.0f32; 16_000 * 3];
        let a = analyze(&quiet, 16_000.0, &Config::DEFAULT);
        assert_eq!(a.speech_ms, 0, "silence reported speech");
        assert!(a.is_silent());
        assert_eq!(a.segment_count, 0);
        assert!(a.would_end_at_ms.is_none(), "a turn ended on silence");
    }

    /// A single transient is the failure the peak gate has. `onset_frames`
    /// is what rejects it, so assert it stays rejected.
    #[test]
    fn a_click_is_not_speech() {
        let mut clip = vec![0.0f32; 16_000 * 2];
        // One 8 ms full-scale burst: far above the peak gate's 0.06.
        for s in clip.iter_mut().skip(8_000).take(128) {
            *s = 0.9;
        }
        let a = analyze(&clip, 16_000.0, &Config::DEFAULT);
        assert!(a.would_end_at_ms.is_none(), "a click ended a turn");
        assert!(
            a.speech_ms < Config::DEFAULT.min_speech_ms,
            "a click counted as {} ms of speech",
            a.speech_ms
        );

        // The same click through the shipped gate, for contrast.
        let p = peak::replay(&clip, 16_000.0, 0);
        assert!(p.heard_speech, "the peak gate is supposed to fail this");
    }

    /// A room that is never quite silent must still end the turn.
    ///
    /// Reported from studio1: the listener asked a question, stopped talking,
    /// and the turn never sent, because background noise kept it listening.
    /// This is that failure at the frame level. After real speech the track
    /// falls to obvious silence but dithers one frame in ten into the
    /// hysteresis band between `decay` and `attack`, which is what an ordinary
    /// room does to a smoothed probability.
    ///
    /// The bug it guards: the band used to set `quiet_ms = 0`, so a single
    /// frame above `decay` restarted the whole two second countdown and the
    /// turn could never end. Only a frame at or above `attack` may reset it.
    #[test]
    fn a_noisy_room_still_ends_the_turn() {
        let mut g = Gate::new(Config::DEFAULT);
        for _ in 0..32 {
            g.step(0.95);
        }
        assert!(g.heard_speech(), "the speech at the start was not detected");

        let mut ended_at = None;
        for i in 0..500 {
            // Sustained, not a spike. Smoothing damps a lone frame, so a
            // one-in-ten dither never reaches the band and proves nothing; a
            // room that holds the probability there is what does.
            let p = if i % 2 == 0 { 0.50 } else { 0.58 };
            if g.step(p) == Event::Ended {
                ended_at = g.ended_at_ms();
                break;
            }
        }
        let ended_at = ended_at.expect(
            "the turn never ended: background noise held it open, which is the studio1 report",
        );
        assert!(
            ended_at >= Config::DEFAULT.hang_ms,
            "ended at {ended_at} ms, before hang_ms"
        );
    }

    /// The band must not cut a speaker off either: a probability that keeps
    /// reaching `attack` is someone still talking, and the countdown restarts.
    #[test]
    fn a_speaker_who_keeps_talking_is_not_cut_off() {
        let mut g = Gate::new(Config::DEFAULT);
        for i in 0..1250 {
            let p = if i % 5 == 0 { 0.30 } else { 0.92 };
            assert_ne!(
                g.step(p),
                Event::Ended,
                "cut the speaker off at frame {i} while they were still talking"
            );
        }
    }

    /// Streaming and offline must agree, or the harness measures the wrong
    /// thing. Same clip, one call against many pushes.
    #[test]
    fn live_and_offline_agree() {
        let mut clip = vec![0.0f32; 16_000 * 2];
        for (i, s) in clip.iter_mut().enumerate() {
            let t = i as f32 / 16_000.0;
            if (0.2..0.9).contains(&t) {
                *s = 0.3 * (t * 900.0).sin() * (t * 13.0).sin();
            }
        }
        let offline = analyze(&clip, 16_000.0, &Config::DEFAULT);

        let mut turn = Turn::new(16_000.0, Config::DEFAULT);
        for chunk in clip.chunks(128) {
            turn.push(chunk);
        }
        assert_eq!(
            turn.speech_ms(),
            offline.speech_ms,
            "streaming and offline disagreed on speech_ms"
        );
    }
}
