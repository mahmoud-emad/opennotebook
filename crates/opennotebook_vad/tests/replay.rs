//! The replay harness: recorded clips in, two error rates out.
//!
//! Run it with output:
//!
//! ```sh
//! cargo test -p opennotebook_vad --test replay -- --nocapture
//! ```
//!
//! # Why a harness and not an accuracy number
//!
//! There is no correct threshold, only a trade. Every millisecond added to
//! `hang_ms` moves failures from CUT OFF EARLY to NEVER SENDS and every
//! millisecond removed moves them back. One "accuracy" figure averages the two
//! and hides the only thing worth knowing, so this reports them separately and
//! never combines them.
//!
//! Three rates, because the corpus has two kinds of clip:
//!
//! | Rate | On which clips | What it means |
//! |---|---|---|
//! | CUT OFF EARLY | `question` | Sent before the speaker finished. They lose words. |
//! | NEVER SENDS | `question` | Still listening at the end of the recording. They wait. |
//! | FALSE SEND | `noise` | Ended a turn on a clip with no question in it. |
//!
//! FALSE SEND is the door slam. The peak gate in `player.html` cannot score
//! anything but badly here, which is the reason this crate exists, so the
//! harness runs both detectors over every clip and prints them side by side.
//!
//! # The corpus
//!
//! `corpus/index.tsv`, tab separated, `#` comments ignored:
//!
//! ```text
//! file            kind      speech_end_ms   note
//! quiet-01.wav    question  4200            plain question, no background
//! think-03.wav    question  9100            pauses twice mid sentence
//! street-02.wav   noise     -               traffic, no speaker
//! ```
//!
//! `speech_end_ms` is when the speaker actually stopped, by ear, and it is the
//! only label that needs care: CUT OFF EARLY is measured against it. `-` for
//! `noise` rows. See `corpus/README.md` for how to record them.

use std::{
    fs,
    path::{Path, PathBuf},
};

use opennotebook_vad::{Config, Event, Gate, Turn, analyze, peak};

/// A clip ending within this much of the labelled end is not "cut off": the
/// label is a human with a waveform and is not accurate to the millisecond.
const LABEL_TOLERANCE_MS: u32 = 250;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Question,
    Noise,
}

struct Clip {
    path: PathBuf,
    kind: Kind,
    speech_end_ms: Option<u32>,
    note: String,
}

fn corpus_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("corpus")
}

fn load_index() -> Vec<Clip> {
    let dir = corpus_dir();
    let Ok(text) = fs::read_to_string(dir.join("index.tsv")) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let f: Vec<&str> = line
            .split('\t')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();
        if f.len() < 2 || f[0] == "file" {
            continue;
        }
        let kind = match f[1] {
            "question" => Kind::Question,
            "noise" => Kind::Noise,
            other => panic!("corpus/index.tsv: unknown kind {other:?} on {:?}", f[0]),
        };
        out.push(Clip {
            path: dir.join(f[0]),
            kind,
            speech_end_ms: f.get(2).and_then(|s| s.parse().ok()),
            note: f.get(3).map(|s| (*s).to_string()).unwrap_or_default(),
        });
    }
    out
}

/// Mono `f32` in `[-1, 1]`, plus the rate, from any WAV the browser might send.
fn read_wav(path: &Path) -> Result<(Vec<f32>, f32), String> {
    let r = hound::WavReader::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let spec = r.spec();
    let ch = spec.channels.max(1) as usize;
    let mut r = r;
    let samples: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => r.samples::<f32>().filter_map(Result::ok).collect(),
        hound::SampleFormat::Int => {
            let scale = 1.0 / (1i64 << (spec.bits_per_sample - 1)) as f32;
            r.samples::<i32>()
                .filter_map(Result::ok)
                .map(|s| s as f32 * scale)
                .collect()
        }
    };
    // Downmix by taking the first channel: the page captures mono, and
    // averaging would change the level a threshold is tuned against.
    let mono = if ch == 1 {
        samples
    } else {
        samples.iter().step_by(ch).copied().collect()
    };
    Ok((mono, spec.sample_rate as f32))
}

#[derive(Default)]
struct Tally {
    questions: u32,
    cut_off_early: u32,
    never_sends: u32,
    noise_clips: u32,
    false_sends: u32,
}

impl Tally {
    fn pct(n: u32, d: u32) -> String {
        if d == 0 {
            "  n/a".into()
        } else {
            format!("{:5.1}%", 100.0 * n as f32 / d as f32)
        }
    }
    fn line(&self, label: &str) {
        println!(
            "  {label:12}  CUT OFF EARLY {}  ({}/{})   NEVER SENDS {}  ({}/{})   FALSE SEND {}  ({}/{})",
            Self::pct(self.cut_off_early, self.questions),
            self.cut_off_early,
            self.questions,
            Self::pct(self.never_sends, self.questions),
            self.never_sends,
            self.questions,
            Self::pct(self.false_sends, self.noise_clips),
            self.false_sends,
            self.noise_clips,
        );
    }
}

/// Drive the live gate over a whole clip and report where it would have sent.
fn live_end_ms(samples: &[f32], rate: f32, cfg: Config) -> Option<u32> {
    let mut t = Turn::new(rate, cfg);
    let mut elapsed_ms = 0u32;
    // 128 frames is the browser's render quantum, so the harness cuts the audio
    // up the same way an AudioWorklet will.
    for chunk in samples.chunks(128) {
        if t.push(chunk) == Event::Ended {
            return Some(elapsed_ms);
        }
        elapsed_ms += ((chunk.len() as u64 * 1000) / rate as u64) as u32;
    }
    None
}

fn score(clip: &Clip, end_ms: Option<u32>, t: &mut Tally) {
    match clip.kind {
        Kind::Question => {
            t.questions += 1;
            match end_ms {
                None => t.never_sends += 1,
                Some(end) => {
                    let spoke_until = clip.speech_end_ms.unwrap_or(0);
                    if end + LABEL_TOLERANCE_MS < spoke_until {
                        t.cut_off_early += 1;
                    }
                }
            }
        }
        Kind::Noise => {
            t.noise_clips += 1;
            if end_ms.is_some() {
                t.false_sends += 1;
            }
        }
    }
}

#[test]
fn replay_corpus() {
    let clips = load_index();
    if clips.is_empty() {
        println!("\n  corpus/index.tsv is empty or missing, so there is nothing to measure.");
        println!(
            "  This is the one step that needs a human: see corpus/README.md for what to record."
        );
        println!("  The harness is exercised without it by `synthetic_smoke_test` below.\n");
        return;
    }

    println!(
        "\n  {} clips from {}\n",
        clips.len(),
        corpus_dir().display()
    );
    let mut vad = Tally::default();
    let mut shipped = Tally::default();
    let mut missing = Vec::new();

    for clip in &clips {
        let (samples, rate) = match read_wav(&clip.path) {
            Ok(v) => v,
            Err(e) => {
                missing.push(e);
                continue;
            }
        };

        let ours = live_end_ms(&samples, rate, Config::DEFAULT);
        score(clip, ours, &mut vad);

        // The shipped gate over the same audio, armed from the start.
        let p = peak::replay(&samples, rate, 0);
        score(clip, p.sent_at_ms, &mut shipped);

        let a = analyze(&samples, rate, &Config::DEFAULT);
        println!(
            "  {:24} {:8} spoke_to={:>6}  ours={:>7}  peak={:>7}  speech_ms={:>6}  segs={}  {}",
            clip.path.file_name().unwrap_or_default().to_string_lossy(),
            match clip.kind {
                Kind::Question => "question",
                Kind::Noise => "noise",
            },
            clip.speech_end_ms
                .map(|v| v.to_string())
                .unwrap_or_else(|| "-".into()),
            ours.map(|v| v.to_string())
                .unwrap_or_else(|| "never".into()),
            p.sent_at_ms
                .map(|v| v.to_string())
                .unwrap_or_else(|| "never".into()),
            a.speech_ms,
            a.segment_count,
            clip.note,
        );
    }

    println!("\n  Config: {:?}\n", Config::DEFAULT);
    vad.line("opennotebook_vad");
    shipped.line("peak 0.06");
    println!();

    assert!(
        missing.is_empty(),
        "clips listed in index.tsv but unreadable:\n{}",
        missing.join("\n")
    );

    // The gate that matters, and the only one asserted: a clip with no question
    // in it must never spend an answer-model call. A regression here is a bill.
    assert_eq!(
        vad.false_sends, 0,
        "{} of {} noise clips ended a turn",
        vad.false_sends, vad.noise_clips
    );
}

/// Proves the harness itself runs, with no recordings needed, so an empty
/// corpus never looks like a passing measurement.
#[test]
fn synthetic_smoke_test() {
    // Two seconds of quiet must not end a turn, at any rate the browser uses.
    for rate in [16_000.0f32, 44_100.0, 48_000.0] {
        let quiet = vec![0.0f32; (rate as usize) * 3];
        assert!(
            live_end_ms(&quiet, rate, Config::DEFAULT).is_none(),
            "silence ended a turn at {rate} Hz"
        );
        let p = peak::replay(&quiet, rate, 0);
        assert!(
            !p.heard_speech,
            "the peak gate heard speech in silence at {rate} Hz"
        );
    }
}

/// A recorded probability track is enough to retune `hang_ms` without decoding
/// audio again, which is what makes a sweep cheap. Guards the property the
/// sweep depends on: longer hang never sends earlier.
#[test]
fn hang_is_monotonic() {
    // Speech for 30 frames, then quiet.
    let track: Vec<f32> = (0..400).map(|i| if i < 30 { 0.9 } else { 0.05 }).collect();
    let mut last = 0;
    for hang in [500u32, 1000, 2000, 3000] {
        let cfg = Config {
            hang_ms: hang,
            ..Config::DEFAULT
        };
        let mut g = Gate::new(cfg);
        let mut end = None;
        for &p in &track {
            if g.step(p) == Event::Ended {
                end = g.ended_at_ms();
                break;
            }
        }
        let end = end.unwrap_or_else(|| panic!("hang_ms={hang} never ended"));
        assert!(
            end >= last,
            "hang_ms={hang} ended at {end}, earlier than {last}"
        );
        last = end;
    }
}
