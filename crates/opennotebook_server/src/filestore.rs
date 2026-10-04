//! The one-JSON-file-per-item store under `var/opennotebook/<kind>/<cid>/<id>.json`
//! that mind maps and study notes share.
//!
//! Kept out of the collection's staging directory on purpose: the build
//! ingests every file in staging (`pipeline::read_resources`), so an output
//! kept there would become a source of the next deck.

use std::path::{Path, PathBuf};

use serde::Serialize;
use serde::de::DeserializeOwned;

/// An item the store keeps: named by its collection and its own id, listed by
/// when it was made.
pub(crate) trait Stored: Serialize + DeserializeOwned {
    fn cid(&self) -> &str;
    fn id(&self) -> &str;
    fn title(&self) -> &str;
    fn created_ms(&self) -> u64;
    /// Its main parts' names: a map's main topics, the notes' key ideas.
    fn parts(&self) -> Vec<String>;
}

impl Stored for crate::mindmap::MindMap {
    fn cid(&self) -> &str {
        &self.sid
    }
    fn id(&self) -> &str {
        &self.id
    }
    fn title(&self) -> &str {
        &self.title
    }
    fn created_ms(&self) -> u64 {
        self.created_ms as u64
    }
    fn parts(&self) -> Vec<String> {
        self.root.children.iter().map(|n| n.name.clone()).collect()
    }
}

impl Stored for crate::notes::StudyNotes {
    fn cid(&self) -> &str {
        &self.sid
    }
    fn id(&self) -> &str {
        &self.id
    }
    fn title(&self) -> &str {
        &self.title
    }
    fn created_ms(&self) -> u64 {
        self.created_ms as u64
    }
    fn parts(&self) -> Vec<String> {
        self.ideas.iter().map(|i| i.heading.clone()).collect()
    }
}

pub(crate) fn path(root: &Path, cid: &str, id: &str) -> PathBuf {
    root.join(cid).join(format!("{id}.json"))
}

/// Write an item whole or not at all: into a temporary file, then renamed over
/// the real name, so a crash never leaves half an item that parses as nothing.
pub(crate) fn save<T: Stored>(root: &Path, item: &T) -> Result<(), String> {
    let dir = root.join(item.cid());
    std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let body = serde_json::to_vec_pretty(item).map_err(|e| e.to_string())?;
    write_whole(&path(root, item.cid(), item.id()), &body)
}

fn write_whole(path: &Path, body: &[u8]) -> Result<(), String> {
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, body).map_err(|e| format!("{}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}

pub(crate) fn load<T: Stored>(root: &Path, cid: &str, id: &str) -> Option<T> {
    let bytes = std::fs::read(path(root, cid, id)).ok()?;
    serde_json::from_slice(&bytes).ok()
}

/// Every readable item of one collection, newest first. A file that does not
/// parse is skipped rather than failing the list: one bad file should not hide
/// the rest.
pub(crate) fn list<T: Stored>(root: &Path, cid: &str) -> Vec<T> {
    let mut items: Vec<T> = std::fs::read_dir(root.join(cid))
        .map(|rd| {
            rd.flatten()
                .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
                .filter_map(|e| serde_json::from_slice(&std::fs::read(e.path()).ok()?).ok())
                .collect()
        })
        .unwrap_or_default();
    items.sort_by_key(|m: &T| std::cmp::Reverse(m.created_ms()));
    items
}

/// Every readable item of every collection, newest first.
pub(crate) fn list_all<T: Stored>(root: &Path) -> Vec<T> {
    let mut items: Vec<T> = std::fs::read_dir(root)
        .map(|rd| {
            rd.flatten()
                .filter(|e| e.path().is_dir())
                .filter_map(|e| e.file_name().to_str().map(str::to_string))
                .filter(|cid| crate::create::safe_sid(cid).is_ok())
                .flat_map(|cid| list::<T>(root, &cid))
                .collect()
        })
        .unwrap_or_default();
    items.sort_by_key(|m: &T| std::cmp::Reverse(m.created_ms()));
    items
}

/// `Ok(false)` when there was no such file.
pub(crate) fn remove(path: &Path) -> Result<bool, String> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// Set the `title` of a stored item, and nothing else.
///
/// Through the JSON value rather than the typed struct, so a field this build
/// does not know — written by a newer one — survives the rename. `Ok(false)`
/// when there is no such file.
pub(crate) fn retitle(path: &Path, title: &str) -> Result<bool, String> {
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(format!("{}: {e}", path.display())),
    };
    let mut value: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|e| format!("{}: {e}", path.display()))?;
    let Some(obj) = value.as_object_mut() else {
        return Err(format!("{}: not a JSON object", path.display()));
    };
    obj.insert("title".into(), serde_json::Value::String(title.to_string()));
    let body = serde_json::to_vec_pretty(&value).map_err(|e| e.to_string())?;
    write_whole(path, &body)?;
    Ok(true)
}
