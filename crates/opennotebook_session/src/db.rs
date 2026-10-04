//! The one SQLite database: session and collection documents, and the prep jobs.
//!
//! Two processes write it — the server, and the prep child it starts — so it
//! runs in WAL mode with a busy timeout, which lets a reader and a writer from
//! either side proceed without "database is locked" in normal use.
//!
//! Calls are short (one row in, one row out), so they run on the blocking pool
//! behind one connection per process rather than through a pool.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use rusqlite::Connection;

use crate::error::StoreError;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS docs (
    kind TEXT NOT NULL,
    id   TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    sid         TEXT NOT NULL,
    status      TEXT NOT NULL,
    step        TEXT NOT NULL DEFAULT '',
    steps_done  INTEGER NOT NULL DEFAULT 0,
    steps_total INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    pid         INTEGER,
    created_ms  INTEGER NOT NULL,
    updated_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_by_sid ON jobs (sid, created_ms);
";

/// A handle on the database. Cheap to clone; every clone shares one connection.
#[derive(Clone)]
pub struct Db {
    conn: Arc<Mutex<Connection>>,
    path: Arc<PathBuf>,
}

impl Db {
    /// Open (creating if needed) the database at `path`.
    pub fn open(path: &Path) -> Result<Self, StoreError> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| StoreError::Open {
                path: path.display().to_string(),
                reason: e.to_string(),
            })?;
        }
        let conn = Connection::open(path).map_err(|e| StoreError::Open {
            path: path.display().to_string(),
            reason: e.to_string(),
        })?;
        Self::init(conn, path.to_path_buf())
    }

    /// A private database that lives as long as the handle. For tests.
    pub fn open_in_memory() -> Result<Self, StoreError> {
        let conn = Connection::open_in_memory().map_err(|e| StoreError::Open {
            path: ":memory:".into(),
            reason: e.to_string(),
        })?;
        Self::init(conn, PathBuf::from(":memory:"))
    }

    /// The process-wide handle on [`crate::paths::db_file`], opened once.
    pub fn shared() -> Result<Self, StoreError> {
        static SHARED: OnceLock<Db> = OnceLock::new();
        if let Some(db) = SHARED.get() {
            return Ok(db.clone());
        }
        let db = Self::open(&crate::paths::db_file())?;
        Ok(SHARED.get_or_init(|| db).clone())
    }

    fn init(conn: Connection, path: PathBuf) -> Result<Self, StoreError> {
        let setup = || -> rusqlite::Result<()> {
            conn.busy_timeout(Duration::from_secs(10))?;
            // `journal_mode` answers with a row, so it is a query, not an execute.
            let _: String = conn.query_row("PRAGMA journal_mode=WAL", [], |r| r.get(0))?;
            conn.execute_batch("PRAGMA synchronous=NORMAL;")?;
            conn.execute_batch(SCHEMA)
        };
        // Two processes opening a fresh file at once (the server and a prep
        // child) can both try to switch it to WAL, and the loser gets
        // SQLITE_BUSY at once: the busy timeout does not cover that lock. It
        // clears as soon as the winner is done, so retry briefly.
        let mut attempt = 0;
        loop {
            match setup() {
                Ok(()) => break,
                Err(rusqlite::Error::SqliteFailure(e, _))
                    if e.code == rusqlite::ErrorCode::DatabaseBusy && attempt < 50 =>
                {
                    attempt += 1;
                    std::thread::sleep(Duration::from_millis(20 * attempt.min(10)));
                }
                Err(e) => {
                    return Err(StoreError::Open {
                        path: path.display().to_string(),
                        reason: e.to_string(),
                    });
                }
            }
        }
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            path: Arc::new(path),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Run `f` against the connection on the blocking pool.
    pub async fn call<T, F>(&self, op: &'static str, f: F) -> Result<T, StoreError>
    where
        T: Send + 'static,
        F: FnOnce(&Connection) -> rusqlite::Result<T> + Send + 'static,
    {
        let conn = self.conn.clone();
        tokio::task::spawn_blocking(move || {
            let guard = conn.lock().unwrap_or_else(|p| p.into_inner());
            f(&guard)
        })
        .await
        .map_err(|e| StoreError::Db {
            call: op,
            reason: e.to_string(),
        })?
        .map_err(|e| StoreError::Db {
            call: op,
            reason: e.to_string(),
        })
    }
}
