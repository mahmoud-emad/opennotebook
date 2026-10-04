//! Turning text into vectors.

use async_trait::async_trait;
use opennotebook_ai::Provider;

use crate::MemoryError;

/// Something that embeds text. Vectors from different models are never
/// compared: each stored vector carries [`Embedder::model`], and a search only
/// reads vectors made by the embedder it was given.
#[async_trait]
pub trait Embedder: Send + Sync {
    /// The model's name, stored beside every vector it made.
    fn model(&self) -> &str;
    /// One vector per text, in order.
    async fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, MemoryError>;
}

/// Embeddings from an OpenAI-compatible `/embeddings` endpoint: OpenAI,
/// OpenRouter, Ollama (`nomic-embed-text`), LM Studio, vLLM.
pub struct OpenAiEmbedder {
    provider: Provider,
    model: String,
}

impl OpenAiEmbedder {
    pub fn new(provider: Provider, model: impl Into<String>) -> Self {
        Self {
            provider,
            model: model.into(),
        }
    }
}

#[async_trait]
impl Embedder for OpenAiEmbedder {
    fn model(&self) -> &str {
        &self.model
    }

    async fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, MemoryError> {
        let got = self
            .provider
            .embeddings(&self.model, texts)
            .await
            .map_err(|e| MemoryError::Embed(e.to_string()))?;
        opennotebook_session::spend::record("embed", &self.model, got.usage.as_ref());
        Ok(got.vectors)
    }
}
