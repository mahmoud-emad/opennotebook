//! Persistence for [`Session`] and [`Collection`], in the SQLite `docs` table.
//!
//! One JSON document per session (kind `session`) and one per collection
//! (kind `collection`). See `docs/phase1-spec.md` section 2 for why one
//! document rather than one per slide.

use rusqlite::{OptionalExtension, params};

use crate::db::Db;
use crate::error::StoreError;
use crate::model::{Collection, Session};

/// The kind every session row is stored under.
const SESSION_KIND: &str = "session";

/// The same, for collections. A sibling kind rather than a field on a session
/// row: a collection exists before it has any output, and a list of one kind
/// must never decode a document of the other.
const COLLECTION_KIND: &str = "collection";

/// One JSON document per id under a kind: the whole of what both stores do.
///
/// Shared so the decision in it — a document that does not decode is an error
/// and not a skip — is made once for both kinds.
#[derive(Clone)]
struct Docs {
    db: Db,
    kind: &'static str,
}

impl Docs {
    fn key(&self, id: &str) -> String {
        format!("{}:{id}", self.kind)
    }

    async fn put<T: serde::Serialize>(&self, id: &str, doc: &T) -> Result<(), StoreError> {
        let body = serde_json::to_string(doc).map_err(|source| StoreError::Encode {
            key: self.key(id),
            source,
        })?;
        let (kind, id) = (self.kind, id.to_string());
        self.db
            .call("docs.put", move |c| {
                c.execute(
                    "INSERT INTO docs (kind, id, body) VALUES (?1, ?2, ?3)
                     ON CONFLICT (kind, id) DO UPDATE SET body = excluded.body",
                    params![kind, id, body],
                )
                .map(|_| ())
            })
            .await
    }

    /// `Ok(None)` is a document that is not there.
    async fn get<T: serde::de::DeserializeOwned>(&self, id: &str) -> Result<Option<T>, StoreError> {
        let (kind, owned) = (self.kind, id.to_string());
        let body: Option<String> = self
            .db
            .call("docs.get", move |c| {
                c.query_row(
                    "SELECT body FROM docs WHERE kind = ?1 AND id = ?2",
                    params![kind, owned],
                    |r| r.get(0),
                )
                .optional()
            })
            .await?;
        let Some(body) = body else {
            return Ok(None);
        };
        serde_json::from_str(&body)
            .map(Some)
            .map_err(|source| StoreError::Decode {
                key: self.key(id),
                source,
            })
    }

    /// Every document of this kind, whole.
    ///
    /// A row whose document does not decode is an error, not a skip. Dropping
    /// it silently would make a corrupt document look like one that was never
    /// created, which is the same silent-empty shape `get` already refuses.
    async fn list<T: serde::de::DeserializeOwned>(&self) -> Result<Vec<T>, StoreError> {
        let kind = self.kind;
        let rows: Vec<(String, String)> = self
            .db
            .call("docs.list", move |c| {
                let mut st = c.prepare("SELECT id, body FROM docs WHERE kind = ?1 ORDER BY id")?;
                st.query_map(params![kind], |r| Ok((r.get(0)?, r.get(1)?)))?
                    .collect()
            })
            .await?;
        rows.into_iter()
            .map(|(id, body)| {
                serde_json::from_str(&body).map_err(|source| StoreError::Decode {
                    key: self.key(&id),
                    source,
                })
            })
            .collect()
    }

    /// `true` if a document was removed.
    async fn delete(&self, id: &str) -> Result<bool, StoreError> {
        let (kind, id) = (self.kind, id.to_string());
        self.db
            .call("docs.delete", move |c| {
                c.execute(
                    "DELETE FROM docs WHERE kind = ?1 AND id = ?2",
                    params![kind, id],
                )
                .map(|n| n > 0)
            })
            .await
    }
}

pub struct SessionStore {
    docs: Docs,
}

impl SessionStore {
    pub fn new(db: Db) -> Self {
        Self {
            docs: Docs {
                db,
                kind: SESSION_KIND,
            },
        }
    }

    /// The store on the process-wide database.
    pub fn open() -> Result<Self, StoreError> {
        Ok(Self::new(Db::shared()?))
    }

    /// The database this store writes.
    pub fn db(&self) -> &Db {
        &self.docs.db
    }

    pub async fn put(&self, session: &Session) -> Result<(), StoreError> {
        self.docs.put(&session.sid, session).await
    }

    /// `Ok(None)` is a session that is not there.
    pub async fn get(&self, sid: &str) -> Result<Option<Session>, StoreError> {
        self.docs.get(sid).await
    }

    /// Every stored session, whole.
    ///
    /// Sessions come back
    /// whole rather than as summaries because the store cannot summarise
    /// without decoding, and a caller that wants less can take less.
    pub async fn list(&self) -> Result<Vec<Session>, StoreError> {
        self.docs.list().await
    }

    /// `true` if a session was removed.
    pub async fn delete(&self, sid: &str) -> Result<bool, StoreError> {
        self.docs.delete(sid).await
    }

    /// A collection store on the same database.
    pub fn collections(&self) -> CollectionStore {
        CollectionStore {
            docs: Docs {
                db: self.docs.db.clone(),
                kind: COLLECTION_KIND,
            },
        }
    }
}

/// Persistence for [`Collection`]: one JSON document per collection, beside
/// the session rows.
pub struct CollectionStore {
    docs: Docs,
}

impl CollectionStore {
    pub async fn put(&self, c: &Collection) -> Result<(), StoreError> {
        self.docs.put(&c.cid, c).await
    }

    pub async fn get(&self, cid: &str) -> Result<Option<Collection>, StoreError> {
        self.docs.get(cid).await
    }

    pub async fn list(&self) -> Result<Vec<Collection>, StoreError> {
        self.docs.list().await
    }

    pub async fn delete(&self, cid: &str) -> Result<bool, StoreError> {
        self.docs.delete(cid).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn collections_sit_beside_sessions_not_inside_them() {
        // A collection listed as a session would fail to decode and fail the
        // whole list, so the two kinds must never see each other's rows.
        let sessions = SessionStore::new(Db::open_in_memory().unwrap());
        let c: Collection = serde_json::from_str(r#"{"cid":"same"}"#).unwrap();
        sessions.collections().put(&c).await.unwrap();
        assert!(sessions.list().await.unwrap().is_empty());
        assert!(sessions.get("same").await.unwrap().is_none());
        assert_eq!(sessions.collections().list().await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn a_document_that_does_not_decode_fails_the_list() {
        let store = SessionStore::new(Db::open_in_memory().unwrap());
        store
            .db()
            .call("seed", |c| {
                c.execute(
                    "INSERT INTO docs (kind, id, body) VALUES ('session', 'bad', '{')",
                    [],
                )
            })
            .await
            .unwrap();
        assert!(matches!(store.list().await, Err(StoreError::Decode { .. })));
    }

    #[test]
    fn a_bare_collection_document_is_named_by_the_studio() {
        let c: Collection = serde_json::from_str(r#"{"cid":"s1"}"#).unwrap();
        assert!(
            c.title_auto,
            "an untitled collection is the studio's to name"
        );
        assert!(c.title.is_empty() && !c.pinned);
    }

    #[test]
    fn a_session_from_before_collections_is_its_own() {
        let mut s: Session = serde_json::from_str(
            r#"{"sid":"s9","title":"t","collection_name":"s9","collection_sid":null,
                "deck_ref":null,"speakers":[],"slides":[],"state":"ready","prep_job_sid":null}"#,
        )
        .unwrap();
        assert_eq!(s.collection, None);
        assert_eq!(s.collection_id(), "s9");
        s.collection = Some("s1".into());
        assert_eq!(s.collection_id(), "s1");
    }
}
