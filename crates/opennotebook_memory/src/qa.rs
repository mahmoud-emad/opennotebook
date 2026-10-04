//! Q&A extraction: a model reads each source once per dimension and writes
//! question-and-answer pairs grounded in it.
//!
//! The prompt, the schema and the dimension descriptions are ported from
//! the memory service's `document_questions` (MIT), so a collection extracts the same
//! material it did there.

use futures_util::{StreamExt, stream};
use opennotebook_ai::Provider;
use serde_json::{Value, json};

use crate::{Doc, MemoryError, QaPair};

/// The model extraction runs on unless told otherwise.
pub const DEFAULT_QA_MODEL: &str = "openai/gpt-4o-mini";

/// Pairs asked for per document and dimension.
const PAIRS_PER_DOCUMENT: usize = 10;
const MAX_TOKENS: u32 = 4000;
const TEMPERATURE: f32 = 0.2;
/// Extraction calls in flight at once.
const PARALLEL: usize = 4;

/// The model extraction is done with.
#[derive(Clone)]
pub struct QaModel {
    pub provider: Provider,
    pub model: String,
}

/// What a dimension asks the model to look for. A name with no description
/// here is still usable: the model is given its name alone.
pub fn dimension_description(name: &str) -> &'static str {
    match name {
        "architecture" => {
            "The high-level concept a file or module documents: what it is for, where it sits \
             in the system, who consumes it. Not line-level implementation detail."
        }
        "technology" => {
            "Technical content: architecture, technical decisions, system integrations, \
             protocols, dependencies. Not pricing, business outcomes, or customer relationships."
        }
        "product" => {
            "Product facts: features, roadmap items, user-facing behaviour, supported \
             platforms. Not pricing, internal architecture, or team assignments."
        }
        "business" => {
            "Business model facts: value propositions, customer segments, deals, revenue, \
             partnerships, market positioning. Not technical implementation, internal team \
             structure, or legal boilerplate."
        }
        _ => "",
    }
}

pub(crate) fn system_prompt(dimension: &str) -> String {
    format!(
        "You are extracting Q&A pairs from a document.\n\n\
         Dimension: {dimension}\n\
         {desc}\n\n\
         Generate up to {PAIRS_PER_DOCUMENT} pairs. Each answer must be at least 2 \
         sentences and grounded in the document. If the document does \
         not contain anything matching the dimension, return an empty \
         pairs array. Prefer precise factual phrasing.\n\n\
         Optional `anchor` is a short locator (heading, slide number, \
         page) that points the answer back to its origin in the source.",
        desc = dimension_description(dimension),
    )
}

fn response_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "minLength": 1},
                        "answer":   {"type": "string", "minLength": 1},
                        "anchor":   {"type": ["string", "null"]}
                    },
                    "required": ["question", "answer", "anchor"],
                    "additionalProperties": false
                }
            }
        },
        "required": ["pairs"],
        "additionalProperties": false
    })
}

/// The pairs in a reply. Tolerates a reply wrapped in a code fence or prose,
/// which a model without structured outputs tends to produce.
pub(crate) fn parse_pairs(text: &str) -> Result<Vec<(String, String)>, String> {
    let json = match (text.find('{'), text.rfind('}')) {
        (Some(a), Some(b)) if b > a => &text[a..=b],
        _ => return Err("the reply holds no JSON object".into()),
    };
    let v: Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
    Ok(v["pairs"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|p| {
            let q = p["question"].as_str()?.trim();
            let a = p["answer"].as_str()?.trim();
            (!q.is_empty() && !a.is_empty()).then(|| (q.to_string(), a.to_string()))
        })
        .take(PAIRS_PER_DOCUMENT)
        .collect())
}

async fn extract_one(
    m: &QaModel,
    coll: &str,
    doc: &Doc,
    dimension: &str,
) -> Result<Vec<QaPair>, MemoryError> {
    let fail = |reason: String| MemoryError::Extract {
        doc: doc.id.clone(),
        dimension: dimension.to_string(),
        reason,
    };
    let resp = m
        .provider
        .completions()
        .model(&m.model)
        .system(system_prompt(dimension))
        .user(doc.text.clone())
        .max_tokens(MAX_TOKENS)
        .temperature(TEMPERATURE)
        .json_schema("qa_pairs", true, response_schema())
        .send()
        .await
        .map_err(|e| fail(e.to_string()))?;
    opennotebook_session::spend::record("qa_extract", &m.model, resp.usage.as_ref());
    let pairs = parse_pairs(&resp.text).map_err(fail)?;
    Ok(pairs
        .into_iter()
        .enumerate()
        .map(|(i, (question, answer))| QaPair {
            id: format!("{coll}::{}::{dimension}::{i}", doc.id),
            doc_id: doc.id.clone(),
            dimension: dimension.to_string(),
            question,
            answer,
        })
        .collect())
}

/// Every document against every dimension, a few calls at a time. The first
/// failure fails the whole extraction: a collection with half its pairs looks
/// complete and is not.
pub(crate) async fn extract_all(
    m: &QaModel,
    coll: &str,
    docs: &[Doc],
    dimensions: &[String],
) -> Result<Vec<QaPair>, MemoryError> {
    let jobs: Vec<(&Doc, &str)> = docs
        .iter()
        .flat_map(|d| dimensions.iter().map(move |dim| (d, dim.as_str())))
        .collect();
    let results: Vec<Result<Vec<QaPair>, MemoryError>> = stream::iter(jobs)
        .map(|(d, dim)| extract_one(m, coll, d, dim))
        .buffered(PARALLEL)
        .collect()
        .await;
    let mut out = Vec::new();
    for r in results {
        out.extend(r?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_prompt_carries_the_dimension_and_its_words() {
        let p = system_prompt("technology");
        assert!(p.contains("Dimension: technology"));
        assert!(p.contains(dimension_description("technology")));
        assert!(p.contains("up to 10 pairs"));
    }

    #[test]
    fn pairs_parse_from_a_fenced_reply_and_empty_ones_are_dropped() {
        let reply = "```json\n{\"pairs\":[{\"question\":\"Q?\",\"answer\":\"A. B.\",\"anchor\":null},\
                     {\"question\":\" \",\"answer\":\"x\",\"anchor\":null}]}\n```";
        assert_eq!(
            parse_pairs(reply).unwrap(),
            vec![("Q?".to_string(), "A. B.".to_string())]
        );
        assert!(parse_pairs("no json here").is_err());
    }
}
