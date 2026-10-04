//! Synthesis: one WAV per line, one voice id per speaker.
//!
//! Through `opennotebook_speech`, which always hands back a 24 kHz mono
//! 16-bit WAV, so every line's duration can be read from its own header and
//! the lines join without re-encoding.

use std::collections::HashMap;
use std::path::Path;

use opennotebook_session::{NarrationLine, SessionSlide, Speaker, SpeakerId};
use opennotebook_speech::Speech;

use crate::error::BuildError;
use crate::wav;

/// The speech client the settings describe.
pub async fn connect() -> Result<Speech, BuildError> {
    Ok(opennotebook_speech::from_settings().await)
}

/// Synthesise every line of every slide, in order, writing one WAV per line.
///
/// Mutates the slides in place so that a failure part way through leaves the
/// lines that did succeed carrying their `audio_path` and `duration_ms`. The
/// caller persists that: a half-built session is worth keeping, and section 3's
/// state field is what stops it being read as finished.
pub async fn synthesise_all(
    client: &Speech,
    speakers: &[Speaker],
    slides: &mut [SessionSlide],
    audio_dir: &Path,
) -> Result<(), BuildError> {
    let voices: HashMap<&SpeakerId, &str> = speakers
        .iter()
        .map(|s| (&s.speaker_id, s.voice_id.as_str()))
        .collect();

    std::fs::create_dir_all(audio_dir).map_err(|source| BuildError::AudioWrite {
        line: String::new(),
        path: audio_dir.to_string_lossy().into_owned(),
        source,
    })?;

    // Every line, in playing order, with its voice resolved up front so an
    // unknown speaker fails before anything is synthesised.
    let mut ordinals: Vec<usize> = (0..slides.len()).collect();
    ordinals.sort_by_key(|&i| slides[i].ordinal);
    let mut work = Vec::new();
    for i in ordinals {
        let mut lines: Vec<usize> = (0..slides[i].lines.len()).collect();
        lines.sort_by_key(|&j| slides[i].lines[j].ordinal);
        for j in lines {
            let line = &slides[i].lines[j];
            let voice =
                *voices
                    .get(&line.speaker_id)
                    .ok_or_else(|| BuildError::UnknownSpeaker {
                        line: line.line_id.0.clone(),
                        speaker: line.speaker_id.0.clone(),
                    })?;
            work.push((i, j, voice, line.clone()));
        }
    }

    // A few lines at a time. A local speech server keeps a pool of engines,
    // and one line at a time left all but one idle: a 20-minute session spent
    // about twelve minutes here at 1.6x real time. Each line still writes its own file, so the order they finish in
    // does not matter; the results go back by index.
    use futures_util::StreamExt;
    let mut done =
        futures_util::stream::iter(work.into_iter().map(|(i, j, voice, mut line)| async move {
            let r = synthesise_line(client, &mut line, voice, audio_dir).await;
            (i, j, line, r)
        }))
        .buffer_unordered(TTS_PARALLEL);

    // Every finished line is kept even when another fails, so a half-built
    // session carries the audio it does have; the first error is returned.
    let mut first_err = None;
    while let Some((i, j, line, r)) = done.next().await {
        match r {
            Ok(()) => slides[i].lines[j] = line,
            Err(e) => {
                first_err.get_or_insert(e);
            }
        }
    }
    match first_err {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

/// Lines synthesised at once. Four keeps a local speech server's engines busy
/// without starving the rest of the box.
const TTS_PARALLEL: usize = 4;

/// One line. The duration comes from the header of the file that will play, not
/// from an estimate and not from the text length.
pub async fn synthesise_line(
    client: &Speech,
    line: &mut NarrationLine,
    voice_id: &str,
    audio_dir: &Path,
) -> Result<(), BuildError> {
    // Spoken, not written. A dash is a held beat on the page and silence to
    // Kokoro, so the clauses either side run together into one breathless
    // phrase. `speakable` turns it into a stop the voice actually takes — see
    // its measurements.
    //
    // The STORED text keeps its dash: that is what the subtitle and the
    // transcript show, and it is what the writer wrote.
    let bytes = client
        .synthesize(
            &opennotebook_script::budget::speakable(&line.text),
            voice_id,
        )
        .await
        .map_err(|source| BuildError::Voice {
            call: "synthesize",
            source,
        })?;
    if bytes.is_empty() {
        return Err(BuildError::EmptyAudio {
            line: line.line_id.0.clone(),
        });
    }

    let duration = wav::duration_of(&bytes, &line.line_id.0)?;
    let path = audio_dir.join(format!("{}.wav", line.line_id.0));
    std::fs::write(&path, &bytes).map_err(|source| BuildError::AudioWrite {
        line: line.line_id.0.clone(),
        path: path.to_string_lossy().into_owned(),
        source,
    })?;

    line.audio_path = Some(path.to_string_lossy().into_owned());
    line.duration_ms = Some(duration);
    Ok(())
}
