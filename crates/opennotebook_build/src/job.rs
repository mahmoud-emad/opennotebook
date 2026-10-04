//! The prep job: one row in the `jobs` table, and the child process that does
//! the work.
//!
//! Preparing a session takes minutes, so it runs in a separate process — the
//! server binary again, as `opennotebook prep --spec <file> --job <id>` — which
//! keeps a crash in a renderer or a decoder from taking the server down with
//! it, and lets a prep finish while the server restarts.
//!
//! The row carries `step`, `steps_done` and `steps_total`, written by the
//! child at the phase boundaries it already has. `steps_total = 0` means "this
//! job does not say", not "zero phases": a client renders a determinate bar
//! only when `steps_total > 0`, so [`PrepJob`] sets it at once and never leaves
//! it at zero.
//!
//! **One row per prep**, created by [`submit`] in the server and adopted by
//! [`PrepJob::adopt`] in the child, so progress lands on the row the server
//! and the player read and never beside it.
//!
//! Preps run one at a time: each is minutes of model calls and synthesis, and
//! two at once would only make both slower while doubling what is in flight.

use std::collections::HashSet;
use std::path::Path;
use std::sync::{LazyLock, Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};

use opennotebook_session::Db;
use rusqlite::{OptionalExtension, params};
use tokio::sync::Semaphore;

use crate::error::BuildError;

/// What the child is told its job id is, beside `--job`.
pub const JOB_ENV: &str = "OPENNOTEBOOK_JOB";

pub const PENDING: &str = "pending";
pub const RUNNING: &str = "running";
pub const SUCCEEDED: &str = "succeeded";
pub const FAILED: &str = "failed";
pub const CANCELLED: &str = "cancelled";

/// A job row as stored.
#[derive(Debug, Clone, PartialEq)]
pub struct JobRow {
    pub id: String,
    /// The session this job prepares.
    pub sid: String,
    pub status: String,
    pub step: String,
    pub steps_done: i64,
    pub steps_total: i64,
    pub error: String,
    pub pid: Option<i64>,
}

impl JobRow {
    /// Pending or running: work that has not ended.
    pub fn is_live(&self) -> bool {
        self.status == PENDING || self.status == RUNNING
    }
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn new_id() -> String {
    use std::sync::atomic::{AtomicU32, Ordering};
    static N: AtomicU32 = AtomicU32::new(0);
    format!(
        "j{}{:04x}{:04x}",
        now_ms(),
        std::process::id() & 0xffff,
        N.fetch_add(1, Ordering::Relaxed) & 0xffff
    )
}

/// Jobs whose child this process started and is waiting on. Their rows are
/// closed by that wait, so a status read must not second-guess them from the
/// pid: between the child being reaped and its row being closed, the pid is
/// gone but the outcome is not yet written.
fn watched() -> MutexGuard<'static, HashSet<String>> {
    static WATCHED: LazyLock<Mutex<HashSet<String>>> = LazyLock::new(Default::default);
    // A poisoned set is still the right set.
    WATCHED.lock().unwrap_or_else(|p| p.into_inner())
}

fn db() -> Result<Db, BuildError> {
    Db::shared().map_err(|source| BuildError::Store { source })
}

fn store_err(source: opennotebook_session::StoreError) -> BuildError {
    BuildError::Store { source }
}

fn read_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<JobRow> {
    Ok(JobRow {
        id: r.get(0)?,
        sid: r.get(1)?,
        status: r.get(2)?,
        step: r.get(3)?,
        steps_done: r.get(4)?,
        steps_total: r.get(5)?,
        error: r.get(6)?,
        pid: r.get(7)?,
    })
}

const COLUMNS: &str = "id, sid, status, step, steps_done, steps_total, error, pid";

/// Create a prep job for `session_sid` and start `program args.. --job <id>`
/// once no other prep is running. Returns the job id at once; it does NOT wait.
///
/// `program` is the caller's, because the binary to run is the caller's: this
/// crate is a library and does not know where the server binary lives. The
/// arguments go to the process as a vector, never through a shell, so nothing
/// in them is ever parsed as a command.
pub async fn submit(
    session_sid: &str,
    program: &Path,
    args: Vec<String>,
) -> Result<String, BuildError> {
    submit_on(&db()?, session_sid, program, args).await
}

async fn submit_on(
    db: &Db,
    session_sid: &str,
    program: &Path,
    args: Vec<String>,
) -> Result<String, BuildError> {
    let id = new_id();
    {
        let (id, sid, t) = (id.clone(), session_sid.to_string(), now_ms());
        db.call("jobs.insert", move |c| {
            c.execute(
                "INSERT INTO jobs (id, sid, status, created_ms, updated_ms)
                 VALUES (?1, ?2, 'pending', ?3, ?3)",
                params![id, sid, t],
            )
        })
        .await
        .map_err(store_err)?;
    }

    static ONE_AT_A_TIME: Semaphore = Semaphore::const_new(1);
    let (db, id2, program) = (db.clone(), id.clone(), program.to_path_buf());
    tokio::spawn(async move {
        let _permit = ONE_AT_A_TIME.acquire().await;
        // Cancelled while it waited its turn: never start it.
        match get_on(&db, &id2).await {
            Ok(Some(row)) if row.status == PENDING => {}
            _ => return,
        }
        let mut cmd = tokio::process::Command::new(&program);
        cmd.args(&args).arg("--job").arg(&id2).env(JOB_ENV, &id2);
        let mut child = match cmd.spawn() {
            Ok(child) => child,
            Err(e) => {
                let _ = end(&db, &id2, FAILED, &format!("could not start the prep: {e}")).await;
                return;
            }
        };
        watched().insert(id2.clone());
        let pid = child.id().map(i64::from);
        {
            let id = id2.clone();
            let _ = db
                .call("jobs.start", move |c| {
                    c.execute(
                        "UPDATE jobs SET status = 'running', pid = ?2, updated_ms = ?3
                         WHERE id = ?1 AND status = 'pending'",
                        params![id, pid, now_ms()],
                    )
                })
                .await;
        }
        // The child closes its own row on the way out. One that died without
        // doing so — a panic, a kill, a crash in native code — is closed here,
        // so a row never says `running` about a process that is gone.
        let exit = child.wait().await;
        if let Ok(Some(row)) = get_on(&db, &id2).await
            && row.is_live()
        {
            let why = match exit {
                Ok(s) if s.success() => None,
                Ok(s) => Some(format!("the prep process exited ({s})")),
                Err(e) => Some(format!("the prep process could not be waited on: {e}")),
            };
            let _ = match why {
                None => end(&db, &id2, SUCCEEDED, "").await,
                Some(why) => end(&db, &id2, FAILED, &why).await,
            };
        }
        watched().remove(&id2);
    });
    Ok(id)
}

/// One job, with its status corrected for a process that is gone.
pub async fn get(job_id: &str) -> Result<Option<JobRow>, BuildError> {
    get_on(&db()?, job_id).await
}

pub(crate) async fn get_on(db: &Db, job_id: &str) -> Result<Option<JobRow>, BuildError> {
    let id = job_id.to_string();
    let row = db
        .call("jobs.get", move |c| {
            c.query_row(
                &format!("SELECT {COLUMNS} FROM jobs WHERE id = ?1"),
                params![id],
                read_row,
            )
            .optional()
        })
        .await
        .map_err(store_err)?;
    Ok(row.map(|mut r| {
        // A running row whose process is gone, after a server restart that
        // lost the watcher, is a prep that died: say so rather than spin.
        if r.status == RUNNING && r.pid.is_some_and(|p| !alive(p)) && !watched().contains(&r.id) {
            r.status = FAILED.into();
            if r.error.is_empty() {
                r.error = "the prep process is no longer running".into();
            }
        }
        r
    }))
}

/// The newest job for a session, if any.
///
/// `Ok(None)` is a real state: the prep binary can be run by hand against a
/// spec with nothing having submitted it.
pub async fn find_own(session_sid: &str) -> Result<Option<String>, BuildError> {
    find_own_on(&db()?, session_sid).await
}

async fn find_own_on(db: &Db, session_sid: &str) -> Result<Option<String>, BuildError> {
    let sid = session_sid.to_string();
    db.call("jobs.find_own", move |c| {
        c.query_row(
            "SELECT id FROM jobs WHERE sid = ?1 ORDER BY created_ms DESC, id DESC LIMIT 1",
            params![sid],
            |r| r.get(0),
        )
        .optional()
    })
    .await
    .map_err(store_err)
}

/// Stop a session's prep, wherever it is: waiting its turn or halfway through
/// its slides.
///
/// `known` is the job the session row names, when it names one; every live job
/// of the session is stopped as well, since the row is written as a
/// placeholder before the job exists. A job that already ended is left as it
/// ended, so calling this on a session that is not preparing changes nothing.
/// Returns the ids of the jobs stopped.
pub async fn stop(session_sid: &str, known: Option<&str>) -> Result<Vec<String>, BuildError> {
    stop_on(&db()?, session_sid, known).await
}

async fn stop_on(
    db: &Db,
    session_sid: &str,
    known: Option<&str>,
) -> Result<Vec<String>, BuildError> {
    let (sid, known) = (session_sid.to_string(), known.unwrap_or("").to_string());
    let live: Vec<(String, Option<i64>)> = db
        .call("jobs.stop", move |c| {
            let mut st = c.prepare(
                "SELECT id, pid FROM jobs
                 WHERE (sid = ?1 OR id = ?2) AND status IN ('pending', 'running')",
            )?;
            let rows: Vec<(String, Option<i64>)> = st
                .query_map(params![sid, known], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?;
            for (id, _) in &rows {
                c.execute(
                    "UPDATE jobs SET status = 'cancelled', error = 'stopped',
                     updated_ms = ?2 WHERE id = ?1",
                    params![id, now_ms()],
                )?;
            }
            Ok(rows)
        })
        .await
        .map_err(store_err)?;
    for (_, pid) in &live {
        if let Some(pid) = pid {
            terminate(*pid);
        }
    }
    Ok(live.into_iter().map(|(id, _)| id).collect())
}

/// Close every job a previous server left live. Run once at startup: the
/// watcher that would have closed them died with that server, and a pending
/// job's turn will never come.
pub async fn recover() -> Result<usize, BuildError> {
    recover_on(&db()?).await
}

async fn recover_on(db: &Db) -> Result<usize, BuildError> {
    let live: Vec<(String, String, Option<i64>)> = db
        .call("jobs.recover", |c| {
            let mut st = c.prepare(
                "SELECT id, status, pid FROM jobs WHERE status IN ('pending', 'running')",
            )?;
            st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
                .collect()
        })
        .await
        .map_err(store_err)?;
    let mut closed = 0;
    for (id, status, pid) in live {
        // A running child that outlived its server is left to finish: it
        // closes its own row.
        if status == RUNNING && pid.is_some_and(alive) {
            continue;
        }
        end(
            db,
            &id,
            FAILED,
            "interrupted: the server restarted before this prep finished",
        )
        .await?;
        closed += 1;
    }
    Ok(closed)
}

/// Close a live row. A row that already ended keeps its outcome.
async fn end(db: &Db, job_id: &str, status: &'static str, error: &str) -> Result<(), BuildError> {
    let (id, error) = (job_id.to_string(), error.to_string());
    db.call("jobs.end", move |c| {
        c.execute(
            "UPDATE jobs SET status = ?2, error = ?3, updated_ms = ?4
             WHERE id = ?1 AND status IN ('pending', 'running')",
            params![id, status, error, now_ms()],
        )
        .map(|_| ())
    })
    .await
    .map_err(store_err)
}

#[cfg(unix)]
fn alive(pid: i64) -> bool {
    // Signal 0 checks for existence without sending anything. EPERM means it
    // exists and belongs to someone else, which still counts as alive.
    let r = unsafe { libc::kill(pid as libc::pid_t, 0) };
    r == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

#[cfg(not(unix))]
fn alive(_pid: i64) -> bool {
    true
}

#[cfg(unix)]
fn terminate(pid: i64) {
    unsafe {
        libc::kill(pid as libc::pid_t, libc::SIGTERM);
    }
}

#[cfg(not(unix))]
fn terminate(_pid: i64) {}

/// The prep's own handle on its job row.
pub struct PrepJob {
    db: Db,
    sid: String,
    total: u32,
    done: u32,
}

impl PrepJob {
    /// Take over the row [`submit`] created for this prep.
    ///
    /// `steps_total` is written immediately rather than at the first phase, so
    /// a prep screen polling before phase one gets a determinate bar instead
    /// of the "does not report" zero.
    pub async fn adopt(sid: &str, phases: u32) -> Result<Self, BuildError> {
        Self::adopt_on(db()?, sid, phases).await
    }

    pub async fn adopt_on(db: Db, sid: &str, phases: u32) -> Result<Self, BuildError> {
        let job = Self {
            db,
            sid: sid.to_string(),
            total: phases,
            done: 0,
        };
        job.write(RUNNING, "", 0, "").await?;
        Ok(job)
    }

    pub fn sid(&self) -> &str {
        &self.sid
    }

    /// A job that has written nothing yet, for a test that checks it never does.
    #[cfg(test)]
    pub(crate) fn detached(db: Db, sid: &str, phases: u32) -> Self {
        Self {
            db,
            sid: sid.to_string(),
            total: phases,
            done: 0,
        }
    }

    /// Name the phase now running. Called on entry to a phase, so `steps_done`
    /// counts phases finished rather than phases started.
    pub async fn phase(&mut self, label: &str) -> Result<(), BuildError> {
        self.write(RUNNING, label, self.done, "").await
    }

    /// Mark the phase that was running as finished.
    pub async fn phase_done(&mut self, label: &str) -> Result<(), BuildError> {
        // `steps_done` never exceeds `steps_total`, so a miscounted phase
        // cannot produce a bar past its end.
        self.done = (self.done + 1).min(self.total);
        self.write(RUNNING, label, self.done, "").await
    }

    /// Close the job. Reaching `steps_total` does not imply success, so the
    /// outcome is carried by `status` and `error`, which is where a reader is
    /// told to look for it.
    pub async fn finish(&mut self, outcome: Result<(), &BuildError>) -> Result<(), BuildError> {
        let (status, error) = match outcome {
            Ok(()) => (SUCCEEDED, String::new()),
            Err(e) => (FAILED, e.to_string()),
        };
        self.write(status, "", self.done, &error).await
    }

    async fn write(
        &self,
        status: &'static str,
        step: &str,
        done: u32,
        error: &str,
    ) -> Result<(), BuildError> {
        let (id, step, error, total) = (
            self.sid.clone(),
            step.to_string(),
            error.to_string(),
            self.total,
        );
        // A cancelled row stays cancelled: the stop was asked for, and progress
        // from a process that has not seen the signal yet must not undo it.
        let changed = self
            .db
            .call("jobs.progress", move |c| {
                c.execute(
                    "UPDATE jobs SET status = ?2, step = ?3, steps_done = ?4,
                     steps_total = ?5, error = ?6, updated_ms = ?7
                     WHERE id = ?1 AND status != 'cancelled'",
                    params![id, status, step, done, total, error, now_ms()],
                )
            })
            .await
            .map_err(store_err)?;
        if changed == 0 {
            let still = self.sid.clone();
            let exists: bool = self
                .db
                .call("jobs.exists", move |c| {
                    c.query_row("SELECT 1 FROM jobs WHERE id = ?1", params![still], |_| {
                        Ok(())
                    })
                    .optional()
                    .map(|r| r.is_some())
                })
                .await
                .map_err(store_err)?;
            if !exists {
                return Err(BuildError::NoJob {
                    job: self.sid.clone(),
                });
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn row(db: &Db, id: &str) -> JobRow {
        get_on(db, id).await.unwrap().expect("the row exists")
    }

    #[tokio::test]
    async fn a_prep_runs_and_closes_its_own_row() {
        let db = Db::open_in_memory().unwrap();
        let id = submit_on(
            &db,
            "s1",
            Path::new("/bin/sh"),
            vec!["-c".into(), "exit 0".into()],
        )
        .await
        .unwrap();
        assert_eq!(find_own_on(&db, "s1").await.unwrap().as_deref(), Some(&*id));
        for _ in 0..200 {
            if !row(&db, &id).await.is_live() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        assert_eq!(row(&db, &id).await.status, SUCCEEDED);
    }

    #[tokio::test]
    async fn a_prep_that_dies_is_failed_not_left_running() {
        let db = Db::open_in_memory().unwrap();
        let id = submit_on(
            &db,
            "s2",
            Path::new("/bin/sh"),
            vec!["-c".into(), "exit 3".into()],
        )
        .await
        .unwrap();
        for _ in 0..200 {
            if !row(&db, &id).await.is_live() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let r = row(&db, &id).await;
        assert_eq!(r.status, FAILED);
        assert!(r.error.contains("exited"), "{}", r.error);
    }

    #[tokio::test]
    async fn progress_is_written_to_the_one_row_and_a_stop_holds() {
        let db = Db::open_in_memory().unwrap();
        db.call("seed", |c| {
            c.execute(
                "INSERT INTO jobs (id, sid, status, created_ms, updated_ms)
                 VALUES ('j1', 's3', 'running', 1, 1)",
                [],
            )
        })
        .await
        .unwrap();
        let mut job = PrepJob::adopt_on(db.clone(), "j1", 4).await.unwrap();
        job.phase("ingest").await.unwrap();
        job.phase_done("ingest").await.unwrap();
        let r = row(&db, "j1").await;
        assert_eq!(
            (r.step.as_str(), r.steps_done, r.steps_total),
            ("ingest", 1, 4)
        );

        assert_eq!(stop_on(&db, "s3", None).await.unwrap(), vec!["j1"]);
        job.phase("script").await.unwrap();
        assert_eq!(row(&db, "j1").await.status, CANCELLED);
        // Stopping again finds nothing live.
        assert!(stop_on(&db, "s3", Some("j1")).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_restart_closes_what_the_last_server_left_live() {
        let db = Db::open_in_memory().unwrap();
        db.call("seed", |c| {
            c.execute_batch(
                "INSERT INTO jobs (id, sid, status, created_ms, updated_ms)
                 VALUES ('p', 's', 'pending', 1, 1);
                 INSERT INTO jobs (id, sid, status, pid, created_ms, updated_ms)
                 VALUES ('r', 's', 'running', 2147483000, 2, 2);",
            )
        })
        .await
        .unwrap();
        assert_eq!(recover_on(&db).await.unwrap(), 2);
        assert_eq!(row(&db, "p").await.status, FAILED);
        assert_eq!(row(&db, "r").await.status, FAILED);
    }

    #[tokio::test]
    async fn progress_for_a_job_that_does_not_exist_is_an_error() {
        let db = Db::open_in_memory().unwrap();
        assert!(matches!(
            PrepJob::adopt_on(db, "nope", 3).await,
            Err(BuildError::NoJob { .. })
        ));
    }
}
