//! `opennotebook prep --spec <path> --job <id>` — the process the server starts
//! for a prep.
//!
//! The other half of [`crate::dispatch`]. That module writes a spec and queues
//! a job whose process is this subcommand pointed at that spec; this reads the
//! spec, and reports progress on the job it was given.

use std::path::{Path, PathBuf};

use crate::session::SessionPrepareReq;

/// Did argv ask for a prep run?
///
/// Returns `true` for `prep`, in which case main() must NOT go on to serve: a
/// prep that fell through to serving would try to bind the port the live
/// server already holds.
pub fn is_prep_invocation() -> bool {
    std::env::args().nth(1).as_deref() == Some("prep")
}

/// The `--spec <path>` argument.
///
/// A hand-rolled parse rather than an argument-parser dependency: the surface is
/// one subcommand with one flag. `prep` with no `--spec` exits non-zero rather
/// than falling through, for the bind reason above.
pub fn spec_path() -> PathBuf {
    flag("--spec").map(PathBuf::from).unwrap_or_else(|| {
        eprintln!("opennotebook prep: --spec <path> is required");
        std::process::exit(2);
    })
}

/// The `--job <id>` argument, or the job id in the environment. `None` when
/// the prep was run by hand with nothing having queued it.
pub fn job_id() -> Option<String> {
    flag("--job")
        .or_else(|| std::env::var(opennotebook_build::job::JOB_ENV).ok())
        .filter(|j| !j.is_empty())
}

fn flag(name: &str) -> Option<String> {
    let args: Vec<String> = std::env::args().collect();
    let mut it = args.iter().skip(2);
    let eq = format!("{name}=");
    while let Some(a) = it.next() {
        if a == name {
            return it.next().cloned();
        }
        if let Some(v) = a.strip_prefix(&eq) {
            return Some(v.to_string());
        }
    }
    None
}

/// Read a spec.
///
/// A failure here is an error and never a default-constructed request: a spec
/// that silently became empty would prepare an empty session and report success.
pub fn load_spec(path: &Path) -> anyhow::Result<SessionPrepareReq> {
    let bytes = std::fs::read(path)
        .map_err(|e| anyhow::anyhow!("prep spec {} unreadable: {e}", path.display()))?;
    serde_json::from_slice(&bytes)
        .map_err(|e| anyhow::anyhow!("prep spec {} did not decode: {e}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_spec_is_an_error_not_an_empty_spec() {
        let r = load_spec(Path::new("/nonexistent/prep/spec.json"));
        assert!(r.is_err());
        assert!(r.unwrap_err().to_string().contains("unreadable"));
    }

    #[test]
    fn a_spec_that_does_not_decode_is_an_error() {
        // The failure this guards: serde returning a default request, which
        // would prepare a session with an empty sid and call it success.
        let dir = std::env::temp_dir().join(format!("hs-prep-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("bad.json");
        std::fs::write(&p, b"{ not json").unwrap();
        let r = load_spec(&p);
        assert!(r.is_err());
        assert!(r.unwrap_err().to_string().contains("did not decode"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
