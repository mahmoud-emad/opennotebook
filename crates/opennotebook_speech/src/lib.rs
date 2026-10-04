//! Speech: text to speech for narration and replies, speech to text for a
//! listener's question.
//!
//! Both go to OpenAI-compatible audio endpoints (`POST /audio/speech`,
//! `POST /audio/transcriptions`), so any of these work by changing a URL:
//!
//! * [Speaches](https://speaches.ai) — Kokoro voices and Whisper in one local
//!   server; the default, at `http://localhost:8000/v1`.
//! * [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) — speech only.
//! * OpenAI — with its own voice names.
//!
//! The voice ids OpenNotebook offers are Kokoro's (`af_bella`, `bm_george`, …),
//! which Speaches and Kokoro-FastAPI both accept as they are.
//!
//! Whatever the server sends, [`Speech::synthesize`] returns a WAV at
//! [`SAMPLE_RATE`], mono, 16-bit: narration joins clips with no re-encoding
//! and the player streams the samples straight into an `AudioContext`, so the
//! format is a contract, not a preference.

use std::time::Duration;

use serde_json::json;

mod wav;

/// The sample rate every synthesised clip is delivered at.
pub const SAMPLE_RATE: u32 = 24_000;

/// Settings, read like every other (environment, then `settings.toml`).
pub const TTS_BASE_URL_KEY: &str = "OPENNOTEBOOK_TTS_BASE_URL";
pub const STT_BASE_URL_KEY: &str = "OPENNOTEBOOK_STT_BASE_URL";
pub const TTS_MODEL_KEY: &str = "OPENNOTEBOOK_TTS_MODEL";
pub const STT_MODEL_KEY: &str = "OPENNOTEBOOK_STT_MODEL";
pub const API_KEY_KEY: &str = "OPENNOTEBOOK_SPEECH_API_KEY";

pub const BASE_URL_DEFAULT: &str = "http://localhost:8000/v1";
pub const TTS_MODEL_DEFAULT: &str = "speaches-ai/Kokoro-82M-v1.0-ONNX";
pub const STT_MODEL_DEFAULT: &str = "Systran/faster-whisper-small";

#[derive(Debug, Clone, thiserror::Error, PartialEq)]
pub enum SpeechError {
    #[error("the speech server at {url} could not be reached: {detail}")]
    Unreachable { url: String, detail: String },

    #[error("the speech server refused the request (HTTP {status}): {detail}")]
    Refused { status: u16, detail: String },

    #[error("the speech server returned audio that is not a usable WAV: {0}")]
    NotWav(String),

    #[error("the speech server returned no audio")]
    Empty,

    #[error("the transcription could not be read: {0}")]
    Decode(String),
}

/// A speech client. Cheap to clone.
#[derive(Clone, Debug)]
pub struct Speech {
    http: reqwest::Client,
    tts_url: String,
    stt_url: String,
    tts_model: String,
    stt_model: String,
    api_key: Option<String>,
}

impl Speech {
    pub fn new(
        tts_url: impl Into<String>,
        stt_url: impl Into<String>,
        tts_model: impl Into<String>,
        stt_model: impl Into<String>,
        api_key: Option<String>,
    ) -> Self {
        let trim = |s: String| s.trim().trim_end_matches('/').to_string();
        Self {
            http: reqwest::Client::builder()
                .connect_timeout(Duration::from_secs(10))
                .build()
                .unwrap_or_default(),
            tts_url: trim(tts_url.into()),
            stt_url: trim(stt_url.into()),
            tts_model: tts_model.into(),
            stt_model: stt_model.into(),
            api_key: api_key.filter(|k| !k.trim().is_empty()),
        }
    }

    fn authed(&self, req: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        match &self.api_key {
            Some(k) => req.bearer_auth(k),
            None => req,
        }
    }

    /// `text` spoken in `voice`, as a 24 kHz mono 16-bit WAV.
    pub async fn synthesize(&self, text: &str, voice: &str) -> Result<Vec<u8>, SpeechError> {
        let url = format!("{}/audio/speech", self.tts_url);
        let resp = self
            .authed(self.http.post(&url))
            .json(&json!({
                "model": self.tts_model,
                "input": text,
                "voice": voice,
                "response_format": "wav",
            }))
            .timeout(Duration::from_secs(120))
            .send()
            .await
            .map_err(|e| SpeechError::Unreachable {
                url: self.tts_url.clone(),
                detail: e.to_string(),
            })?;
        let status = resp.status();
        let bytes = resp.bytes().await.map_err(|e| SpeechError::Unreachable {
            url: self.tts_url.clone(),
            detail: e.to_string(),
        })?;
        if !status.is_success() {
            return Err(refused(status.as_u16(), &bytes));
        }
        if bytes.is_empty() {
            return Err(SpeechError::Empty);
        }
        wav::normalize(&bytes, SAMPLE_RATE)
    }

    /// The words in a WAV recording.
    pub async fn transcribe(&self, wav: &[u8]) -> Result<String, SpeechError> {
        let url = format!("{}/audio/transcriptions", self.stt_url);
        let part = reqwest::multipart::Part::bytes(wav.to_vec())
            .file_name("question.wav")
            .mime_str("audio/wav")
            .map_err(|e| SpeechError::Decode(e.to_string()))?;
        let form = reqwest::multipart::Form::new()
            .part("file", part)
            .text("model", self.stt_model.clone())
            .text("response_format", "json");
        let resp = self
            .authed(self.http.post(&url))
            .multipart(form)
            .timeout(Duration::from_secs(120))
            .send()
            .await
            .map_err(|e| SpeechError::Unreachable {
                url: self.stt_url.clone(),
                detail: e.to_string(),
            })?;
        let status = resp.status();
        let body = resp.bytes().await.map_err(|e| SpeechError::Unreachable {
            url: self.stt_url.clone(),
            detail: e.to_string(),
        })?;
        if !status.is_success() {
            return Err(refused(status.as_u16(), &body));
        }
        let v: serde_json::Value =
            serde_json::from_slice(&body).map_err(|e| SpeechError::Decode(e.to_string()))?;
        v["text"]
            .as_str()
            .map(|t| t.trim().to_string())
            .ok_or_else(|| SpeechError::Decode("no `text` in the response".into()))
    }
}

fn refused(status: u16, body: &[u8]) -> SpeechError {
    let text = String::from_utf8_lossy(body);
    let detail = serde_json::from_str::<serde_json::Value>(&text)
        .ok()
        .and_then(|v| {
            v["error"]["message"]
                .as_str()
                .or_else(|| v["detail"].as_str())
                .or_else(|| v["error"].as_str())
                .map(str::to_string)
        })
        .unwrap_or_else(|| text.chars().take(300).collect());
    SpeechError::Refused { status, detail }
}

/// The client the settings describe. Speech-to-text defaults to the same server
/// as speech, so one Speaches instance serves both.
pub async fn from_settings() -> Speech {
    use opennotebook_session::settings::setting;
    let tts_url = setting(TTS_BASE_URL_KEY, BASE_URL_DEFAULT).await;
    let stt_url = match setting(STT_BASE_URL_KEY, "").await {
        s if s.is_empty() => tts_url.clone(),
        s => s,
    };
    let key = setting(API_KEY_KEY, "").await;
    Speech::new(
        tts_url,
        stt_url,
        setting(TTS_MODEL_KEY, TTS_MODEL_DEFAULT).await,
        setting(STT_MODEL_KEY, STT_MODEL_DEFAULT).await,
        (!key.is_empty()).then_some(key),
    )
}

#[cfg(test)]
mod tests;
