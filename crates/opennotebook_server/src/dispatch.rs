//! Prep job dispatch: the server's half of starting a prep.
//!
//! The job row and the child process are [`opennotebook_build::job::submit`]'s.
//! This module supplies the two things that are the server's and not the
//! library's: the program (this binary) and the spec on disk it is pointed at.
//!
//! The spec travels as a PATH, never as interpolated values. A prep spec
//! carries a title, a resource directory and speaker display names — all free
//! text from a user — and the child is started with an argument vector, not a
//! shell, so none of it is ever parsed as a command.

use std::path::PathBuf;

use crate::session::SessionPrepareReq;

/// Write the spec and queue the prep.
///
/// Returns the job id — the same row the prep process adopts for progress and
/// the same one that lands in `Session.prep_job_sid`. It does NOT wait: a
/// 20-slide deck is minutes of work.
pub async fn submit(req: &SessionPrepareReq) -> anyhow::Result<String> {
    // The spec is written BEFORE the job is queued. The other order leaves a
    // window where a process has started that finds no spec.
    let spec_path = write_spec(req)?;
    Ok(opennotebook_build::job::submit(
        &req.sid,
        &exe(),
        vec![
            "prep".into(),
            "--spec".into(),
            spec_path.to_string_lossy().into_owned(),
        ],
    )
    .await?)
}

/// This binary, by absolute path.
///
/// **The `(deleted)` suffix is stripped.** On Linux `current_exe` reads
/// `/proc/self/exe`, which the kernel renders as `/path/opennotebook (deleted)`
/// once the file at that path has been replaced — which is what installing a
/// new build under a running server does. Stripping it means the child is the
/// binary now AT that path, the new build, rather than a path that does not
/// exist.
fn exe() -> PathBuf {
    std::env::current_exe()
        .map(|p| PathBuf::from(strip_deleted(&p.to_string_lossy())))
        .unwrap_or_else(|_| PathBuf::from("opennotebook"))
}

/// The path without the kernel's `(deleted)` marker. See [`exe`].
fn strip_deleted(path: &str) -> String {
    match path.strip_suffix(" (deleted)") {
        Some(live) => live.to_string(),
        None => path.to_string(),
    }
}

/// Where prep specs are written: the data directory rather than `/tmp`, which
/// is cleared on a schedule that has nothing to do with a job that can run for
/// an hour.
pub fn spec_dir() -> PathBuf {
    opennotebook_session::paths::data_dir().join("prep")
}

fn write_spec(req: &SessionPrepareReq) -> anyhow::Result<PathBuf> {
    let dir = spec_dir();
    std::fs::create_dir_all(&dir)?;
    let path = dir.join(format!("{}.json", req.sid));
    std::fs::write(&path, serde_json::to_vec_pretty(req)?)?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_replaced_binary_still_dispatches() {
        assert_eq!(
            strip_deleted("/home/x/bin/opennotebook (deleted)"),
            "/home/x/bin/opennotebook"
        );
        assert_eq!(
            strip_deleted("/home/x/bin/opennotebook"),
            "/home/x/bin/opennotebook"
        );
    }

    #[test]
    fn the_prep_runs_this_binary_by_absolute_path() {
        let e = exe();
        assert!(
            e.is_absolute(),
            "the child must not rely on PATH: {}",
            e.display()
        );
    }
}
