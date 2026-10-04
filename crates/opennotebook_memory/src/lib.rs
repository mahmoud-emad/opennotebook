//! Retrieval over a collection's sources.
//!
//! Two kinds of material, both in the shared SQLite database:
//!
//! * **chunks**: every source split into passages of roughly a paragraph or
//!   two (Markdown-aware, so a passage does not start mid-heading), indexed
//!   for full-text search (FTS5, BM25) and, when an embedder is configured,
//!   as vectors. [`Memory::search`] fuses the two rankings with reciprocal
//!   rank fusion, so its scores are on one scale and higher is better.
//! * **Q&A pairs**: questions and answers a model extracted from each source
//!   along named dimensions ([`Memory::qa_extract`]), searched the same way.
//!
//! A collection is addressed by `(workspace, collection)`; the workspace is
//! only a namespace, and nothing has to be created before it is written.
//!
//! Without an embedder everything still works on full-text search alone,
//! which keeps tests hermetic and lets OpenNotebook run against an endpoint
//! that offers no embedding model.

use std::sync::Arc;

use opennotebook_session::Db;
use rusqlite::{OptionalExtension, params};

mod embed;
mod error;
mod qa;
mod rank;

pub use embed::{Embedder, OpenAiEmbedder};
pub use error::MemoryError;
pub use qa::{DEFAULT_QA_MODEL, QaModel, dimension_description};

/// The embedding model, read like any setting (environment, then
/// `settings.toml`). Empty, the default, means full-text search only. Any model
/// the endpoint serves at `/embeddings` works: `openai/text-embedding-3-small`,
/// or `nomic-embed-text` on Ollama.
pub const EMBED_MODEL_KEY: &str = "OPENNOTEBOOK_EMBED_MODEL";

/// The model Q&A extraction runs on.
pub const QA_MODEL_KEY: &str = "OPENNOTEBOOK_QA_MODEL";

/// The store on the shared database, with the embedder the settings name.
pub async fn from_settings() -> Result<Memory, MemoryError> {
    use opennotebook_session::settings::setting;
    let embed_model = setting(EMBED_MODEL_KEY, "").await;
    let embedder: Option<Arc<dyn Embedder>> = if embed_model.is_empty() {
        None
    } else {
        let provider = opennotebook_session::ai::provider()
            .await
            .map_err(MemoryError::Embed)?;
        Some(Arc::new(OpenAiEmbedder::new(provider, embed_model)))
    };
    Memory::open(Db::shared()?, embedder).await
}

/// The extraction model the settings name.
pub async fn qa_model_from_settings() -> Result<QaModel, String> {
    Ok(QaModel {
        provider: opennotebook_session::ai::provider().await?,
        model: opennotebook_session::settings::setting(QA_MODEL_KEY, DEFAULT_QA_MODEL).await,
    })
}

/// Passage size, in characters: a range so a split falls on a paragraph or
/// sentence boundary rather than at an exact count.
const CHUNK_CHARS: std::ops::Range<usize> = 600..1200;

/// Candidates each ranking contributes before fusion.
const CANDIDATES_PER_HIT: usize = 4;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS mem_chunks (
    id         INTEGER PRIMARY KEY,
    coll       TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    ord        INTEGER NOT NULL,
    start      INTEGER NOT NULL,
    end        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    embed_model TEXT,
    embedding  BLOB
);
CREATE INDEX IF NOT EXISTS mem_chunks_by_coll ON mem_chunks (coll, doc_id, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS mem_chunks_fts USING fts5 (
    text, coll UNINDEXED, chunk_id UNINDEXED, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS mem_qa (
    id          TEXT PRIMARY KEY,
    coll        TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    dimension   TEXT NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    embed_model TEXT,
    embedding   BLOB
);
CREATE INDEX IF NOT EXISTS mem_qa_by_coll ON mem_qa (coll);
CREATE VIRTUAL TABLE IF NOT EXISTS mem_qa_fts USING fts5 (
    text, coll UNINDEXED, qa_id UNINDEXED, tokenize = 'porter unicode61'
);
";

/// One source to index: its id (a file name) and its Markdown.
#[derive(Debug, Clone)]
pub struct Doc {
    pub id: String,
    pub text: String,
}

/// A passage that matched a search.
#[derive(Debug, Clone, PartialEq)]
pub struct SearchHit {
    /// The source it came from.
    pub doc_id: String,
    /// Its position in that source, from 0.
    pub ord: usize,
    /// Byte offsets of the passage in the source.
    pub start: usize,
    pub end: usize,
    pub text: String,
    /// Fused rank score: higher is better, comparable only within one search.
    pub score: f64,
}

/// An extracted question and answer.
#[derive(Debug, Clone, PartialEq)]
pub struct QaPair {
    pub id: String,
    pub doc_id: String,
    pub dimension: String,
    pub question: String,
    pub answer: String,
}

/// A pair that matched a search, with its fused rank score.
#[derive(Debug, Clone, PartialEq)]
pub struct QaHit {
    pub pair: QaPair,
    pub score: f64,
}

/// What indexing wrote.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndexStats {
    pub docs: usize,
    pub chunks: usize,
    /// Chunks stored with a vector; 0 without an embedder.
    pub embedded: usize,
}

/// The retrieval store. Cheap to clone.
#[derive(Clone)]
pub struct Memory {
    db: Db,
    embedder: Option<Arc<dyn Embedder>>,
}

fn key(workspace: &str, collection: &str) -> String {
    format!("{workspace}/{collection}")
}

impl Memory {
    /// The store on `db`, creating its tables if needed. `embedder` adds
    /// vector search beside full-text search.
    pub async fn open(db: Db, embedder: Option<Arc<dyn Embedder>>) -> Result<Self, MemoryError> {
        db.call("memory.schema", |c| c.execute_batch(SCHEMA))
            .await?;
        Ok(Self { db, embedder })
    }

    pub fn has_embedder(&self) -> bool {
        self.embedder.is_some()
    }

    /// Split, embed and store `docs`. A document indexed again under the same
    /// id replaces what was there.
    pub async fn index_add(
        &self,
        workspace: &str,
        collection: &str,
        docs: &[Doc],
    ) -> Result<IndexStats, MemoryError> {
        let coll = key(workspace, collection);
        let mut rows: Vec<(String, usize, usize, usize, String)> = Vec::new();
        let splitter = text_splitter::MarkdownSplitter::new(CHUNK_CHARS);
        for d in docs {
            for (ord, (start, text)) in splitter.chunk_indices(&d.text).enumerate() {
                if text.trim().is_empty() {
                    continue;
                }
                rows.push((
                    d.id.clone(),
                    ord,
                    start,
                    start + text.len(),
                    text.to_string(),
                ));
            }
        }
        let vectors = self
            .embed(rows.iter().map(|r| r.4.clone()).collect())
            .await?;
        let model = self.embedder.as_ref().map(|e| e.model().to_string());
        let embedded = vectors.as_ref().map_or(0, Vec::len);
        let chunks = rows.len();
        let doc_ids: Vec<String> = docs.iter().map(|d| d.id.clone()).collect();

        self.db
            .call("memory.index_add", move |c| {
                let tx = c.unchecked_transaction()?;
                for id in &doc_ids {
                    delete_doc(&tx, &coll, id)?;
                }
                for (i, (doc_id, ord, start, end, text)) in rows.iter().enumerate() {
                    let blob = vectors.as_ref().map(|v| rank::to_blob(&v[i]));
                    tx.execute(
                        "INSERT INTO mem_chunks
                         (coll, doc_id, ord, start, end, text, embed_model, embedding)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                        params![
                            coll,
                            doc_id,
                            *ord as i64,
                            *start as i64,
                            *end as i64,
                            text,
                            model,
                            blob
                        ],
                    )?;
                    let id = tx.last_insert_rowid();
                    tx.execute(
                        "INSERT INTO mem_chunks_fts (text, coll, chunk_id) VALUES (?1, ?2, ?3)",
                        params![text, coll, id],
                    )?;
                }
                tx.commit()
            })
            .await?;
        Ok(IndexStats {
            docs: docs.len(),
            chunks,
            embedded,
        })
    }

    /// The passages most about `query`, best first.
    pub async fn search(
        &self,
        workspace: &str,
        collection: &str,
        query: &str,
        top_k: usize,
    ) -> Result<Vec<SearchHit>, MemoryError> {
        let coll = key(workspace, collection);
        let n = top_k.max(1) * CANDIDATES_PER_HIT;
        let fts = rank::fts_query(query);
        let qvec = self.embed_one(query).await?;
        let model = self.embedder.as_ref().map(|e| e.model().to_string());

        let rows = self
            .db
            .call("memory.search", move |c| {
                let mut lists: Vec<Vec<i64>> = Vec::new();
                if let Some(q) = &fts {
                    let mut st = c.prepare(
                        "SELECT chunk_id FROM mem_chunks_fts
                         WHERE mem_chunks_fts MATCH ?1 AND coll = ?2
                         ORDER BY rank LIMIT ?3",
                    )?;
                    lists.push(
                        st.query_map(params![q, coll, n as i64], |r| r.get(0))?
                            .collect::<rusqlite::Result<_>>()?,
                    );
                }
                if let (Some(qv), Some(m)) = (&qvec, &model) {
                    let mut st = c.prepare(
                        "SELECT id, embedding FROM mem_chunks
                         WHERE coll = ?1 AND embed_model = ?2 AND embedding IS NOT NULL",
                    )?;
                    let all: Vec<(i64, Vec<u8>)> = st
                        .query_map(params![coll, m], |r| Ok((r.get(0)?, r.get(1)?)))?
                        .collect::<rusqlite::Result<_>>()?;
                    lists.push(rank::nearest(qv, &all, n));
                }
                let fused = rank::fuse(&lists, top_k);
                let mut out = Vec::with_capacity(fused.len());
                let mut st = c.prepare(
                    "SELECT doc_id, ord, start, end, text FROM mem_chunks WHERE id = ?1",
                )?;
                for (id, score) in fused {
                    if let Some(hit) = st
                        .query_row(params![id], |r| {
                            Ok(SearchHit {
                                doc_id: r.get(0)?,
                                ord: r.get::<_, i64>(1)? as usize,
                                start: r.get::<_, i64>(2)? as usize,
                                end: r.get::<_, i64>(3)? as usize,
                                text: r.get(4)?,
                                score,
                            })
                        })
                        .optional()?
                    {
                        out.push(hit);
                    }
                }
                Ok(out)
            })
            .await?;
        Ok(rows)
    }

    /// Extract Q&A pairs from `docs` along `dimensions`, one model call per
    /// document and dimension, and store them. Returns how many were stored.
    pub async fn qa_extract(
        &self,
        workspace: &str,
        collection: &str,
        docs: &[Doc],
        dimensions: &[String],
        model: &QaModel,
    ) -> Result<usize, MemoryError> {
        let coll = key(workspace, collection);
        let pairs = qa::extract_all(model, &coll, docs, dimensions).await?;
        let vectors = self
            .embed(
                pairs
                    .iter()
                    .map(|p| format!("Q: {}\nA: {}", p.question, p.answer))
                    .collect(),
            )
            .await?;
        let emodel = self.embedder.as_ref().map(|e| e.model().to_string());
        let n = pairs.len();
        let doc_ids: Vec<String> = docs.iter().map(|d| d.id.clone()).collect();
        self.db
            .call("memory.qa_extract", move |c| {
                let tx = c.unchecked_transaction()?;
                for id in &doc_ids {
                    delete_doc_qa(&tx, &coll, id)?;
                }
                for (i, p) in pairs.iter().enumerate() {
                    let blob = vectors.as_ref().map(|v| rank::to_blob(&v[i]));
                    tx.execute(
                        "INSERT OR REPLACE INTO mem_qa
                         (id, coll, doc_id, dimension, question, answer, embed_model, embedding)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                        params![
                            p.id,
                            coll,
                            p.doc_id,
                            p.dimension,
                            p.question,
                            p.answer,
                            emodel,
                            blob
                        ],
                    )?;
                    tx.execute(
                        "INSERT INTO mem_qa_fts (text, coll, qa_id) VALUES (?1, ?2, ?3)",
                        params![format!("{}\n{}", p.question, p.answer), coll, p.id],
                    )?;
                }
                tx.commit()
            })
            .await?;
        Ok(n)
    }

    /// The pairs most about `query`, best first.
    pub async fn qa_search(
        &self,
        workspace: &str,
        collection: &str,
        query: &str,
        top_k: usize,
    ) -> Result<Vec<QaHit>, MemoryError> {
        let coll = key(workspace, collection);
        let n = top_k.max(1) * CANDIDATES_PER_HIT;
        let fts = rank::fts_query(query);
        let qvec = self.embed_one(query).await?;
        let model = self.embedder.as_ref().map(|e| e.model().to_string());
        self.db
            .call("memory.qa_search", move |c| {
                // Ranked as rowids of `mem_qa`, so both lists share one id space.
                let mut lists: Vec<Vec<i64>> = Vec::new();
                if let Some(q) = &fts {
                    let mut st = c.prepare(
                        "SELECT q.rowid FROM mem_qa_fts f JOIN mem_qa q ON q.id = f.qa_id
                         WHERE mem_qa_fts MATCH ?1 AND f.coll = ?2
                         ORDER BY f.rank LIMIT ?3",
                    )?;
                    lists.push(
                        st.query_map(params![q, coll, n as i64], |r| r.get(0))?
                            .collect::<rusqlite::Result<_>>()?,
                    );
                }
                if let (Some(qv), Some(m)) = (&qvec, &model) {
                    let mut st = c.prepare(
                        "SELECT rowid, embedding FROM mem_qa
                         WHERE coll = ?1 AND embed_model = ?2 AND embedding IS NOT NULL",
                    )?;
                    let all: Vec<(i64, Vec<u8>)> = st
                        .query_map(params![coll, m], |r| Ok((r.get(0)?, r.get(1)?)))?
                        .collect::<rusqlite::Result<_>>()?;
                    lists.push(rank::nearest(qv, &all, n));
                }
                let mut st = c.prepare(
                    "SELECT id, doc_id, dimension, question, answer FROM mem_qa WHERE rowid = ?1",
                )?;
                let mut out = Vec::new();
                for (rowid, score) in rank::fuse(&lists, top_k) {
                    if let Some(pair) = st.query_row(params![rowid], read_pair).optional()? {
                        out.push(QaHit { pair, score });
                    }
                }
                Ok(out)
            })
            .await
            .map_err(Into::into)
    }

    /// Every pair of a collection, in a stable order.
    pub async fn qa_list(
        &self,
        workspace: &str,
        collection: &str,
    ) -> Result<Vec<QaPair>, MemoryError> {
        let coll = key(workspace, collection);
        self.db
            .call("memory.qa_list", move |c| {
                let mut st = c.prepare(
                    "SELECT id, doc_id, dimension, question, answer FROM mem_qa
                     WHERE coll = ?1 ORDER BY doc_id, dimension, id",
                )?;
                st.query_map(params![coll], read_pair)?.collect()
            })
            .await
            .map_err(Into::into)
    }

    /// Forget a collection: its passages and its pairs. `true` if anything was
    /// there.
    pub async fn delete_collection(
        &self,
        workspace: &str,
        collection: &str,
    ) -> Result<bool, MemoryError> {
        let coll = key(workspace, collection);
        self.db
            .call("memory.delete", move |c| {
                let tx = c.unchecked_transaction()?;
                tx.execute("DELETE FROM mem_chunks_fts WHERE coll = ?1", params![coll])?;
                tx.execute("DELETE FROM mem_qa_fts WHERE coll = ?1", params![coll])?;
                let n = tx.execute("DELETE FROM mem_chunks WHERE coll = ?1", params![coll])?
                    + tx.execute("DELETE FROM mem_qa WHERE coll = ?1", params![coll])?;
                tx.commit()?;
                Ok(n > 0)
            })
            .await
            .map_err(Into::into)
    }

    async fn embed(&self, texts: Vec<String>) -> Result<Option<Vec<Vec<f32>>>, MemoryError> {
        let Some(e) = &self.embedder else {
            return Ok(None);
        };
        if texts.is_empty() {
            return Ok(Some(Vec::new()));
        }
        let mut out = Vec::with_capacity(texts.len());
        for batch in texts.chunks(64) {
            let got = e.embed(batch).await?;
            if got.len() != batch.len() {
                return Err(MemoryError::Embed(format!(
                    "asked for {} vectors, got {}",
                    batch.len(),
                    got.len()
                )));
            }
            out.extend(got.into_iter().map(rank::normalized));
        }
        Ok(Some(out))
    }

    async fn embed_one(&self, text: &str) -> Result<Option<Vec<f32>>, MemoryError> {
        Ok(self
            .embed(vec![text.to_string()])
            .await?
            .and_then(|mut v| v.pop()))
    }
}

fn read_pair(r: &rusqlite::Row<'_>) -> rusqlite::Result<QaPair> {
    Ok(QaPair {
        id: r.get(0)?,
        doc_id: r.get(1)?,
        dimension: r.get(2)?,
        question: r.get(3)?,
        answer: r.get(4)?,
    })
}

fn delete_doc(c: &rusqlite::Connection, coll: &str, doc_id: &str) -> rusqlite::Result<()> {
    c.execute(
        "DELETE FROM mem_chunks_fts WHERE chunk_id IN
         (SELECT id FROM mem_chunks WHERE coll = ?1 AND doc_id = ?2)",
        params![coll, doc_id],
    )?;
    c.execute(
        "DELETE FROM mem_chunks WHERE coll = ?1 AND doc_id = ?2",
        params![coll, doc_id],
    )?;
    Ok(())
}

fn delete_doc_qa(c: &rusqlite::Connection, coll: &str, doc_id: &str) -> rusqlite::Result<()> {
    c.execute(
        "DELETE FROM mem_qa_fts WHERE qa_id IN
         (SELECT id FROM mem_qa WHERE coll = ?1 AND doc_id = ?2)",
        params![coll, doc_id],
    )?;
    c.execute(
        "DELETE FROM mem_qa WHERE coll = ?1 AND doc_id = ?2",
        params![coll, doc_id],
    )?;
    Ok(())
}

#[cfg(test)]
mod tests;
