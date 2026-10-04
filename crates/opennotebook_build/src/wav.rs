//! Reading a duration out of a WAV header.
//!
//! `DurationMs` has one constructor, `measured_from_wav`, and no `From<u64>`,
//! because Kokoro is not reproducible per render: the same voice and text
//! produced a 6 ms length difference across two renders. A duration carried
//! over from a previous render is wrong by an unbounded amount. This module is
//! the only thing that may produce one.

use opennotebook_session::DurationMs;

use crate::error::BuildError;

/// Duration of a RIFF/WAVE buffer, from its `fmt ` and `data` chunks.
///
/// Chunks are walked rather than assumed at fixed offsets: a WAV may carry
/// `LIST`, `fact` or padding before `data`, and a reader that assumes byte 44
/// silently mis-measures those.
pub fn duration_of(bytes: &[u8], name: &str) -> Result<DurationMs, BuildError> {
    let bad = |why: &str| BuildError::NotWav {
        name: name.to_string(),
        why: why.to_string(),
    };
    if bytes.len() < 12 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err(bad("not a RIFF/WAVE buffer"));
    }

    let mut byte_rate: Option<u32> = None;
    let mut data_len: Option<u32> = None;
    let mut at = 12usize;
    while at + 8 <= bytes.len() {
        let id = &bytes[at..at + 4];
        let size = u32::from_le_bytes(bytes[at + 4..at + 8].try_into().unwrap()) as usize;
        let body = at + 8;
        match id {
            b"fmt " if body + 16 <= bytes.len() => {
                byte_rate = Some(u32::from_le_bytes(
                    bytes[body + 8..body + 12].try_into().unwrap(),
                ));
            }
            b"data" => {
                // The declared size can overrun a truncated file; trust the
                // bytes actually present, which is what a player will hear.
                data_len = Some(size.min(bytes.len() - body) as u32);
                break;
            }
            _ => {}
        }
        at = body + size + (size & 1); // chunks are word-aligned
    }

    match (byte_rate, data_len) {
        (Some(0), _) => Err(bad("byte rate is zero")),
        (Some(rate), Some(len)) => Ok(DurationMs::measured_from_wav(
            (u64::from(len) * 1000) / u64::from(rate),
        )),
        (None, _) => Err(bad("no fmt chunk")),
        (_, None) => Err(bad("no data chunk")),
    }
}

/// The format and PCM bytes of a WAV: (channels, sample rate, bits per
/// sample, data). Walks the chunks the same way [`duration_of`] does.
fn pcm_of(bytes: &[u8]) -> Option<(u16, u32, u16, &[u8])> {
    if bytes.len() < 12 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return None;
    }
    let mut fmt: Option<(u16, u16, u32, u16)> = None;
    let mut at = 12usize;
    while at + 8 <= bytes.len() {
        let id = &bytes[at..at + 4];
        let size = u32::from_le_bytes(bytes[at + 4..at + 8].try_into().ok()?) as usize;
        let body = at + 8;
        match id {
            b"fmt " if body + 16 <= bytes.len() => {
                let le16 = |o: usize| u16::from_le_bytes([bytes[body + o], bytes[body + o + 1]]);
                let rate = u32::from_le_bytes(bytes[body + 4..body + 8].try_into().ok()?);
                fmt = Some((le16(0), le16(2), rate, le16(14)));
            }
            b"data" => {
                let (format, channels, rate, bits) = fmt?;
                // PCM only: anything else cannot be joined by copying bytes.
                if format != 1 {
                    return None;
                }
                let end = (body + size).min(bytes.len());
                return Some((channels, rate, bits, &bytes[body..end]));
            }
            _ => {}
        }
        at = body + size + (size & 1);
    }
    None
}

/// Several WAVs as one, each with its own silence in front of it: an audio
/// overview's lines as the one file a listener downloads. The first clip's
/// gap is ignored.
///
/// Every clip must share the first one's format; Kokoro writes them all at
/// 24 kHz mono 16-bit. A clip that does not parse, or differs, is an error
/// naming it rather than a file that plays at the wrong speed.
pub fn join(clips: &[(String, Vec<u8>, u32)]) -> Result<Vec<u8>, BuildError> {
    let bad = |name: &str, why: &str| BuildError::NotWav {
        name: name.to_string(),
        why: why.to_string(),
    };
    let mut format: Option<(u16, u32, u16)> = None;
    let mut data: Vec<u8> = Vec::new();
    for (k, (name, bytes, gap_ms)) in clips.iter().enumerate() {
        let (ch, rate, bits, pcm) = pcm_of(bytes).ok_or_else(|| bad(name, "not a PCM WAV"))?;
        match format {
            None => format = Some((ch, rate, bits)),
            Some(f) if f != (ch, rate, bits) => {
                return Err(bad(name, "its format differs from the first clip's"));
            }
            _ => {}
        }
        if k > 0 {
            let frame = usize::from(ch) * usize::from(bits / 8);
            let frames = rate as usize * *gap_ms as usize / 1000;
            data.resize(data.len() + frames * frame, 0);
        }
        data.extend_from_slice(pcm);
    }
    let (ch, rate, bits) = format.ok_or_else(|| bad("episode", "no clips to join"))?;
    let block = u32::from(ch) * u32::from(bits / 8);
    let mut out = Vec::with_capacity(44 + data.len());
    out.extend(b"RIFF");
    out.extend((36 + data.len() as u32).to_le_bytes());
    out.extend(b"WAVEfmt ");
    out.extend(16u32.to_le_bytes());
    out.extend(1u16.to_le_bytes());
    out.extend(ch.to_le_bytes());
    out.extend(rate.to_le_bytes());
    out.extend((rate * block).to_le_bytes());
    out.extend((block as u16).to_le_bytes());
    out.extend(bits.to_le_bytes());
    out.extend(b"data");
    out.extend((data.len() as u32).to_le_bytes());
    out.extend(data);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clips_join_into_one_wav_with_silence_between() {
        // 24 kHz mono: 0.5 s, then 1 s, with a 250 ms gap.
        let a = wav(24_000, 12_000, true);
        let b = wav(24_000, 24_000, false);
        let joined = join(&[("a".into(), a, 900), ("b".into(), b, 250)]).unwrap();
        let d = duration_of(&joined, "joined").unwrap();
        assert_eq!(d.millis(), 1_750);
        assert!(join(&[]).is_err());
        let other = wav(16_000, 100, false);
        assert!(
            join(&[
                ("a".into(), wav(24_000, 10, false), 0),
                ("o".into(), other, 0)
            ])
            .is_err(),
            "a clip at another rate is refused"
        );
    }

    /// A minimal 16-bit mono WAV, optionally with a junk chunk before `data`.
    fn wav(sample_rate: u32, samples: u32, junk_before_data: bool) -> Vec<u8> {
        let byte_rate = sample_rate * 2;
        let data_bytes = samples * 2;
        let mut v = Vec::new();
        v.extend(b"RIFF");
        v.extend(0u32.to_le_bytes());
        v.extend(b"WAVE");
        v.extend(b"fmt ");
        v.extend(16u32.to_le_bytes());
        v.extend(1u16.to_le_bytes()); // pcm
        v.extend(1u16.to_le_bytes()); // mono
        v.extend(sample_rate.to_le_bytes());
        v.extend(byte_rate.to_le_bytes());
        v.extend(2u16.to_le_bytes());
        v.extend(16u16.to_le_bytes());
        if junk_before_data {
            v.extend(b"LIST");
            v.extend(5u32.to_le_bytes());
            v.extend([1, 2, 3, 4, 5]);
            v.push(0); // word alignment pad
        }
        v.extend(b"data");
        v.extend(data_bytes.to_le_bytes());
        v.extend(std::iter::repeat_n(0u8, data_bytes as usize));
        v
    }

    #[test]
    fn one_second_of_mono_24k_reads_as_1000ms() {
        let d = duration_of(&wav(24_000, 24_000, false), "x").unwrap();
        assert_eq!(d.millis(), 1000);
    }

    /// The reason chunks are walked instead of assumed at byte 44.
    #[test]
    fn a_chunk_before_data_does_not_shift_the_measurement() {
        let plain = duration_of(&wav(24_000, 12_000, false), "x").unwrap();
        let padded = duration_of(&wav(24_000, 12_000, true), "x").unwrap();
        assert_eq!(plain.millis(), 500);
        assert_eq!(
            padded.millis(),
            500,
            "a LIST chunk must not change the duration"
        );
    }

    #[test]
    fn a_truncated_file_measures_what_is_actually_there() {
        let mut v = wav(24_000, 24_000, false);
        v.truncate(v.len() - 24_000); // lose half the samples
        assert_eq!(duration_of(&v, "x").unwrap().millis(), 500);
    }

    #[test]
    fn non_wav_bytes_are_refused_rather_than_measured() {
        assert!(matches!(
            duration_of(b"ID3\x04not audio at all", "clip.mp3"),
            Err(BuildError::NotWav { .. })
        ));
    }
}
