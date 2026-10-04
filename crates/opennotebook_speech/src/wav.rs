//! Bringing a server's WAV to the one format OpenNotebook plays: mono, 16-bit
//! PCM, at a fixed rate.

use crate::SpeechError;

struct Pcm {
    rate: u32,
    channels: u16,
    /// Interleaved 16-bit samples.
    samples: Vec<i16>,
}

fn u16_at(b: &[u8], i: usize) -> u16 {
    u16::from_le_bytes([b[i], b[i + 1]])
}

fn u32_at(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}

fn parse(b: &[u8]) -> Result<Pcm, SpeechError> {
    let bad = |why: &str| SpeechError::NotWav(why.to_string());
    if b.len() < 12 || &b[0..4] != b"RIFF" || &b[8..12] != b"WAVE" {
        return Err(bad("no RIFF/WAVE header (was another format asked for?)"));
    }
    let mut fmt: Option<(u16, u16, u32, u16)> = None;
    let mut i = 12;
    while i + 8 <= b.len() {
        let id = &b[i..i + 4];
        let size = u32_at(b, i + 4) as usize;
        let body = i + 8;
        // A streaming server writes 0 or 0xFFFFFFFF for a data size it did not
        // know in advance: the data runs to the end of the file.
        let end = if id == b"data" && (size == 0 || body + size > b.len()) {
            b.len()
        } else {
            (body + size).min(b.len())
        };
        if id == b"fmt " && end - body >= 16 {
            fmt = Some((
                u16_at(b, body),
                u16_at(b, body + 2),
                u32_at(b, body + 4),
                u16_at(b, body + 14),
            ));
        } else if id == b"data" {
            let (format, channels, rate, bits) = fmt.ok_or_else(|| bad("data before fmt"))?;
            // 1 is PCM; 0xFFFE is WAVE_FORMAT_EXTENSIBLE, PCM in practice here.
            if !(format == 1 || format == 0xFFFE) || bits != 16 {
                return Err(bad(&format!(
                    "format {format} at {bits} bits; only 16-bit PCM is supported"
                )));
            }
            if channels == 0 || rate == 0 {
                return Err(bad("zero channels or rate"));
            }
            let samples = b[body..end]
                .chunks_exact(2)
                .map(|c| i16::from_le_bytes([c[0], c[1]]))
                .collect();
            return Ok(Pcm {
                rate,
                channels,
                samples,
            });
        }
        i = body + size + (size & 1);
    }
    Err(bad("no data chunk"))
}

fn encode(rate: u32, samples: &[i16]) -> Vec<u8> {
    let data = (samples.len() * 2) as u32;
    let mut out = Vec::with_capacity(44 + data as usize);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes()); // PCM
    out.extend_from_slice(&1u16.to_le_bytes()); // mono
    out.extend_from_slice(&rate.to_le_bytes());
    out.extend_from_slice(&(rate * 2).to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data.to_le_bytes());
    for s in samples {
        out.extend_from_slice(&s.to_le_bytes());
    }
    out
}

/// `bytes` as a canonical mono 16-bit WAV at `rate`: channels averaged, then
/// resampled linearly when the rate differs. Always re-encoded, so a header
/// with a streaming placeholder size comes out with real sizes.
pub(crate) fn normalize(bytes: &[u8], rate: u32) -> Result<Vec<u8>, SpeechError> {
    let pcm = parse(bytes)?;
    let ch = pcm.channels as usize;
    let mono: Vec<i16> = pcm
        .samples
        .chunks_exact(ch)
        .map(|f| (f.iter().map(|&s| s as i32).sum::<i32>() / ch as i32) as i16)
        .collect();
    if mono.is_empty() {
        return Err(SpeechError::Empty);
    }
    let out = if pcm.rate == rate {
        mono
    } else {
        let n = (mono.len() as u64 * rate as u64 / pcm.rate as u64).max(1) as usize;
        let step = pcm.rate as f64 / rate as f64;
        (0..n)
            .map(|i| {
                let pos = i as f64 * step;
                let j = pos as usize;
                let frac = pos - j as f64;
                let a = mono[j.min(mono.len() - 1)] as f64;
                let b = mono[(j + 1).min(mono.len() - 1)] as f64;
                (a + (b - a) * frac).round() as i16
            })
            .collect()
    };
    Ok(encode(rate, &out))
}

#[cfg(test)]
pub(crate) fn test_wav(rate: u32, channels: u16, frames: usize) -> Vec<u8> {
    let data = (frames * channels as usize * 2) as u32;
    let mut out = Vec::new();
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&channels.to_le_bytes());
    out.extend_from_slice(&rate.to_le_bytes());
    out.extend_from_slice(&(rate * 2 * channels as u32).to_le_bytes());
    out.extend_from_slice(&(2 * channels).to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data.to_le_bytes());
    for i in 0..frames * channels as usize {
        out.extend_from_slice(&((i % 100) as i16 * 100).to_le_bytes());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_target_format_passes_through_unchanged() {
        let w = test_wav(24_000, 1, 2400);
        assert_eq!(normalize(&w, 24_000).unwrap(), w);
    }

    #[test]
    fn stereo_is_averaged_and_the_rate_converted() {
        let w = test_wav(48_000, 2, 4800); // 0.1 s
        let out = parse(&normalize(&w, 24_000).unwrap()).unwrap();
        assert_eq!((out.rate, out.channels), (24_000, 1));
        assert_eq!(out.samples.len(), 2400, "same duration at half the rate");
    }

    #[test]
    fn a_streaming_header_with_no_size_is_read_to_the_end() {
        let mut w = test_wav(24_000, 1, 100);
        let at = w.len() - 200 - 4;
        w[at..at + 4].copy_from_slice(&u32::MAX.to_le_bytes());
        let out = parse(&normalize(&w, 24_000).unwrap()).unwrap();
        assert_eq!(out.samples.len(), 100);
    }

    #[test]
    fn mp3_and_float_wavs_are_refused_by_name() {
        assert!(matches!(
            normalize(b"ID3\x03\x00 mp3 bytes", 24_000),
            Err(SpeechError::NotWav(_))
        ));
        let mut float = test_wav(24_000, 1, 10);
        float[20..22].copy_from_slice(&3u16.to_le_bytes()); // IEEE float
        float[34..36].copy_from_slice(&32u16.to_le_bytes());
        assert!(matches!(
            normalize(&float, 24_000),
            Err(SpeechError::NotWav(_))
        ));
    }
}
